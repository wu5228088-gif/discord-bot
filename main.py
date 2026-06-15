# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import csv
import gc
import io
import json
import logging
import os
import re
import time
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Optional, Tuple

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks

from tools.analyze_pjsk_snapshot import (
    build_mysekai_report,
    build_profile_report,
    draw_music_status_query,
    draw_music_status_query_pages,
)
from tools.pjsk_score_batch import (
    BONUS_MULTIPLIERS,
    DIFFICULTY_ORDER,
    build_analysis as build_pjsk_score_analysis,
    calculate_event_points,
    cache_path as pjsk_score_cache_path,
    default_master_dir as pjsk_default_master_dir,
    default_length_overrides as pjsk_length_overrides_path,
    find_chart as find_pjsk_score_chart,
    load_analysis as load_pjsk_score_analysis,
    rank_charts as rank_pjsk_score_charts,
)
from tools.snapshot_pipeline import SnapshotPipelineError, prepare_snapshot_input


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("BOT_DATA_DIR") or os.getenv("RENDER_DISK_PATH") or BASE_DIR)
ID_FILE = DATA_DIR / "idfile.json"
DATA_FILE = DATA_DIR / "event_data.json"
UPLOAD_REPORT_DIR = DATA_DIR / "reports" / "uploads"
ANALYSIS_STATE_FILE = DATA_DIR / "analysis_state.json"

TW = timezone(timedelta(hours=8))
REQUEST_CONNECT_TIMEOUT = float(os.getenv("REQUEST_CONNECT_TIMEOUT", "5"))
REQUEST_READ_TIMEOUT = float(os.getenv("REQUEST_READ_TIMEOUT", "10"))
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)
EMBED_COLOR = 0x6BFF3D
GRAPH_TITLE_SIZE = 20
GRAPH_TICK_SIZE = 12
GRAPH_LINE_COLOR = "#55BFA3"
GRAPH_FONT_WEIGHT = "black"
GRAPH_FONT_FILE = BASE_DIR / "NotoSansTC-Bold.ttf"
MAX_EVENT_HISTORY_SNAPSHOTS = int(os.getenv("MAX_EVENT_HISTORY_SNAPSHOTS", "4320"))
MAX_GRAPH_POINTS = int(os.getenv("MAX_GRAPH_POINTS", "1200"))

TOP100_URL = os.getenv(
    "HISEKAI_TOP100_URL",
    "https://api.hisekai.org/tw/event/live/top100",
)
BORDER_URL = os.getenv(
    "HISEKAI_BORDER_URL",
    "https://api.hisekai.org/tw/event/live/border",
)

MODE_CHOICES = [
    app_commands.Choice(name="總榜", value="total"),
    app_commands.Choice(name="章節榜", value="chapter"),
]

DIFFICULTY_CHOICES = [
    app_commands.Choice(name="全部", value="all"),
    app_commands.Choice(name="Easy", value="easy"),
    app_commands.Choice(name="Normal", value="normal"),
    app_commands.Choice(name="Hard", value="hard"),
    app_commands.Choice(name="Expert", value="expert"),
    app_commands.Choice(name="Master", value="master"),
    app_commands.Choice(name="Append", value="append"),
]

MUSIC_STATUS_CHOICES = [
    app_commands.Choice(name="全部", value="all"),
    app_commands.Choice(name="Clear", value="clear"),
    app_commands.Choice(name="Full Combo", value="full_combo"),
    app_commands.Choice(name="All Perfect", value="all_perfect"),
    app_commands.Choice(name="未通關", value="not_clear"),
    app_commands.Choice(name="未記錄", value="not_played"),
]

BONUS_CHOICES = [
    app_commands.Choice(name=f"{fire}火 x{multiplier}", value=fire)
    for fire, multiplier in BONUS_MULTIPLIERS.items()
]

PJSK_SCORE_SORT_CHOICES = [
    app_commands.Choice(name="活動pt", value="event_pt"),
    app_commands.Choice(name="理論分數", value="score"),
    app_commands.Choice(name="技能覆蓋率", value="skill_coverage_pct_total"),
]

PJSK_SKILL_MODE_CHOICES = [
    app_commands.Choice(name="單一倍率套用 6 段", value="single"),
    app_commands.Choice(name="6 段分別輸入", value="custom"),
]

PJSK_SCORE_MODE_CHOICES = [
    app_commands.Choice(name="多人/協力：加活躍分", value="multi"),
    app_commands.Choice(name="單人/挑戰：不加活躍分", value="solo"),
]

CHARACTER_MAP = {
    1: "星乃一歌",
    2: "天馬咲希",
    3: "望月穗波",
    4: "日野森志步",
    5: "花里實乃理",
    6: "桐谷遙",
    7: "桃井愛莉",
    8: "日野森雫",
    9: "小豆澤心羽",
    10: "白石杏",
    11: "東雲彰人",
    12: "青柳冬彌",
    13: "天馬司",
    14: "鳳笑夢",
    15: "草薙寧寧",
    16: "神代類",
    17: "宵崎奏",
    18: "朝比奈真冬",
    19: "東雲繪名",
    20: "曉山瑞希",
    21: "初音未來",
    22: "鏡音鈴",
    23: "鏡音連",
    24: "巡音流歌",
    25: "MEIKO",
    26: "KAITO",
}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "hisekai-discord-bot/2.0"})

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hisekai_bot")


def load_env_file(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and not os.environ.get(key):
            os.environ[key] = value


def configure_data_paths() -> None:
    global DATA_DIR, ID_FILE, DATA_FILE, UPLOAD_REPORT_DIR, ANALYSIS_STATE_FILE

    DATA_DIR = Path(os.getenv("BOT_DATA_DIR") or os.getenv("RENDER_DISK_PATH") or BASE_DIR)
    ID_FILE = DATA_DIR / "idfile.json"
    DATA_FILE = DATA_DIR / "event_data.json"
    UPLOAD_REPORT_DIR = DATA_DIR / "reports" / "uploads"
    ANALYSIS_STATE_FILE = DATA_DIR / "analysis_state.json"


def setup_matplotlib_font() -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.font_manager as fm
    import matplotlib.pyplot as plt

    if GRAPH_FONT_FILE.exists():
        fm.fontManager.addfont(str(GRAPH_FONT_FILE))
        font_name = fm.FontProperties(fname=str(GRAPH_FONT_FILE)).get_name()
        plt.rcParams["font.family"] = font_name
        plt.rcParams["font.sans-serif"] = [font_name]
        plt.rcParams["axes.unicode_minus"] = False
        plt.rcParams["font.weight"] = "bold"
        plt.rcParams["axes.titleweight"] = "bold"
        plt.rcParams["axes.labelweight"] = "bold"
        plt.rcParams["xtick.labelsize"] = GRAPH_TICK_SIZE
        plt.rcParams["ytick.labelsize"] = GRAPH_TICK_SIZE
        return

    preferred_fonts = [
        "Noto Sans CJK TC",
        "Noto Sans CJK JP",
        "Noto Sans TC",
        "Noto Sans JP",
        "Yu Gothic",
        "Yu Gothic UI",
        "Meiryo",
        "Microsoft JhengHei UI",
        "Microsoft JhengHei",
        "Microsoft YaHei",
        "PingFang TC",
        "SimHei",
        "MS Gothic",
        "Arial Unicode MS",
    ]
    installed = {font.name for font in fm.fontManager.ttflist}
    available_fonts = [font_name for font_name in preferred_fonts if font_name in installed]

    if available_fonts:
        plt.rcParams["font.family"] = "sans-serif"
        plt.rcParams["font.sans-serif"] = available_fonts

    plt.rcParams["axes.unicode_minus"] = False
    plt.rcParams["font.weight"] = GRAPH_FONT_WEIGHT
    plt.rcParams["axes.titleweight"] = GRAPH_FONT_WEIGHT
    plt.rcParams["axes.labelweight"] = GRAPH_FONT_WEIGHT
    plt.rcParams["xtick.labelsize"] = GRAPH_TICK_SIZE
    plt.rcParams["ytick.labelsize"] = GRAPH_TICK_SIZE


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("JSON 檔案格式錯誤，將使用預設值: %s", path)
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def safe_upload_name(name: str) -> str:
    stem = Path(name).stem
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    return safe or "snapshot"


def safe_upload_basename(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    return safe or "snapshot"


async def save_snapshot_attachment(
    attachment: discord.Attachment,
    output_dir: Path,
) -> Path:
    suffix = Path(attachment.filename).suffix.lower()
    if attachment.size and attachment.size > 50 * 1024 * 1024:
        raise ValueError("檔案太大，請上傳 50MB 以內的檔案；更大的 response/包體建議改走網頁或本地處理。")

    output_dir.mkdir(parents=True, exist_ok=True)
    if suffix == ".json":
        input_path = output_dir / f"{safe_upload_name(attachment.filename)}.json"
    else:
        input_path = output_dir / f"{safe_upload_basename(attachment.filename)}.bin"
    await attachment.save(input_path)
    return input_path


def zip_report_dir(output_dir: Path, zip_name: str) -> Path:
    zip_path = output_dir.with_name(zip_name)
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(output_dir.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(output_dir))
    return zip_path


def upload_output_dir(ctx: commands.Context, kind: str) -> Path:
    stamp = datetime.now(TW).strftime("%Y%m%d_%H%M%S")
    return UPLOAD_REPORT_DIR / f"{ctx.author.id}_{stamp}_{kind}"


def load_analysis_state() -> Dict[str, Any]:
    data = load_json(ANALYSIS_STATE_FILE, {})
    return data if isinstance(data, dict) else {}


def save_analysis_state(state: Dict[str, Any]) -> None:
    save_json(ANALYSIS_STATE_FILE, state)


def remember_analysis(user_id: int, kind: str, output_dir: Path, zip_path: Path) -> None:
    state = load_analysis_state()
    user_state = state.setdefault(str(user_id), {})
    user_state[kind] = {
        "outputDir": str(output_dir),
        "zipPath": str(zip_path),
        "updatedAt": datetime.now(TW).isoformat(timespec="seconds"),
    }
    save_analysis_state(state)


def last_analysis_dir(user_id: int, kind: str) -> Path | None:
    state = load_analysis_state()
    user_state = state.get(str(user_id))
    if not isinstance(user_state, dict):
        return None
    item = user_state.get(kind)
    if not isinstance(item, dict):
        return None
    raw_path = item.get("outputDir")
    if not isinstance(raw_path, str):
        return None
    path = Path(raw_path)
    return path if path.exists() else None


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return [dict(row) for row in csv.DictReader(fp)]


def clamp_count(value: int, *, default: int = 20, minimum: int = 1, maximum: int = 25) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(minimum, min(maximum, count))


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def sendable_lines(lines: Iterable[str], limit: int = 3600) -> str:
    selected: List[str] = []
    used = 0
    for line in lines:
        line = str(line)
        if used + len(line) + 1 > limit:
            selected.append("...")
            break
        selected.append(line)
        used += len(line) + 1
    return "\n".join(selected)


async def send_query_embed(ctx: commands.Context, title: str, lines: Iterable[str], empty: str) -> None:
    body = sendable_lines(lines)
    if not body:
        await safe_ctx_send(ctx, empty)
        return
    embed = discord.Embed(title=title, description=body, color=EMBED_COLOR)
    await safe_ctx_send(ctx, embed=embed)


async def safe_ctx_send(ctx: commands.Context, *args: Any, **kwargs: Any) -> bool:
    try:
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None and interaction.response.is_done():
            await interaction.followup.send(*args, **kwargs)
        else:
            await ctx.send(*args, **kwargs)
        return True
    except discord.NotFound:
        log.warning("Discord interaction expired before the bot could respond.")
        return False
    except discord.HTTPException:
        log.exception("Discord response send failed")
        return False


async def safe_ctx_defer(ctx: commands.Context) -> bool:
    try:
        await ctx.defer()
        return True
    except discord.NotFound:
        log.warning("Discord interaction expired before the bot could be deferred.")
        return False
    except discord.HTTPException:
        log.exception("Discord response defer failed")
        return False


def fetch_json(url: str) -> Dict[str, Any]:
    last_error: Exception | None = None
    headers = {"User-Agent": "hisekai-discord-bot/2.0"}
    for attempt in range(2):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=headers)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError(f"API 回傳格式不是 object: {url}")
            return data
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError(f"無法取得 API 資料: {url}")


async def fetch_top_data() -> Dict[str, Any]:
    return await asyncio.to_thread(fetch_json, TOP100_URL)


async def fetch_border_data() -> Dict[str, Any]:
    return await asyncio.to_thread(fetch_json, BORDER_URL)


def parse_iso_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def to_tw_naive(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(TW).replace(tzinfo=None)


def now_tw() -> datetime:
    return datetime.now(TW)


def now_tw_naive() -> datetime:
    return now_tw().replace(tzinfo=None)


def now_text() -> str:
    return now_tw().strftime("%Y-%m-%d %H:%M:%S")


def parse_snapshot_time(value: str) -> datetime:
    iso_dt = parse_iso_time(value)
    if iso_dt:
        return to_tw_naive(iso_dt)

    try:
        parsed = datetime.strptime(value, "%m/%d %H:%M")
        return parsed.replace(year=now_tw().year)
    except ValueError:
        return now_tw_naive()


def format_number(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def format_decimal(value: Any, digits: int = 3) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"

    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".")


def normalize_mode(mode: Optional[Any]) -> str:
    if isinstance(mode, app_commands.Choice):
        mode = mode.value

    value = str(mode or "total").lower()
    return value if value in {"total", "chapter"} else "total"


def is_world_link(data: Dict[str, Any]) -> bool:
    return isinstance(data.get("world_link_top_100_rankings"), list)


def event_id(data: Dict[str, Any]) -> Optional[Any]:
    return data.get("id") or data.get("event_id")


def event_name(data: Dict[str, Any]) -> str:
    return str(data.get("name") or "目前活動")


def get_world_link_chapters(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    chapters = data.get("world_link_top_100_rankings", [])
    return [chapter for chapter in chapters if isinstance(chapter, dict)]


def current_chapter_index(data: Dict[str, Any]) -> int:
    chapters = get_world_link_chapters(data)
    if not chapters:
        return 0

    now_utc = datetime.now(timezone.utc)
    first_future_index: Optional[int] = None
    last_started_index = 0

    for index, chapter in enumerate(chapters):
        start_at = parse_iso_time(chapter.get("start_at"))
        closed_at = parse_iso_time(chapter.get("closed_at"))

        if start_at and start_at <= now_utc:
            last_started_index = index

        if start_at and now_utc < start_at and first_future_index is None:
            first_future_index = index

        if start_at and closed_at and start_at <= now_utc <= closed_at:
            return index

    if first_future_index is not None and last_started_index == 0:
        return first_future_index
    return last_started_index


def current_chapter(data: Dict[str, Any]) -> Tuple[int, Optional[Dict[str, Any]]]:
    chapters = get_world_link_chapters(data)
    if not chapters:
        return 0, None

    index = current_chapter_index(data)
    if 0 <= index < len(chapters):
        return index, chapters[index]
    return 0, chapters[0]


def current_character_name(data: Dict[str, Any]) -> Optional[str]:
    _, chapter = current_chapter(data)
    if not chapter:
        return None

    character_id = (
        chapter.get("character")
        or chapter.get("character_id")
        or chapter.get("game_character_id")
    )

    if isinstance(character_id, dict):
        character_id = character_id.get("id")

    try:
        character_id = int(character_id)
    except (TypeError, ValueError):
        return None

    return CHARACTER_MAP.get(character_id, f"角色 ID {character_id}")


def mode_label(mode: str, data: Dict[str, Any]) -> str:
    if mode == "chapter":
        character = current_character_name(data)
        return f"{character}章節榜" if character else "章節榜"
    return "總榜"


def total_rankings(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("player_top_100_rankings", "top_100_player_rankings", "rankings"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def chapter_rankings(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    _, chapter = current_chapter(data)
    if not chapter:
        return []

    for key in ("player_rankings", "player_top_100_rankings", "top_100_player_rankings"):
        value = chapter.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def get_rankings(data: Dict[str, Any], mode: str) -> List[Dict[str, Any]]:
    if mode == "chapter" and is_world_link(data):
        return chapter_rankings(data)
    return total_rankings(data)


def player_id(player: Dict[str, Any]) -> str:
    profile = (
        player.get("last_player_info", {})
        .get("profile", {})
    )
    profile_id = profile.get("id")
    if profile_id is not None:
        return str(profile_id)

    direct_id = player.get("id") or player.get("user_id") or player.get("player_id")
    return str(direct_id or "")


def normalize_player(player: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": player_id(player),
        "rank": int(player.get("rank") or 0),
        "name": str(player.get("name") or "Unknown"),
        "score": int(player.get("score") or 0),
    }


def snapshot_from_rankings(rankings: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    snapshot: Dict[str, Dict[str, Any]] = {}
    for raw_player in rankings:
        player = normalize_player(raw_player)
        if player["id"]:
            snapshot[player["id"]] = {
                "rank": player["rank"],
                "name": player["name"],
                "score": player["score"],
            }
    return snapshot


def new_storage(data: Dict[str, Any]) -> Dict[str, Any]:
    current_id = event_id(data)
    return {
        "event_id": current_id,
        "id": current_id,
        "event_name": event_name(data),
        "total": [],
        "chapters": {},
    }


def normalize_storage(raw: Any, data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return new_storage(data)

    current_id = event_id(data)
    stored_id = raw.get("event_id", raw.get("id"))
    if stored_id != current_id:
        return new_storage(data)

    raw.setdefault("event_id", current_id)
    raw.setdefault("id", current_id)
    raw.setdefault("event_name", event_name(data))
    raw.setdefault("total", [])
    raw.setdefault("chapters", {})

    if not isinstance(raw["total"], list):
        raw["total"] = []
    if not isinstance(raw["chapters"], dict):
        raw["chapters"] = {}

    return raw


def snapshot_players(snapshot: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    players = snapshot.get("players", snapshot.get("data", {}))
    return players if isinstance(players, dict) else {}


def append_snapshot_if_changed(
    timeline: List[Dict[str, Any]],
    snapshot: Dict[str, Dict[str, Any]],
    captured_at: str,
) -> bool:
    if not snapshot:
        return False

    last_snapshot = snapshot_players(timeline[-1]) if timeline else {}
    if last_snapshot == snapshot:
        return False

    timeline.append({"time": captured_at, "players": snapshot})
    return True


def prune_timeline(timeline: Any, limit: int = MAX_EVENT_HISTORY_SNAPSHOTS) -> List[Dict[str, Any]]:
    if not isinstance(timeline, list):
        return []
    if limit <= 0 or len(timeline) <= limit:
        return timeline
    return timeline[-limit:]


def prune_event_storage(storage: Dict[str, Any]) -> None:
    storage["total"] = prune_timeline(storage.get("total"))
    chapters = storage.get("chapters")
    if not isinstance(chapters, dict):
        storage["chapters"] = {}
        return
    for key, timeline in list(chapters.items()):
        chapters[key] = prune_timeline(timeline)


def downsample_history_points(
    points: List[Tuple[datetime, Optional[int]]],
    max_points: int = MAX_GRAPH_POINTS,
) -> List[Tuple[datetime, Optional[int]]]:
    if max_points <= 0 or len(points) <= max_points:
        return points
    if max_points <= 2:
        return points[-max_points:]

    last_index = len(points) - 1
    step = last_index / (max_points - 1)
    selected = []
    used_indexes: set[int] = set()
    for index in range(max_points):
        source_index = min(last_index, round(index * step))
        if source_index in used_indexes:
            continue
        used_indexes.add(source_index)
        selected.append(points[source_index])
    return selected


def record_rankings(data: Dict[str, Any], storage: Dict[str, Any]) -> bool:
    captured_at = now_tw().isoformat(timespec="minutes")
    changed = False

    total_snapshot = snapshot_from_rankings(get_rankings(data, "total"))
    changed |= append_snapshot_if_changed(storage["total"], total_snapshot, captured_at)

    if is_world_link(data):
        chapters = get_world_link_chapters(data)
        for index, chapter in enumerate(chapters):
            rankings = []
            for key in ("player_rankings", "player_top_100_rankings", "top_100_player_rankings"):
                value = chapter.get(key)
                if isinstance(value, list):
                    rankings = value
                    break

            chapter_key = str(index)
            storage["chapters"].setdefault(chapter_key, [])
            chapter_snapshot = snapshot_from_rankings(rankings)
            changed |= append_snapshot_if_changed(
                storage["chapters"][chapter_key],
                chapter_snapshot,
                captured_at,
            )

    prune_event_storage(storage)
    return changed


def dataset_for_mode(
    storage: Dict[str, Any],
    data: Dict[str, Any],
    mode: str,
) -> List[Dict[str, Any]]:
    if mode == "chapter" and is_world_link(data):
        chapter_key = str(current_chapter_index(data))
        dataset = storage.get("chapters", {}).get(chapter_key, [])
    else:
        dataset = storage.get("total", [])

    return dataset if isinstance(dataset, list) else []


def start_time_for_mode(data: Dict[str, Any], mode: str) -> datetime:
    if mode == "chapter" and is_world_link(data):
        _, chapter = current_chapter(data)
        chapter_start = parse_iso_time(chapter.get("start_at")) if chapter else None
        if chapter_start:
            return to_tw_naive(chapter_start)

    event_start = parse_iso_time(data.get("start_at"))
    return to_tw_naive(event_start) if event_start else now_tw_naive()


def find_latest_player(
    dataset: Iterable[Dict[str, Any]],
    target_id: str,
) -> Optional[Dict[str, Any]]:
    for snapshot in reversed(list(dataset)):
        player = snapshot_players(snapshot).get(target_id)
        if isinstance(player, dict):
            return player
    return None


def score_diff(rankings: List[Dict[str, Any]], index: int) -> Tuple[Optional[int], Optional[int]]:
    current_score = int(rankings[index].get("score") or 0)

    gap_to_previous = None
    gap_to_next = None

    if index > 0:
        previous_score = int(rankings[index - 1].get("score") or 0)
        gap_to_previous = max(previous_score - current_score, 0)

    if index < len(rankings) - 1:
        next_score = int(rankings[index + 1].get("score") or 0)
        gap_to_next = max(current_score - next_score, 0)

    return gap_to_previous, gap_to_next


def parse_rank_query(raw: str, max_rank: int) -> Tuple[List[int], Optional[str]]:
    text = str(raw or "").strip()
    if not text:
        return [], "請輸入要查詢的名次，例如 `14`、`14,15,16,17,18` 或 `14-18`。"

    normalized = re.sub(r"[，、\s]+", ",", text)
    parts = [part for part in normalized.split(",") if part]
    ranks: List[int] = []

    for part in parts:
        range_match = re.fullmatch(r"(\d+)\s*[-~～]\s*(\d+)", part)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            step = 1 if start <= end else -1
            ranks.extend(range(start, end + step, step))
            continue

        if not part.isdigit():
            return [], f"無法解析 `{part}`，請輸入單一名次、逗號分隔名次或範圍，例如 `14`、`14,15,16,17,18`、`14-18`。"

        ranks.append(int(part))

    unique_ranks = list(dict.fromkeys(ranks))
    if len(unique_ranks) > 5:
        return [], "一次最多只能查詢 5 名玩家。"

    invalid = [rank for rank in unique_ranks if rank < 1 or rank > max_rank]
    if invalid:
        return [], f"排名必須介於 1 到 {max_rank}。"

    return unique_ranks, None


def player_stats_text(player: Dict[str, Any]) -> str:
    stats = player.get("last_1h_stats", {})
    if not isinstance(stats, dict):
        stats = {}

    last_score = int(player.get("last_score") or 0)
    lines = [
        f"排名:{player.get('rank', 0)}",
        f"總分:{format_number(player.get('score'))}",
        "",
        "**時速**",
        format_number(stats.get("speed")),
        "",
        "**週回**",
        format_number(stats.get("count")),
        "",
        "**場均**",
        format_decimal(stats.get("average")),
        "",
        "**最近一把pt**",
        format_number(last_score),
    ]

    last_played_at = parse_iso_time(player.get("last_played_at"))
    if last_played_at:
        lines.extend(["", f"最後遊玩:{to_tw_naive(last_played_at):%Y-%m-%d %H:%M:%S}"])

    return "\n".join(lines)


def make_player_embed(
    player: Dict[str, Any],
    rankings: List[Dict[str, Any]],
    index: int,
    title_prefix: str,
) -> discord.Embed:
    gap_to_previous, gap_to_next = score_diff(rankings, index)
    description_lines = player_stats_text(player).splitlines()
    gap_lines: List[str] = []

    if gap_to_previous is not None:
        previous_rank = int(player.get("rank") or 0) - 1
        gap_lines.append(f"與前一名 ({previous_rank}名) 差距: `+{format_number(gap_to_previous)}`")

    if gap_to_next is not None:
        next_rank = int(player.get("rank") or 0) + 1
        gap_lines.append(f"與後一名 ({next_rank}名) 差距: `+{format_number(gap_to_next)}`")

    if gap_lines:
        description_lines[3:3] = gap_lines + [""]

    embed = discord.Embed(
        title=f"{player.get('rank')}名-{player.get('name')}",
        description="\n".join(description_lines),
        color=EMBED_COLOR,
    )
    embed.set_footer(text=f"最後更新於: {now_text()}")
    return embed


def history_for_player_id(
    dataset: Iterable[Dict[str, Any]],
    target_id: str,
    start_at: datetime,
) -> List[Tuple[datetime, Optional[int]]]:
    points: List[Tuple[datetime, Optional[int]]] = [(start_at, None)]

    for snapshot in dataset:
        player = snapshot_players(snapshot).get(target_id)
        if not isinstance(player, dict):
            continue

        points.append((
            parse_snapshot_time(str(snapshot.get("time", ""))),
            int(player.get("score") or 0),
        ))

    return points


def history_for_rank(
    dataset: Iterable[Dict[str, Any]],
    rank: int,
    start_at: datetime,
) -> List[Tuple[datetime, Optional[int]]]:
    points: List[Tuple[datetime, Optional[int]]] = [(start_at, None)]

    for snapshot in dataset:
        players = snapshot_players(snapshot).values()
        for player in players:
            if isinstance(player, dict) and int(player.get("rank") or 0) == rank:
                points.append((
                    parse_snapshot_time(str(snapshot.get("time", ""))),
                    int(player.get("score") or 0),
                ))
                break

    return points


def plot_history(points: List[Tuple[datetime, Optional[int]]], title: str, start_at: datetime) -> io.BytesIO:
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker

    setup_matplotlib_font()
    points = downsample_history_points(points)

    plot_points = [(time, score) for time, score in points if score is not None]
    fig, ax = plt.subplots(figsize=(10, 6), dpi=140)

    if plot_points:
        times = [time for time, _ in plot_points]
        scores = [score for _, score in plot_points]
        ax.plot(
            times,
            scores,
            color=GRAPH_LINE_COLOR,
            linewidth=3.0,
        )
        ax.set_ylim(bottom=0)
    else:
        times = [start_at]
        ax.text(
            0.5,
            0.5,
            "目前沒有可用的歷史資料",
            ha="center",
            va="center",
            transform=ax.transAxes,
            fontsize=14,
            fontweight=GRAPH_FONT_WEIGHT,
        )
        ax.set_ylim(bottom=0, top=1)

    end_at = max(now_tw_naive(), max(times))
    if end_at <= start_at:
        end_at = start_at + timedelta(minutes=1)

    ax.set_xlim(start_at, end_at)
    ax.set_title(title, fontsize=GRAPH_TITLE_SIZE, fontweight=GRAPH_FONT_WEIGHT, pad=14)
    ax.set_xlabel("")
    ax.set_ylabel("")
    ax.grid(False)
    ax.yaxis.set_major_formatter(ticker.StrMethodFormatter("{x:,.0f}"))
    ax.yaxis.set_major_locator(ticker.MaxNLocator(nbins=8))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d\n%H:%M"))
    for label in ax.get_xticklabels() + ax.get_yticklabels():
        label.set_fontweight(GRAPH_FONT_WEIGHT)
        label.set_color("#000000")
    for spine in ax.spines.values():
        spine.set_color("#222222")
        spine.set_linewidth(1.1)
    fig.autofmt_xdate()
    fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    buffer.seek(0)
    plt.close(fig)
    gc.collect()
    return buffer


def border_rankings(border_data: Dict[str, Any], top_data: Dict[str, Any], mode: str) -> List[Dict[str, Any]]:
    if mode == "chapter" and is_world_link(top_data):
        chapter_index = current_chapter_index(top_data)
        chapters = border_data.get("world_link_border_rankings", [])
        if isinstance(chapters, list) and 0 <= chapter_index < len(chapters):
            chapter = chapters[chapter_index]
            if isinstance(chapter, dict):
                for key in ("player_borders", "player_border_rankings", "borders"):
                    value = chapter.get(key)
                    if isinstance(value, list):
                        return [item for item in value if isinstance(item, dict)]
        return []

    for key in ("player_border_rankings", "player_borders", "borders"):
        value = border_data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def rank_line_embed(
    border_data: Dict[str, Any],
    top_data: Dict[str, Any],
    mode: str,
) -> discord.Embed:
    rankings = get_rankings(top_data, mode)
    borders = border_rankings(border_data, top_data, mode)
    wanted_ranks = {10, 20, 30, 40, 50}
    rows: Dict[int, int] = {}

    for player in rankings:
        rank = int(player.get("rank") or 0)
        if rank in wanted_ranks:
            rows[rank] = int(player.get("score") or 0)

    for border in borders:
        rank = int(border.get("rank") or 0)
        score = int(border.get("score") or 0)
        if rank:
            rows[rank] = score

    description = "\n".join(
        f"{rank} 位：`{format_number(score)}`"
        for rank, score in sorted(rows.items())
    )
    if not description:
        description = "目前沒有榜線資料。"

    embed = discord.Embed(
        title=f"{event_name(top_data)} {mode_label(mode, top_data)}",
        description=description,
        color=EMBED_COLOR,
    )
    embed.set_footer(text=f"最後更新於: {now_text()}")
    return embed


def load_bound_ids() -> Dict[str, Dict[str, str]]:
    data = load_json(ID_FILE, {})
    return data if isinstance(data, dict) else {}


def save_bound_ids(data: Dict[str, Dict[str, str]]) -> None:
    save_json(ID_FILE, data)


def load_event_storage(top_data: Dict[str, Any]) -> Dict[str, Any]:
    storage = normalize_storage(load_json(DATA_FILE, {}), top_data)
    prune_event_storage(storage)
    return storage


load_env_file()
configure_data_paths()
TOP100_URL = os.getenv("HISEKAI_TOP100_URL", TOP100_URL)
BORDER_URL = os.getenv("HISEKAI_BORDER_URL", BORDER_URL)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix=os.getenv("COMMAND_PREFIX", "!"),
    intents=intents,
)
bot.remove_command("help")
bot.synced_commands_once = False
PJSK_SCORE_UPDATE_LOCK = asyncio.Lock()
TRACKER_BACKOFF_UNTIL: datetime | None = None


@tasks.loop(minutes=1)
async def tracker() -> None:
    global TRACKER_BACKOFF_UNTIL
    if TRACKER_BACKOFF_UNTIL and datetime.now(timezone.utc) < TRACKER_BACKOFF_UNTIL:
        return

    try:
        top_data = await fetch_top_data()
        storage = await asyncio.to_thread(load_event_storage, top_data)
        changed = await asyncio.to_thread(record_rankings, top_data, storage)
        if changed:
            await asyncio.to_thread(save_json, DATA_FILE, storage)
        TRACKER_BACKOFF_UNTIL = None
    except requests.exceptions.RequestException as exc:
        TRACKER_BACKOFF_UNTIL = datetime.now(timezone.utc) + timedelta(minutes=5)
        log.warning("背景追蹤 API 暫時失敗，5 分鐘後重試: %s", exc)
    except Exception:
        log.exception("背景追蹤更新失敗")


@tracker.before_loop
async def before_tracker() -> None:
    await bot.wait_until_ready()


async def run_pjsk_score_update(
    *,
    reason: str,
    force_download: bool = False,
    difficulties: set[str] | None = None,
    limit: Optional[int] = None,
    auto_update_menu: bool = True,
) -> dict[str, Any]:
    if PJSK_SCORE_UPDATE_LOCK.locked():
        log.info("PJSK score update skipped because another update is running: %s", reason)
        existing = await asyncio.to_thread(load_pjsk_score_analysis, DATA_DIR)
        return existing if existing else {"charts": [], "errors": [], "chart_count": 0, "error_count": 0}

    async with PJSK_SCORE_UPDATE_LOCK:
        log.info("PJSK score update started: %s", reason)
        payload = await asyncio.to_thread(
            build_pjsk_score_analysis,
            DATA_DIR,
            master_dir=pjsk_default_master_dir(),
            force_download=force_download,
            difficulties=difficulties,
            limit=limit,
            auto_update_menu=auto_update_menu,
        )
        if not pjsk_score_song_index_path().exists():
            await asyncio.to_thread(write_pjsk_score_song_index_from_analysis, payload)
        await asyncio.to_thread(load_pjsk_score_song_index)
        log.info(
            "PJSK score update finished: %s charts=%s errors=%s",
            reason,
            payload.get("chart_count"),
            payload.get("error_count"),
        )
        return payload


async def ensure_pjsk_score_cache_on_startup() -> None:
    cache_file = pjsk_score_cache_path(DATA_DIR)
    startup_update_enabled = env_flag("PJSK_STARTUP_SCORE_UPDATE", False)
    if cache_file.exists():
        analysis = await asyncio.to_thread(load_pjsk_score_analysis, DATA_DIR)
        cache_complete = bool(analysis.get("complete", True)) if analysis else False
        if not cache_complete:
            if not startup_update_enabled:
                log.warning(
                    "PJSK score cache is partial; startup auto-resume is disabled. "
                    "Run /pjskupdatescores manually or set PJSK_STARTUP_SCORE_UPDATE=1 to resume on startup."
                )
                return
            log.info("PJSK score cache is partial; startup will resume SUS analysis: %s", cache_file)
            try:
                await run_pjsk_score_update(reason="startup-resume", force_download=False, auto_update_menu=True)
            except Exception:
                log.exception("PJSK startup SUS analysis resume failed")
            return

        log.info("PJSK score cache already exists; startup full analysis skipped: %s", cache_file)
        if not pjsk_score_song_index_path().exists():
            if analysis:
                await asyncio.to_thread(write_pjsk_score_song_index_from_analysis, analysis)
                log.info("PJSK song autocomplete index rebuilt from existing score cache.")
        return

    if not startup_update_enabled:
        log.warning(
            "PJSK score cache is missing; startup full SUS analysis is disabled. "
            "Run /pjskupdatescores manually or set PJSK_STARTUP_SCORE_UPDATE=1 to build it on startup."
        )
        return

    log.info("PJSK score cache is missing; building full SUS analysis cache on startup.")
    try:
        await run_pjsk_score_update(reason="startup-initial", force_download=False, auto_update_menu=True)
        log.info("PJSK startup SUS analysis cache build finished.")
    except Exception:
        log.exception("PJSK startup SUS analysis cache build failed")


@bot.event
async def on_ready() -> None:
    if not getattr(bot, "pjsk_startup_cache_task_started", False):
        bot.pjsk_startup_cache_task_started = True
        bot.loop.create_task(ensure_pjsk_score_cache_on_startup())

    if not tracker.is_running():
        tracker.start()

    if not bot.synced_commands_once:
        try:
            await bot.tree.sync()
            bot.synced_commands_once = True
            log.info("Slash commands synced")
        except Exception:
            log.exception("Slash commands sync failed")

    log.info("Logged in as %s", bot.user)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return

    original = getattr(error, "original", None)
    if isinstance(original, discord.NotFound):
        log.warning("Discord interaction expired before the bot could respond.")
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await safe_ctx_send(ctx, f"缺少參數：`{error.param.name}`，請使用 `/help` 查看用法。")
        return

    if isinstance(error, commands.BadArgument):
        await safe_ctx_send(ctx, "參數格式錯誤，請確認 rank 是數字。")
        return

    log.exception("Command error: %s", error)
    await safe_ctx_send(ctx, "指令執行時發生錯誤，請稍後再試。")


@bot.hybrid_command(name="bind", description="綁定你的遊戲玩家 ID")
@app_commands.describe(id="遊戲玩家 ID")
async def bind(ctx: commands.Context, id: str) -> None:
    bound_ids = load_bound_ids()
    bound_ids[str(ctx.author.id)] = {"game_id": str(id)}
    save_bound_ids(bound_ids)
    await ctx.send(f"已綁定玩家 ID：`{id}`")


@bot.hybrid_command(name="graph", description="顯示已綁定玩家的分數走勢圖")
@app_commands.describe(mode="總榜或章節榜")
@app_commands.choices(mode=MODE_CHOICES)
async def graph(ctx: commands.Context, mode: str = "total") -> None:
    if not await safe_ctx_defer(ctx):
        return
    mode = normalize_mode(mode)

    bound_id = load_bound_ids().get(str(ctx.author.id), {}).get("game_id")
    if not bound_id:
        await ctx.send("尚未綁定玩家 ID，請先使用 `/bind id`。")
        return

    top_data = await fetch_top_data()
    storage = await asyncio.to_thread(load_event_storage, top_data)
    dataset = dataset_for_mode(storage, top_data, mode)
    start_at = start_time_for_mode(top_data, mode)
    points = history_for_player_id(dataset, str(bound_id), start_at)
    latest = find_latest_player(dataset, str(bound_id))

    if latest:
        title = f"{mode_label(mode, top_data)} 第{latest.get('rank')}名-{latest.get('name')}"
    else:
        title = f"{mode_label(mode, top_data)} 玩家 {bound_id}"

    buffer = await asyncio.to_thread(plot_history, points, title, start_at)
    await ctx.send(file=discord.File(buffer, "graph.png"))


@bot.hybrid_command(name="trackgraph", description="顯示某個名次本身的分數線")
@app_commands.describe(mode="總榜或章節榜", rank="要追蹤的名次")
@app_commands.choices(mode=MODE_CHOICES)
async def trackgraph(ctx: commands.Context, mode: str, rank: int) -> None:
    if not await safe_ctx_defer(ctx):
        return
    mode = normalize_mode(mode)

    if rank <= 0:
        await ctx.send("排名必須大於 0。")
        return

    top_data = await fetch_top_data()
    rankings = get_rankings(top_data, mode)
    if rank > len(rankings):
        await ctx.send(f"目前只有 {len(rankings)} 筆排名資料。")
        return

    storage = await asyncio.to_thread(load_event_storage, top_data)
    dataset = dataset_for_mode(storage, top_data, mode)
    start_at = start_time_for_mode(top_data, mode)
    points = history_for_rank(dataset, rank, start_at)
    title = f"{mode_label(mode, top_data)} 第 {rank} 名分數線"

    buffer = await asyncio.to_thread(plot_history, points, title, start_at)
    await ctx.send(file=discord.File(buffer, "trackgraph.png"))


@bot.hybrid_command(name="rankgraph", description="顯示目前指定名次玩家的歷史分數圖")
@app_commands.describe(mode="總榜或章節榜", rank="目前名次")
@app_commands.choices(mode=MODE_CHOICES)
async def rankgraph(ctx: commands.Context, mode: str, rank: int) -> None:
    if not await safe_ctx_defer(ctx):
        return
    mode = normalize_mode(mode)

    top_data = await fetch_top_data()
    rankings = get_rankings(top_data, mode)
    if rank <= 0 or rank > len(rankings):
        await ctx.send(f"排名必須介於 1 到 {len(rankings)}。")
        return

    target = rankings[rank - 1]
    target_id = player_id(target)
    storage = await asyncio.to_thread(load_event_storage, top_data)
    dataset = dataset_for_mode(storage, top_data, mode)
    start_at = start_time_for_mode(top_data, mode)
    points = history_for_player_id(dataset, target_id, start_at)
    title = f"{mode_label(mode, top_data)} 第{target.get('rank')}名-{target.get('name')}"

    buffer = await asyncio.to_thread(plot_history, points, title, start_at)
    await ctx.send(file=discord.File(buffer, "rankgraph.png"))


@bot.hybrid_command(name="trackrank", description="查詢指定名次的即時資訊")
@app_commands.describe(mode="總榜或章節榜", rank="要查詢的名次，可輸入 14,15,16,17,18 或 14-18，最多 5 名")
@app_commands.choices(mode=MODE_CHOICES)
async def trackrank(ctx: commands.Context, mode: str, rank: str) -> None:
    if not await safe_ctx_defer(ctx):
        return
    mode = normalize_mode(mode)

    top_data = await fetch_top_data()
    rankings = get_rankings(top_data, mode)
    ranks, error_message = parse_rank_query(rank, len(rankings))
    if error_message:
        await ctx.send(error_message)
        return

    embeds = []
    for target_rank in ranks:
        index = target_rank - 1
        player = rankings[index]
        embeds.append(make_player_embed(player, rankings, index, mode_label(mode, top_data)))

    await ctx.send(embeds=embeds)


@bot.hybrid_command(name="playerrank", description="查詢已綁定玩家的目前排名")
@app_commands.describe(mode="總榜或章節榜")
@app_commands.choices(mode=MODE_CHOICES)
async def playerrank(ctx: commands.Context, mode: str = "total") -> None:
    if not await safe_ctx_defer(ctx):
        return
    mode = normalize_mode(mode)

    bound_id = load_bound_ids().get(str(ctx.author.id), {}).get("game_id")
    if not bound_id:
        await ctx.send("尚未綁定玩家 ID，請先使用 `/bind id`。")
        return

    top_data = await fetch_top_data()
    rankings = get_rankings(top_data, mode)

    for index, player in enumerate(rankings):
        if player_id(player) == str(bound_id):
            embed = make_player_embed(player, rankings, index, mode_label(mode, top_data))
            await ctx.send(embed=embed)
            return

    await ctx.send("目前 Top 100 內找不到這個玩家。")


@bot.hybrid_command(name="line", description="查詢目前活動榜線")
@app_commands.describe(mode="總榜或章節榜")
@app_commands.choices(mode=MODE_CHOICES)
async def line(ctx: commands.Context, mode: str = "total") -> None:
    if not await safe_ctx_defer(ctx):
        return
    mode = normalize_mode(mode)

    top_data, border_data = await asyncio.gather(fetch_top_data(), fetch_border_data())
    embed = rank_line_embed(border_data, top_data, mode)
    await ctx.send(embed=embed)


@bot.hybrid_command(name="analyzemysekai", description="分析上傳的 MySekai JSON")
@app_commands.describe(file="原始 response、sssekai_mysekai.json 或 sssekai_mysekai_readable.json")
async def analyze_mysekai_command(ctx: commands.Context, file: discord.Attachment) -> None:
    if not await safe_ctx_defer(ctx):
        return
    output_dir = upload_output_dir(ctx, "mysekai")

    try:
        uploaded_path = await save_snapshot_attachment(file, output_dir)
        input_path = await asyncio.to_thread(
            prepare_snapshot_input,
            uploaded_path,
            output_dir,
            locale="tc",
            cache_dir=BASE_DIR / "master_cache",
        )
        args = SimpleNamespace(
            json_file=input_path,
            output_dir=output_dir,
            master_cache=BASE_DIR / "master_cache",
            locale="tc",
            no_maps=False,
        )
        await asyncio.to_thread(build_mysekai_report, args)
        gc.collect()
        zip_path = zip_report_dir(output_dir, f"{output_dir.name}.zip")
        remember_analysis(ctx.author.id, "mysekai", output_dir, zip_path)
    except (ValueError, SnapshotPipelineError) as exc:
        await ctx.send(str(exc))
        return
    except Exception:
        log.exception("MySekai snapshot analysis failed")
        await ctx.send("分析 MySekai 檔案時發生錯誤，請確認你上傳的是原始 response、sssekai JSON 或 readable JSON。")
        return

    await ctx.send(
        "MySekai 分析完成。第一張是當前拜訪角色＋資源統計，第二張是四張地圖的資源標示。",
        files=[
            discord.File(output_dir / "mysekai_current_summary.png", filename="mysekai_current_summary.png"),
            discord.File(output_dir / "mysekai_resource_map.png", filename="mysekai_resource_map.png"),
        ],
    )


async def run_profile_snapshot_analysis(
    ctx: commands.Context,
    file: discord.Attachment,
    fetch_hisekai: bool,
    fetch_top100_history: bool,
    max_events: int,
    send_zip: bool = True,
) -> None:
    output_dir = upload_output_dir(ctx, "profile")

    try:
        uploaded_path = await save_snapshot_attachment(file, output_dir)
        input_path = await asyncio.to_thread(
            prepare_snapshot_input,
            uploaded_path,
            output_dir,
            locale="tc",
            cache_dir=BASE_DIR / "master_cache",
        )
        args = SimpleNamespace(
            json_file=input_path,
            output_dir=output_dir,
            master_cache=BASE_DIR / "master_cache",
            locale="tc",
            fetch_hisekai=fetch_hisekai,
            server="tw",
            timeout=15,
            current_rank_url=None,
            history_url=None,
            fetch_top100_history=fetch_top100_history,
            max_events=max(max_events, 0),
            event_list_url="https://api.hisekai.org/{server}/event/list",
            event_top100_url="https://api.hisekai.org/{server}/event/{event_id}/top100",
        )
        await asyncio.to_thread(build_profile_report, args)
        gc.collect()
        zip_path = zip_report_dir(output_dir, f"{output_dir.name}.zip")
        remember_analysis(ctx.author.id, "profile", output_dir, zip_path)
    except (ValueError, SnapshotPipelineError) as exc:
        await ctx.send(str(exc))
        return
    except Exception:
        log.exception("Profile snapshot analysis failed")
        await ctx.send("分析玩家檔案時發生錯誤，請確認你上傳的是原始 response、sssekai JSON 或 readable JSON。")
        return

    if send_zip:
        await ctx.send(
            "玩家檔分析完成，HTML/CSV/圖表都在 zip 裡。之後可用 `/suitemusic`、`/suiteprofile` 查詢圖片。",
            file=discord.File(zip_path, filename=zip_path.name),
        )
    else:
        await ctx.send("Suite 資料已上傳並整理完成。可使用 `/suitemusic`、`/suiteprofile` 產生圖片。")


@bot.hybrid_command(name="analyzeprofile", description="分析上傳的玩家 JSON")
@app_commands.describe(
    file="原始 response、sssekai_玩家.json 或 sssekai_玩家_readable.json",
    fetch_hisekai="是否額外查 HiSekai 即時/快取資料",
    fetch_top100_history="是否掃描歷史 Top100，較慢",
    max_events="歷史 Top100 最多掃描幾期，0 表示全部",
)
async def analyze_profile_command(
    ctx: commands.Context,
    file: discord.Attachment,
    fetch_hisekai: bool = False,
    fetch_top100_history: bool = False,
    max_events: int = 40,
) -> None:
    if not await safe_ctx_defer(ctx):
        return
    await run_profile_snapshot_analysis(ctx, file, fetch_hisekai, fetch_top100_history, max_events)


@bot.hybrid_command(name="analyzesuite", description="分析上傳的 suite/玩家 JSON")
@app_commands.describe(
    file="原始 response、sssekai_suite.json、玩家 JSON 或 readable JSON",
    fetch_hisekai="是否額外查 HiSekai 即時/快取資料",
    fetch_top100_history="是否掃描歷史 Top100，較慢",
    max_events="歷史 Top100 最多掃描幾期，0 表示全部",
)
async def analyze_suite_command(
    ctx: commands.Context,
    file: discord.Attachment,
    fetch_hisekai: bool = False,
    fetch_top100_history: bool = False,
    max_events: int = 40,
) -> None:
    if not await safe_ctx_defer(ctx):
        return
    await run_profile_snapshot_analysis(ctx, file, fetch_hisekai, fetch_top100_history, max_events)


@bot.hybrid_command(name="uploadsuite", description="只上傳並整理 suite/玩家資料，不立即回傳整包報表")
@app_commands.describe(
    file="原始 response、sssekai_suite.json、玩家 JSON 或 readable JSON",
    fetch_hisekai="是否額外查 HiSekai 即時/快取資料",
    fetch_top100_history="是否掃描歷史 Top100，較慢",
    max_events="歷史 Top100 最多掃描幾期，0 表示全部",
)
async def upload_suite_command(
    ctx: commands.Context,
    file: discord.Attachment,
    fetch_hisekai: bool = False,
    fetch_top100_history: bool = False,
    max_events: int = 40,
) -> None:
    if not await safe_ctx_defer(ctx):
        return
    await run_profile_snapshot_analysis(ctx, file, fetch_hisekai, fetch_top100_history, max_events, send_zip=False)


@bot.hybrid_command(name="mysekairesources", description="查詢最近一次 MySekai 分析的資源統計")
@app_commands.describe(top="最多顯示幾項，預設 20")
async def mysekai_resources_command(ctx: commands.Context, top: int = 20) -> None:
    output_dir = last_analysis_dir(ctx.author.id, "mysekai")
    if not output_dir:
        await ctx.send("還沒有你的 MySekai 分析結果，請先使用 `/analyzemysekai file` 上傳檔案。")
        return

    rows = read_csv_rows(output_dir / "mysekai_resources.csv")
    totals: Dict[Tuple[str, str, str], int] = {}
    sites: Dict[Tuple[str, str, str], List[str]] = {}
    for row in rows:
        key = (row.get("resourceName", ""), row.get("resourceType", ""), row.get("resourceId", ""))
        try:
            quantity = int(row.get("quantity") or 0)
        except ValueError:
            quantity = 0
        totals[key] = totals.get(key, 0) + quantity
        site_text = f"{row.get('siteName', '')} {quantity}".strip()
        if site_text:
            sites.setdefault(key, []).append(site_text)

    count = clamp_count(top)
    sorted_rows = sorted(totals.items(), key=lambda item: item[1], reverse=True)[:count]
    lines = []
    for (name, resource_type, resource_id), quantity in sorted_rows:
        site_summary = " / ".join(sites.get((name, resource_type, resource_id), [])[:4])
        lines.append(f"**{name or resource_id}**：{quantity}（{site_summary}）")
    await send_query_embed(ctx, "MySekai 資源統計", lines, "這份 MySekai 報表裡沒有資源資料。")


@bot.hybrid_command(name="mysekaivisitors", description="查詢最近一次 MySekai 分析的來訪角色")
@app_commands.describe(top="最多顯示幾項，預設 20")
async def mysekai_visitors_command(ctx: commands.Context, top: int = 20) -> None:
    output_dir = last_analysis_dir(ctx.author.id, "mysekai")
    if not output_dir:
        await ctx.send("還沒有你的 MySekai 分析結果，請先使用 `/analyzemysekai file` 上傳檔案。")
        return

    rows = read_csv_rows(output_dir / "mysekai_visitors.csv")
    rows.sort(key=lambda row: int(row.get("visitCount") or 0), reverse=True)
    count = clamp_count(top)
    lines = [
        f"**{row.get('visitor') or row.get('groupId')}**：{row.get('visitCount', 0)} 次｜{row.get('characters', '')}"
        for row in rows[:count]
    ]
    await send_query_embed(ctx, "MySekai 來訪角色", lines, "這份 MySekai 報表裡沒有來訪資料。")


def normalize_music_status(value: str) -> str:
    text = (value or "").strip().lower().replace(" ", "_")
    if text in {"all_perfect", "full_perfect"} or "perfect" in text:
        return "all_perfect"
    if text == "full_combo" or "combo" in text:
        return "full_combo"
    if text == "clear":
        return "clear"
    if text in {"未通關", "not_clear"}:
        return "not_clear"
    if text in {"未記錄", "not_played", ""}:
        return "not_played"
    return text


@bot.hybrid_command(name="musicstatus", description="查詢最近一次玩家分析的歌曲 Clear/FC/AP 狀態")
@app_commands.describe(difficulty="難度", status="狀態", top="最多顯示幾首，預設 20")
@app_commands.choices(difficulty=DIFFICULTY_CHOICES, status=MUSIC_STATUS_CHOICES)
async def music_status_command(
    ctx: commands.Context,
    difficulty: str = "all",
    status: str = "all",
    top: int = 20,
) -> None:
    output_dir = last_analysis_dir(ctx.author.id, "profile")
    if not output_dir:
        await ctx.send("還沒有你的玩家檔分析結果，請先使用 `/analyzeprofile file` 上傳檔案。")
        return

    rows = read_csv_rows(output_dir / "profile_music_status.csv")
    filtered = []
    for row in rows:
        row_difficulty = (row.get("difficulty") or "").strip().lower()
        row_status = normalize_music_status(row.get("bestStatus", ""))
        if difficulty != "all" and row_difficulty != difficulty:
            continue
        if status != "all" and row_status != status:
            continue
        filtered.append(row)

    counts: Dict[str, Dict[str, int]] = {}
    for row in rows:
        row_difficulty = row.get("difficulty") or "Unknown"
        row_status = normalize_music_status(row.get("bestStatus", ""))
        counts.setdefault(row_difficulty, {})
        counts[row_difficulty][row_status] = counts[row_difficulty].get(row_status, 0) + 1

    status_label = {
        "clear": "Clear",
        "full_combo": "Full Combo",
        "all_perfect": "All Perfect",
        "not_clear": "未通關",
        "not_played": "未記錄",
    }
    count_lines = []
    for diff_name in ["Easy", "Normal", "Hard", "Expert", "Master", "Append"]:
        values = counts.get(diff_name)
        if not values:
            continue
        count_lines.append(
            f"{diff_name}: "
            + " / ".join(f"{status_label.get(key, key)} {value}" for key, value in sorted(values.items()))
        )

    count = clamp_count(top)
    song_lines = [
        f"`{row.get('musicId')}` **{row.get('title')}** [{row.get('difficulty')}] {row.get('bestStatus')}｜{row.get('highScore')}"
        for row in filtered[:count]
    ]
    lines = count_lines + ([""] if count_lines and song_lines else []) + song_lines
    await send_query_embed(ctx, "歌曲狀態", lines, "這份玩家報表裡沒有符合條件的歌曲資料。")


@bot.hybrid_command(name="cardstatus", description="查詢最近一次玩家分析的卡片狀態")
@app_commands.describe(top="最多顯示幾張，預設 20")
async def card_status_command(ctx: commands.Context, top: int = 20) -> None:
    output_dir = last_analysis_dir(ctx.author.id, "profile")
    if not output_dir:
        await ctx.send("還沒有你的玩家檔分析結果，請先使用 `/analyzeprofile file` 上傳檔案。")
        return

    rows = read_csv_rows(output_dir / "profile_cards.csv")
    rarity_counts: Dict[str, int] = {}
    for row in rows:
        rarity = row.get("rarity") or "Unknown"
        rarity_counts[rarity] = rarity_counts.get(rarity, 0) + 1

    count = clamp_count(top)
    summary = " / ".join(f"{key} {value}" for key, value in sorted(rarity_counts.items()))
    card_lines = [
        f"`{row.get('cardId')}` **{row.get('cardName')}**｜{row.get('character')}｜{row.get('rarity')}｜Lv {row.get('level')}｜MR {row.get('masterRank')}"
        for row in rows[:count]
    ]
    lines = ([summary, ""] if summary else []) + card_lines
    await send_query_embed(ctx, "卡片狀態", lines, "這份玩家報表裡沒有卡片資料。")


@bot.hybrid_command(name="eventhistory", description="查詢最近一次玩家分析的活動分數和排名")
@app_commands.describe(top="最多顯示幾期，預設 20")
async def event_history_command(ctx: commands.Context, top: int = 20) -> None:
    output_dir = last_analysis_dir(ctx.author.id, "profile")
    if not output_dir:
        await ctx.send("還沒有你的玩家檔分析結果，請先使用 `/analyzeprofile file` 上傳檔案。")
        return

    rows = read_csv_rows(output_dir / "profile_events.csv")
    rows.sort(key=lambda row: int(row.get("eventId") or 0), reverse=True)
    count = clamp_count(top)
    lines = []
    for row in rows[:count]:
        rank = row.get("rank") or (f"Top {row.get('rankUpperBound')}" if row.get("rankUpperBound") else "排名未記錄")
        score = row.get("score") or "分數未記錄"
        chapter = f"｜章節 {row.get('chapter')}" if row.get("chapter") else ""
        lines.append(f"`{row.get('eventId') or '-'}` **{row.get('eventName')}**{chapter}｜{rank}｜{score}｜{row.get('source')}")
    await send_query_embed(ctx, "活動紀錄", lines, "這份玩家報表裡沒有活動資料。")


@bot.hybrid_command(name="suitemusic", description="依難度種類或歌曲等級條列 Suite 歌曲通關狀態")
@app_commands.describe(
    mode="選擇用難度種類或歌曲等級查詢",
    value="請用選單選 expert/master/append 或 14~38",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="難度種類", value="difficulty"),
        app_commands.Choice(name="歌曲等級", value="level"),
    ]
)
async def suite_music_image_command(
    ctx: commands.Context,
    mode: str = "difficulty",
    value: str = "master",
) -> None:
    if not await safe_ctx_defer(ctx):
        return
    output_dir = last_analysis_dir(ctx.author.id, "profile")
    if not output_dir:
        await ctx.send("還沒有你的 Suite 資料，請先使用 `/uploadsuite file` 或 `/analyzesuite file` 上傳。")
        return

    rows = read_csv_rows(output_dir / "profile_music_status.csv")
    if not rows:
        await ctx.send("找不到歌曲資料，請重新上傳 Suite 檔案。")
        return

    image_names = await asyncio.to_thread(draw_music_status_query_pages, rows, output_dir, mode=mode, value=value, per_page=30)
    gc.collect()
    if not image_names:
        await ctx.send("沒有符合條件的歌曲資料。")
        return

    for index in range(0, len(image_names), 10):
        batch = image_names[index : index + 10]
        files = [discord.File(output_dir / image_name, filename=image_name) for image_name in batch]
        await ctx.send(files=files)


@suite_music_image_command.autocomplete("value")
async def suite_music_value_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    mode = getattr(interaction.namespace, "mode", "difficulty")
    current = (current or "").lower()
    if mode == "level":
        values = [str(level) for level in range(14, 39)]
    else:
        values = ["expert", "master", "append"]
    return [
        app_commands.Choice(name=value, value=value)
        for value in values
        if not current or value.startswith(current)
    ][:25]


@bot.hybrid_command(name="suiteprofile", description="回傳 Suite 個人資料整理圖片")
async def suite_profile_image_command(ctx: commands.Context) -> None:
    output_dir = last_analysis_dir(ctx.author.id, "profile")
    if not output_dir:
        await ctx.send("還沒有你的 Suite 資料，請先使用 `/uploadsuite file` 或 `/analyzesuite file` 上傳。")
        return

    path = output_dir / "profile_overview.png"
    if not path.exists():
        await ctx.send("找不到個人資料圖片，請重新上傳 Suite 檔案。")
        return
    await ctx.send(file=discord.File(path, filename=path.name))


PJSK_SCORE_SONG_INDEX_CACHE: list[dict[str, Any]] = []
PJSK_SCORE_SONG_INDEX_MTIME: float | None = None


def pjsk_score_song_index_path() -> Path:
    return DATA_DIR / "pjsk_score_song_index.json"


def load_pjsk_score_song_index() -> list[dict[str, Any]]:
    global PJSK_SCORE_SONG_INDEX_CACHE, PJSK_SCORE_SONG_INDEX_MTIME
    path = pjsk_score_song_index_path()
    if not path.exists():
        PJSK_SCORE_SONG_INDEX_CACHE = []
        PJSK_SCORE_SONG_INDEX_MTIME = None
        return []

    mtime = path.stat().st_mtime
    if PJSK_SCORE_SONG_INDEX_MTIME == mtime:
        return PJSK_SCORE_SONG_INDEX_CACHE

    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("PJSK song index load failed: %s", path)
        PJSK_SCORE_SONG_INDEX_CACHE = []
        PJSK_SCORE_SONG_INDEX_MTIME = None
        return []

    PJSK_SCORE_SONG_INDEX_CACHE = rows if isinstance(rows, list) else []
    PJSK_SCORE_SONG_INDEX_MTIME = mtime
    return PJSK_SCORE_SONG_INDEX_CACHE


def write_pjsk_score_song_index_from_analysis(analysis: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for chart in analysis.get("charts", []):
        try:
            music_id = int(chart["music_id"])
            difficulty = str(chart["difficulty"])
        except (KeyError, TypeError, ValueError):
            continue
        key = (music_id, difficulty)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "music_id": music_id,
                "title": str(chart.get("title") or ""),
                "difficulty": difficulty,
                "level": int(chart.get("level") or 0),
            }
        )
    rows.sort(key=lambda row: (row["music_id"], row["difficulty"]))
    path = pjsk_score_song_index_path()
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    load_pjsk_score_song_index()


def load_pjsk_score_cache_or_none() -> dict[str, Any] | None:
    return load_pjsk_score_analysis(DATA_DIR)


async def load_pjsk_score_cache_or_none_async() -> dict[str, Any] | None:
    return await asyncio.to_thread(load_pjsk_score_cache_or_none)


def resolve_skill_multipliers(
    mode: str = "single",
    skill_multiplier: float = 3.7,
    skill1: Optional[float] = None,
    skill2: Optional[float] = None,
    skill3: Optional[float] = None,
    skill4: Optional[float] = None,
    skill5: Optional[float] = None,
    skill6: Optional[float] = None,
) -> list[float]:
    default_multiplier = 3.7 if skill_multiplier is None else float(skill_multiplier)
    if mode == "custom":
        values = [skill1, skill2, skill3, skill4, skill5, skill6]
        return [float(value if value is not None else default_multiplier) for value in values]
    return [default_multiplier] * 6


def active_bonus_power_multiplier_for_mode(mode: str) -> float:
    # Multi-live active bonus is independent score: five players' total power * 7.5%.
    # With the bot assumption that all five players have the input power, this adds
    # input_power * 5 * 0.075 = input_power * 0.375 to the final score.
    return 0.375 if mode == "multi" else 0.0


def format_number_range(low: float, high: float, *, digits: int = 0, suffix: str = "") -> str:
    if abs(low - high) < 10 ** (-(digits + 1)):
        return f"{low:.{digits}f}{suffix}"
    return f"{low:.{digits}f}-{high:.{digits}f}{suffix}"


def format_skill_coverages(
    chart: dict[str, Any],
    skill_multipliers: list[float] | None = None,
    *,
    team_power: int | None = None,
    total_score: float | None = None,
    use_fever: bool = True,
) -> str:
    multipliers = skill_multipliers or [3.7] * 6
    parts = []
    suffix = "" if use_fever else "_no_fever"
    skill_terms = chart.get(f"skill_score_terms{suffix}") or chart.get("skill_score_terms") or []
    skill_terms_min = chart.get(f"skill_score_terms_min{suffix}") or chart.get("skill_score_terms_min") or skill_terms
    skill_terms_max = chart.get(f"skill_score_terms_max{suffix}") or chart.get("skill_score_terms_max") or skill_terms
    
    for row in chart.get("skill_coverages", [])[:6]:
        index = int(row["index"])
        multiplier = multipliers[index - 1] if index - 1 < len(multipliers) else multipliers[-1]
        fever_weight = float(row.get("fever_covered_weight") or 0)
        
        segment_score_min = None
        segment_score_max = None
        segment_pct_min = None
        segment_pct_max = None
        segment_multiplier_min = None
        segment_multiplier_max = None
        
        if index - 1 < len(skill_terms):
            segment_multiplier_min = float(skill_terms_min[index - 1]) * multiplier
            segment_multiplier_max = float(skill_terms_max[index - 1]) * multiplier
            
            if team_power is not None and total_score:
                segment_score_min = segment_multiplier_min * team_power
                segment_score_max = segment_multiplier_max * team_power
                segment_pct_min = segment_score_min / total_score * 100 if total_score else 0.0
                segment_pct_max = segment_score_max / total_score * 100 if total_score else 0.0

        coverage_low = float(row.get("coverage_pct_min", row["coverage_pct"]))
        coverage_high = float(row.get("coverage_pct_max", row["coverage_pct"]))
        coverage_text = format_number_range(coverage_low, coverage_high, digits=2, suffix="%")
        
        # 根據有沒有綜合力，決定要顯示絕對分數還是倍率
        if team_power is not None and segment_score_min is not None:
            score_text = (
                "｜段分數 "
                f"{format_number_range(segment_score_min, segment_score_max, digits=0)} "
                f"({format_number_range(segment_pct_min, segment_pct_max, digits=2, suffix='%')})"
            )
        elif segment_multiplier_min is not None:
            score_text = (
                "｜段分數 "
                f"{format_number_range(segment_multiplier_min, segment_multiplier_max, digits=4, suffix='x')}"
            )
        else:
            score_text = ""
            
        fever_tag = f"｜fever重疊 {float(row.get('fever_overlap_pct') or 0):.1f}%" if fever_weight > 0 else ""
        line = f"S{index} x{multiplier:g}: 覆蓋 {coverage_text}{score_text}{fever_tag}"
        if fever_weight > 0:
            line = f"**{line}**"
        parts.append(line)
        
    return "\n".join(parts) if parts else "沒有技能段資料"


def format_score_rank_line(index: int, row: dict[str, Any], sort_by: str) -> str:
    length_note = " 缺長度" if row.get("length_missing") else ""
    prefix = f"`#{index:02d}` **{row['title']}** {row['difficulty'].upper()} Lv.{row['level']}｜"
    if sort_by == "event_pt":
        pt_text = format_number_range(
            float(row.get("event_pt_min", row["event_pt"])),
            float(row.get("event_pt_max", row["event_pt"])),
            digits=0,
        )
        return f"{prefix}{pt_text}pt{length_note}"
    if sort_by == "score":
        score_text = format_number_range(
            float(row.get("score_min", row["score"])),
            float(row.get("score_max", row["score"])),
            digits=0,
        )
        return f"{prefix}理論分數 {score_text}"
    score_text = ""
    if row.get("score") is not None:
        score_text = "｜理論分數 " + format_number_range(
            float(row.get("score_min", row["score"])),
            float(row.get("score_max", row["score"])),
            digits=0,
        )
    return f"{prefix}技能 {row['skill_coverage_pct_total']:.2f}%{score_text}"


@bot.hybrid_command(name="pjskupdatescores", description="下載/分析全歌曲 SUS，建立技能覆蓋率與理論分數快取")
@app_commands.describe(
    difficulty="只更新特定難度，預設全部",
    force_download="重新下載已快取的 SUS",
    limit="測試用，只處理前 N 張譜，正式更新請留空",
)
@app_commands.choices(difficulty=DIFFICULTY_CHOICES)
async def pjsk_update_scores_command(
    ctx: commands.Context,
    difficulty: str = "all",
    force_download: bool = False,
    limit: Optional[int] = None,
) -> None:
    if not await safe_ctx_defer(ctx):
        return
    selected = None if difficulty == "all" else {difficulty}
    payload = await run_pjsk_score_update(
        reason="discord-command",
        force_download=force_download,
        difficulties=selected,
        limit=limit,
        auto_update_menu=True,
    )
    cache_file = pjsk_score_cache_path(DATA_DIR)
    length_file = pjsk_length_overrides_path(DATA_DIR)
    mismatch_count = sum(1 for chart in payload.get("charts", []) if not chart.get("combo_match"))
    missing_length_count = sum(1 for chart in payload.get("charts", []) if chart.get("length_multiplier") is None)
    await ctx.send(
        "更新完成："
        f"成功 `{payload['chart_count']}` 張譜，失敗 `{payload['error_count']}` 張。"
        f"\nCombo 不一致 `{mismatch_count}` 張，缺長度倍率 `{missing_length_count}` 張。"
        f"\n快取：`{cache_file}`"
        f"\n長度倍率補檔：`{length_file}`"
    )


@bot.hybrid_command(name="pjsklengthfile", description="取得可手動補長度倍率的 CSV 檔")
async def pjsk_length_file_command(ctx: commands.Context) -> None:
    path = pjsk_length_overrides_path(DATA_DIR)
    if not path.exists():
        await ctx.send("還沒有長度倍率補檔，請先跑 `/pjskupdatescores` 產生。")
        return
    await ctx.send(
        "把 `length_multiplier` 欄位填好後，再跑 `/pjskupdatescores` 會自動套用。",
        file=discord.File(path, filename=path.name),
    )


@bot.hybrid_command(name="pjskrank", description="依綜合力/活動倍率/火數計算活動pt排行")
@app_commands.describe(
    power="綜合力；依活動pt/理論分數排行時需要",
    event_multiplier="活動倍率，依活動pt排行時使用，預設 1",
    bonus="bonus 消耗，預設 5 火",
    score_mode="分數模式，多人套 fever 與活躍分，單人不套",
    skill_mode="技能倍率輸入方式",
    skill_multiplier="單一技能倍率，預設 3.7",
    skill1="第 1 段技能倍率，skill_mode=6段分別輸入時使用",
    skill2="第 2 段技能倍率",
    skill3="第 3 段技能倍率",
    skill4="第 4 段技能倍率",
    skill5="第 5 段技能倍率",
    skill6="第 6 段技能倍率",
    difficulty="難度，預設全部",
    start_rank="起始名次，預設 1",
    end_rank="結束名次，最多回傳 50 筆，預設10筆",
    sort_by="排序依據",
)
@app_commands.choices(
    difficulty=DIFFICULTY_CHOICES,
    bonus=BONUS_CHOICES,
    sort_by=PJSK_SCORE_SORT_CHOICES,
    skill_mode=PJSK_SKILL_MODE_CHOICES,
    score_mode=PJSK_SCORE_MODE_CHOICES,
)
async def pjsk_rank_command(
    ctx: commands.Context,
    power: Optional[int] = None,
    event_multiplier: float = 1.0,
    bonus: int = 5,
    score_mode: str = "multi",
    skill_mode: str = "single",
    skill_multiplier: Optional[float] = None,
    skill1: Optional[float] = None,
    skill2: Optional[float] = None,
    skill3: Optional[float] = None,
    skill4: Optional[float] = None,
    skill5: Optional[float] = None,
    skill6: Optional[float] = None,
    difficulty: str = "all",
    start_rank: int = 1,
    end_rank: int = 10,
    sort_by: str = "event_pt",
) -> None:
    if not await safe_ctx_defer(ctx):
        return

    analysis = await load_pjsk_score_cache_or_none_async()
    if not analysis:
        await safe_ctx_send(ctx, "還沒有分析快取，請先跑 `/pjskupdatescores`。")
        return
    if sort_by in {"event_pt", "score"} and power is None:
        await safe_ctx_send(ctx, "依活動pt或理論分數排行需要填 `power`；若只想看覆蓋率，排序請選 `技能覆蓋率`。")
        return
    start_rank = max(1, start_rank)
    end_rank = max(start_rank, end_rank)
    end_rank = min(end_rank, start_rank + 49)
    skill_multipliers = resolve_skill_multipliers(
        skill_mode, skill_multiplier, skill1, skill2, skill3, skill4, skill5, skill6
    )
    active_bonus = active_bonus_power_multiplier_for_mode(score_mode)
    use_fever = score_mode == "multi"
    if power is None:
        rows = []
        for chart in analysis.get("charts", []):
            if difficulty != "all" and chart["difficulty"] != difficulty:
                continue
            skill_total = sum(float(row["covered_weight"]) for row in chart.get("skill_coverages", []))
            rows.append(
                {
                    **chart,
                    "skill_coverage_pct_total": skill_total / chart["total_weight"] * 100
                    if chart["total_weight"]
                    else 0.0,
                    "score": None,
                }
            )
        rows.sort(key=lambda row: row["skill_coverage_pct_total"], reverse=True)
    else:
        rows = rank_pjsk_score_charts(
            analysis,
            team_power=power,
            event_multiplier=event_multiplier,
            bonus=bonus,
            skill_multipliers=skill_multipliers,
            active_bonus_power_multiplier=active_bonus,
            use_fever=use_fever,
            difficulty=difficulty,
            sort_by=sort_by,
    )
    if not rows:
        await safe_ctx_send(ctx, "沒有符合條件的譜面資料。")
        return
    page = rows[start_rank - 1 : end_rank]
    skill_label = "技能 " + "/".join(f"{value:g}" for value in skill_multipliers)
    mode_label = "多人" if score_mode == "multi" else "單人/挑戰"
    power_label = f"{power:,}" if power is not None else "未填綜合力"
    title = f"PJSK 排行 {start_rank}-{start_rank + len(page) - 1}｜{mode_label}｜{power_label}｜活動倍率 {event_multiplier:g}｜{bonus}火｜{skill_label}"
    await send_query_embed(
        ctx,
        title,
        [format_score_rank_line(i, row, sort_by) for i, row in enumerate(page, start=start_rank)],
        "沒有排行資料。",
    )

@bot.hybrid_command(name="pjskchart", description="查詢單曲的技能覆蓋率、理論分數與預測活動pt")
@app_commands.describe(
    song="曲名或歌曲 ID，可輸入關鍵字",
    difficulty="難度",
    power="綜合力；不填則只顯示覆蓋率與分數倍率",
    event_multiplier="活動倍率，預設 1",
    bonus="bonus 消耗，預設 5 火",
    score_mode="分數模式，多人套 fever 與活躍分，單人不套",
    skill_mode="技能倍率輸入方式",
    skill_multiplier="單一技能倍率，預設 3.7",
    skill1="第 1 段技能倍率，skill_mode=6段分別輸入時使用",
    skill2="第 2 段技能倍率",
    skill3="第 3 段技能倍率",
    skill4="第 4 段技能倍率",
    skill5="第 5 段技能倍率",
    skill6="第 6 段技能倍率",
)
@app_commands.choices(
    difficulty=[choice for choice in DIFFICULTY_CHOICES if choice.value != "all"],
    bonus=BONUS_CHOICES,
    skill_mode=PJSK_SKILL_MODE_CHOICES,
    score_mode=PJSK_SCORE_MODE_CHOICES,
)
async def pjsk_chart_command(
    ctx: commands.Context,
    song: str,
    difficulty: str,
    power: Optional[int] = None,
    event_multiplier: float = 1.0,
    bonus: int = 5,
    score_mode: str = "multi",
    skill_mode: str = "single",
    skill_multiplier: Optional[float] = None,
    skill1: Optional[float] = None,
    skill2: Optional[float] = None,
    skill3: Optional[float] = None,
    skill4: Optional[float] = None,
    skill5: Optional[float] = None,
    skill6: Optional[float] = None,
) -> None:
    if not await safe_ctx_defer(ctx):
        return

    analysis = await load_pjsk_score_cache_or_none_async()
    if not analysis:
        await safe_ctx_send(ctx, "還沒有分析快取，請先跑 `/pjskupdatescores`。")
        return
    chart = find_pjsk_score_chart(analysis, song, difficulty)
    if not chart:
        await safe_ctx_send(ctx, "找不到這首歌/難度；可以用歌曲 ID 或更完整的曲名試一次。")
        return
    skill_multipliers = resolve_skill_multipliers(
        skill_mode, skill_multiplier, skill1, skill2, skill3, skill4, skill5, skill6
    )
    active_bonus = active_bonus_power_multiplier_for_mode(score_mode)
    use_fever = score_mode == "multi"
    
    # 不管有沒有填綜合力，都先假定至少為 1 算一次，藉此拿精準的倍率
    dummy_power = power if power is not None else 1
    calc = calculate_event_points(chart, dummy_power, event_multiplier, bonus, skill_multipliers, active_bonus, use_fever)
    
    fever = chart.get("fever", {})
    length_multiplier = calc["length_multiplier"]
    length_text = str(length_multiplier) if length_multiplier is not None else "缺資料，暫用 1.0"
    
    # 根據有沒有填綜合力，決定顯示文字
    if power is not None:
        score_text = format_number_range(
            float(calc.get("score_min", calc["score"])),
            float(calc.get("score_max", calc["score"])),
            digits=0,
        )
        pt_text = format_number_range(
            float(calc.get("event_pt_min", calc["event_pt"])),
            float(calc.get("event_pt_max", calc["event_pt"])),
            digits=0,
        )
        # 刪除了這邊重複的長度倍率
        score_line = f"\n理論分數 `{score_text}`｜預測活動pt `{pt_text}`"
    else:
        score_multiplier_text = format_number_range(
            float(calc.get("score_power_multiplier_min", calc["score_power_multiplier"])),
            float(calc.get("score_power_multiplier_max", calc["score_power_multiplier"])),
            digits=4,
            suffix="x",
        )
        score_line = f"\n理論分數 `{score_multiplier_text} 綜合力`"

    embed = discord.Embed(
        title=f"{chart['title']}｜{difficulty.upper()} Lv.{chart['level']}",
        description=(
            f"Combo `{chart['parsed_combo']}/{chart['official_combo']}`｜"
            f"加權 note `{chart['total_weight']:.1f}`｜長度倍率 `{length_text}`"
            f"{score_line}"
        ),
        color=EMBED_COLOR,
    )
    skill_total = sum(float(row["covered_weight"]) for row in chart.get("skill_coverages", []))
    skill_total_pct = skill_total / chart["total_weight"] * 100 if chart["total_weight"] else 0.0
    mode_text = "多人/協力" if score_mode == "multi" else "單人/挑戰"
    embed.add_field(name="分數模式", value=mode_text, inline=False)
    embed.add_field(name="總技能覆蓋率", value=f"{skill_total_pct:.2f}%", inline=False)
    embed.add_field(name="技能倍率", value="/".join(f"x{value:g}" for value in skill_multipliers), inline=False)
    embed.add_field(
        name="6 段技能覆蓋率 / 段分數",
        value=format_skill_coverages(
            chart,
            skill_multipliers,
            team_power=power,
            total_score=calc["score"] if power is not None else None,
            use_fever=use_fever,
        ),
        inline=False,
    )
    if use_fever:
        embed.add_field(
            name="Fever",
            value=(
                f"combo {fever.get('combo_start')}-{fever.get('combo_end')}｜"
                f"加權覆蓋 {float(fever.get('coverage_pct') or 0):.2f}%"
            ),
            inline=False,
        )
    await safe_ctx_send(ctx, embed=embed)


def normalize_song_query(value: str) -> str:
    return "".join(str(value).split()).lower()


def find_pjsk_score_charts_for_song(analysis: dict[str, Any], query: str) -> list[dict[str, Any]]:
    query_norm = normalize_song_query(query)
    exact_matches = [
        chart
        for chart in analysis.get("charts", [])
        if str(chart.get("music_id")) == str(query) or normalize_song_query(chart.get("title", "")) == query_norm
    ]
    if exact_matches:
        music_id = exact_matches[0]["music_id"]
        matches = [chart for chart in analysis.get("charts", []) if chart.get("music_id") == music_id]
    else:
        partial_matches = [
            chart
            for chart in analysis.get("charts", [])
            if query_norm and query_norm in normalize_song_query(chart.get("title", ""))
        ]
        if not partial_matches:
            return []
        music_id = partial_matches[0]["music_id"]
        matches = [chart for chart in analysis.get("charts", []) if chart.get("music_id") == music_id]

    return sorted(
        matches,
        key=lambda chart: (
            DIFFICULTY_ORDER.index(chart["difficulty"]) if chart.get("difficulty") in DIFFICULTY_ORDER else 99,
            chart.get("level", 0),
        ),
    )


@bot.hybrid_command(name="pjskchartall", description="查詢單曲所有難度的理論分數與預測活動pt")
@app_commands.describe(
    song="曲名或歌曲 ID，可輸入關鍵字",
    power="綜合力；不填則只顯示分數倍率",
    event_multiplier="活動倍率，預設 1",
    bonus="bonus 消耗，預設 5 火",
    score_mode="分數模式，多人套 fever 與活躍分，單人不套",
    skill_mode="技能倍率輸入方式",
    skill_multiplier="單一技能倍率，預設 3.7",
    skill1="第 1 段技能倍率，skill_mode=6段分別輸入時使用",
    skill2="第 2 段技能倍率",
    skill3="第 3 段技能倍率",
    skill4="第 4 段技能倍率",
    skill5="第 5 段技能倍率",
    skill6="第 6 段技能倍率",
)
@app_commands.choices(
    bonus=BONUS_CHOICES,
    skill_mode=PJSK_SKILL_MODE_CHOICES,
    score_mode=PJSK_SCORE_MODE_CHOICES,
)
async def pjsk_chart_all_command(
    ctx: commands.Context,
    song: str,
    power: Optional[int] = None,
    event_multiplier: float = 1.0,
    bonus: int = 5,
    score_mode: str = "multi",
    skill_mode: str = "single",
    skill_multiplier: Optional[float] = None,
    skill1: Optional[float] = None,
    skill2: Optional[float] = None,
    skill3: Optional[float] = None,
    skill4: Optional[float] = None,
    skill5: Optional[float] = None,
    skill6: Optional[float] = None,
) -> None:
    if not await safe_ctx_defer(ctx):
        return

    analysis = await load_pjsk_score_cache_or_none_async()
    if not analysis:
        await safe_ctx_send(ctx, "還沒有分析快取，請先跑 `/pjskupdatescores`。")
        return

    charts = find_pjsk_score_charts_for_song(analysis, song)
    if not charts:
        await safe_ctx_send(ctx, "找不到這首歌；可以用歌曲 ID 或更完整的曲名試一次。")
        return

    skill_multipliers = resolve_skill_multipliers(
        skill_mode, skill_multiplier, skill1, skill2, skill3, skill4, skill5, skill6
    )
    active_bonus = active_bonus_power_multiplier_for_mode(score_mode)
    use_fever = score_mode == "multi"
    dummy_power = power if power is not None else 1
    lines = []
    for chart in charts:
        calc = calculate_event_points(
            chart,
            dummy_power,
            event_multiplier,
            bonus,
            skill_multipliers,
            active_bonus,
            use_fever,
        )
        prefix = f"**{chart['difficulty'].upper()}** Lv.{chart['level']}｜"
        if power is not None:
            score_text = format_number_range(
                float(calc.get("score_min", calc["score"])),
                float(calc.get("score_max", calc["score"])),
                digits=0,
            )
            pt_text = format_number_range(
                float(calc.get("event_pt_min", calc["event_pt"])),
                float(calc.get("event_pt_max", calc["event_pt"])),
                digits=0,
            )
            length_note = "｜缺長度倍率" if calc.get("length_missing") else ""
            lines.append(f"{prefix}理論分數 `{score_text}`｜預測pt `{pt_text}`{length_note}")
        else:
            score_multiplier_text = format_number_range(
                float(calc.get("score_power_multiplier_min", calc["score_power_multiplier"])),
                float(calc.get("score_power_multiplier_max", calc["score_power_multiplier"])),
                digits=4,
                suffix="x",
            )
            lines.append(f"{prefix}理論分數 `{score_multiplier_text} 綜合力`")

    mode_text = "多人/協力" if score_mode == "multi" else "單人/挑戰"
    power_text = f"{power:,}" if power is not None else "未填"
    embed = discord.Embed(
        title=f"{charts[0]['title']}｜全難度",
        description="\n".join(lines),
        color=EMBED_COLOR,
    )
    embed.add_field(name="設定", value=f"{mode_text}｜綜合力 {power_text}｜活動倍率 {event_multiplier:g}｜{bonus}火", inline=False)
    embed.add_field(name="技能倍率", value="/".join(f"x{value:g}" for value in skill_multipliers), inline=False)
    await safe_ctx_send(ctx, embed=embed)


@pjsk_chart_command.autocomplete("song")
async def pjsk_chart_song_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    rows = load_pjsk_score_song_index()
    if not rows:
        return []
    difficulty = getattr(interaction.namespace, "difficulty", None)
    current_norm = (current or "").lower()
    seen: set[int] = set()
    choices: list[app_commands.Choice[str]] = []
    for row in rows:
        row_difficulty = str(row.get("difficulty") or "")
        if difficulty and row_difficulty != difficulty:
            continue
        title = str(row.get("title") or "")
        music_id = int(row.get("music_id") or 0)
        if current_norm and current_norm not in title.lower() and current_norm not in str(music_id):
            continue
        if music_id in seen:
            continue
        seen.add(music_id)
        label = f"{music_id:04d} {title}"
        choices.append(app_commands.Choice(name=label[:100], value=title[:100]))
        if len(choices) >= 25:
            break
    return choices


@pjsk_chart_all_command.autocomplete("song")
async def pjsk_chart_all_song_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    return await pjsk_chart_song_autocomplete(interaction, current)


@bot.hybrid_command(name="help", description="顯示指令列表")
async def help_command(ctx: commands.Context) -> None:
    embed = discord.Embed(
        title="指令列表",
        description=(
            "`/bind id` 綁定玩家 ID\n"
            "`/graph mode` 顯示自己分數走勢\n"
            "`/trackgraph mode rank` 顯示某名次的分數線\n"
            "`/rankgraph mode rank` 顯示目前該名次玩家的歷史分數\n"
            "`/trackrank mode rank` 查指定名次資訊，可輸入 `14,15,16,17,18` 或 `14-18`\n"
            "`/playerrank mode` 查自己目前排名\n"
            "`/line mode` 查活動榜線\n"
            "`/analyzemysekai file` 分析 MySekai JSON，回傳資源/訪客/地圖報表\n"
            "`/uploadsuite file` 上傳並整理 Suite/玩家資料，不立即回傳 zip\n"
            "`/suitemusic mode value` 依難度種類或等級條列歌曲通關狀態圖，會自動分頁全送出\n"
            "`/suiteprofile` 產生個人資料整理圖\n"
            "`/pjskupdatescores` 下載並分析 SUS 分數資料\n"
            "`/pjskrank` 查技能覆蓋/理論分數/活動pt排行\n"
            "`/pjskchart song difficulty` 查單曲分數細節，綜合力可留空\n"
            "`/pjskchartall song` 查單曲全難度分數，綜合力可留空\n"
            "`/pjsklengthfile` 取得可手動補長度倍率的 CSV"
        ),
        color=EMBED_COLOR,
    )
    await ctx.send(embed=embed)


LEGACY_COMMANDS = (
    "analyzeprofile",
    "analyzesuite",
    "mysekairesources",
    "mysekaivisitors",
    "musicstatus",
    "cardstatus",
    "eventhistory",
    "suiteevents",
)
for legacy_command in LEGACY_COMMANDS:
    bot.remove_command(legacy_command)
    bot.tree.remove_command(legacy_command)


class HealthRequestHandler(BaseHTTPRequestHandler):
    def _health_path_ok(self) -> bool:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        return path in ("/", "/healthz")

    def _send_health(self, *, include_body: bool) -> None:
        if not self._health_path_ok():
            self.send_response(404)
            self.end_headers()
            return

        payload = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload) if include_body else 0))
        self.end_headers()
        if not include_body:
            return
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            log.debug("Health check client disconnected before response was written.")

    def do_GET(self) -> None:
        self._send_health(include_body=True)

    def do_HEAD(self) -> None:
        self._send_health(include_body=False)

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("Health check: " + format, *args)


def start_render_health_server() -> None:
    port = os.getenv("PORT")
    if not port:
        return

    try:
        server = ThreadingHTTPServer(("0.0.0.0", int(port)), HealthRequestHandler)
        server.daemon_threads = True
    except ValueError as exc:
        raise RuntimeError(f"PORT 必須是數字，目前是: {port!r}") from exc

    thread = Thread(target=server.serve_forever, name="render-health-server", daemon=True)
    thread.start()
    log.info("Health server listening on 0.0.0.0:%s", port)


def main() -> None:
    load_env_file()
    configure_data_paths()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    start_render_health_server()

    token = os.getenv("DISCORD_TOKEN")
    if token == "replace_with_your_discord_bot_token":
        raise RuntimeError(
            ".env 裡的 DISCORD_TOKEN 還是範例文字，請到 Discord Developer Portal 重新產生 token 後填入。"
        )
    if not token:
        raise RuntimeError(
            "找不到 DISCORD_TOKEN。請在環境變數或 .env 中設定 DISCORD_TOKEN。"
        )

    bot.run(token)


if __name__ == "__main__":
    main()


