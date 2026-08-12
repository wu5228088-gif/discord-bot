# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
from typing import Any, Dict, Iterable, List, Optional, Tuple

import discord
import requests
from discord import app_commands
from discord.ext import commands, tasks

from tools.pjsk_score_batch import (
    BONUS_MULTIPLIERS,
    DIFFICULTY_ORDER,
    build_analysis as build_pjsk_score_analysis,
    calculate_event_points,
    cache_path as pjsk_score_cache_path,
    default_master_dir as pjsk_default_master_dir,
    find_chart as find_pjsk_score_chart,
    load_analysis as load_pjsk_score_analysis,
    rank_charts as rank_pjsk_score_charts,
)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("BOT_DATA_DIR") or os.getenv("RENDER_DISK_PATH") or BASE_DIR)
ID_FILE = DATA_DIR / "idfile.json"
DATA_FILE = DATA_DIR / "event_data.json"

TW = timezone(timedelta(hours=8))
REQUEST_CONNECT_TIMEOUT = float(os.getenv("REQUEST_CONNECT_TIMEOUT", "5"))
REQUEST_READ_TIMEOUT = float(os.getenv("REQUEST_READ_TIMEOUT", "10"))
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)
EMBED_COLOR = 0x6BFF3D
MAX_EVENT_HISTORY_SNAPSHOTS = int(os.getenv("MAX_EVENT_HISTORY_SNAPSHOTS", "4320"))

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
    global DATA_DIR, ID_FILE, DATA_FILE

    DATA_DIR = Path(os.getenv("BOT_DATA_DIR") or os.getenv("RENDER_DISK_PATH") or BASE_DIR)
    ID_FILE = DATA_DIR / "idfile.json"
    DATA_FILE = DATA_DIR / "event_data.json"


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
PJSK_MASTER_UPDATE_FILES = ("musics.json", "musicDifficulties.json")
PJSK_MASTER_BASE_URL = os.getenv(
    "PJSK_SCORE_MASTER_BASE_URL",
    "https://sekai-world.github.io/sekai-master-db-diff",
).rstrip("/")


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


def fetch_pjsk_master_update_files() -> dict[str, str]:
    files: dict[str, str] = {}
    for filename in PJSK_MASTER_UPDATE_FILES:
        response = SESSION.get(
            f"{PJSK_MASTER_BASE_URL}/{filename}",
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        files[filename] = response.text
    return files


def apply_pjsk_master_update_if_changed(master_dir: Path, files: dict[str, str]) -> bool:
    master_dir.mkdir(parents=True, exist_ok=True)
    changed = False
    for filename, content in files.items():
        path = master_dir / filename
        old_content = path.read_text(encoding="utf-8") if path.exists() else None
        if old_content == content:
            continue
        path.write_text(content, encoding="utf-8")
        changed = True
    return changed


async def check_pjsk_master_update_once(reason: str) -> bool:
    master_dir = pjsk_default_master_dir()
    files = await asyncio.to_thread(fetch_pjsk_master_update_files)
    changed = await asyncio.to_thread(apply_pjsk_master_update_if_changed, master_dir, files)
    if not changed:
        log.info("PJSK master auto-check found no changes: %s", reason)
        return False

    log.info("PJSK master changed; rebuilding score analysis: %s", reason)
    await run_pjsk_score_update(
        reason=reason,
        force_download=False,
        auto_update_menu=True,
    )
    return True


@tasks.loop(hours=24)
async def pjsk_auto_score_update() -> None:
    if not env_flag("PJSK_AUTO_SCORE_UPDATE", True):
        return

    try:
        await check_pjsk_master_update_once("auto-master-check")
    except requests.exceptions.RequestException as exc:
        log.warning("PJSK master auto-check failed temporarily: %s", exc)
    except Exception:
        log.exception("PJSK master auto-check failed")


@pjsk_auto_score_update.before_loop
async def before_pjsk_auto_score_update() -> None:
    await bot.wait_until_ready()
    interval_hours = float(os.getenv("PJSK_AUTO_SCORE_UPDATE_INTERVAL_HOURS", "24"))
    if interval_hours > 0:
        pjsk_auto_score_update.change_interval(hours=interval_hours)


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
    startup_update_enabled = env_flag("PJSK_STARTUP_SCORE_UPDATE", True)
    if cache_file.exists():
        analysis = await asyncio.to_thread(load_pjsk_score_analysis, DATA_DIR)
        cache_complete = bool(analysis.get("complete", True)) if analysis else False
        if not cache_complete:
            if not startup_update_enabled:
                log.warning(
                    "PJSK score cache is partial; startup auto-resume is disabled by PJSK_STARTUP_SCORE_UPDATE."
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
            "PJSK score cache is missing; startup full SUS analysis is disabled by PJSK_STARTUP_SCORE_UPDATE."
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

    if not pjsk_auto_score_update.is_running():
        pjsk_auto_score_update.start()

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

    original = getattr(error, "original", error)
    log.exception("Command error: %s", error)
    detail = str(original).strip()
    if len(detail) > 180:
        detail = detail[:177] + "..."
    if detail:
        await safe_ctx_send(ctx, f"指令執行時發生錯誤：`{type(original).__name__}` {detail}")
    else:
        await safe_ctx_send(ctx, f"指令執行時發生錯誤：`{type(original).__name__}`")


@bot.hybrid_command(name="bind", description="綁定你的遊戲玩家 ID")
@app_commands.describe(id="遊戲玩家 ID")
async def bind(ctx: commands.Context, id: str) -> None:
    bound_ids = load_bound_ids()
    bound_ids[str(ctx.author.id)] = {"game_id": str(id)}
    save_bound_ids(bound_ids)
    await safe_ctx_send(ctx, f"已綁定玩家 ID：`{id}`")


@bot.hybrid_command(name="trackrank", description="查詢指定名次的即時資訊")
@app_commands.describe(mode="總榜或章節榜", rank="要查詢的名次，可輸入 14,15,16,17,18 或 14-18，最多 5 名")
@app_commands.choices(mode=MODE_CHOICES)
async def trackrank(ctx: commands.Context, mode: str, rank: str) -> None:
    if not await safe_ctx_defer(ctx):
        return
    mode = normalize_mode(mode)

    try:
        top_data = await fetch_top_data()
    except requests.exceptions.RequestException as exc:
        log.warning("HiSekai top100 API request failed: %s", exc)
        await safe_ctx_send(ctx, "目前連不上 HiSekai Top100 API，請稍後再試。")
        return
    rankings = get_rankings(top_data, mode)
    if not rankings:
        await safe_ctx_send(ctx, "目前 API 沒有回傳可用的排名資料。")
        return
    ranks, error_message = parse_rank_query(rank, len(rankings))
    if error_message:
        await safe_ctx_send(ctx, error_message)
        return

    embeds = []
    for target_rank in ranks:
        index = target_rank - 1
        player = rankings[index]
        embeds.append(make_player_embed(player, rankings, index, mode_label(mode, top_data)))

    await safe_ctx_send(ctx, embeds=embeds)


@bot.hybrid_command(name="playerrank", description="查詢已綁定玩家的目前排名")
@app_commands.describe(mode="總榜或章節榜")
@app_commands.choices(mode=MODE_CHOICES)
async def playerrank(ctx: commands.Context, mode: str = "total") -> None:
    if not await safe_ctx_defer(ctx):
        return
    mode = normalize_mode(mode)

    bound_id = load_bound_ids().get(str(ctx.author.id), {}).get("game_id")
    if not bound_id:
        await safe_ctx_send(ctx, "尚未綁定玩家 ID，請先使用 `/bind id`。")
        return

    try:
        top_data = await fetch_top_data()
    except requests.exceptions.RequestException as exc:
        log.warning("HiSekai top100 API request failed: %s", exc)
        await safe_ctx_send(ctx, "目前連不上 HiSekai Top100 API，請稍後再試。")
        return
    rankings = get_rankings(top_data, mode)
    if not rankings:
        await safe_ctx_send(ctx, "目前 API 沒有回傳可用的排名資料。")
        return

    for index, player in enumerate(rankings):
        if player_id(player) == str(bound_id):
            embed = make_player_embed(player, rankings, index, mode_label(mode, top_data))
            await safe_ctx_send(ctx, embed=embed)
            return

    await safe_ctx_send(ctx, "目前 Top 100 內找不到這個玩家。")


@bot.hybrid_command(name="line", description="查詢目前活動榜線")
@app_commands.describe(mode="總榜或章節榜")
@app_commands.choices(mode=MODE_CHOICES)
async def line(ctx: commands.Context, mode: str = "total") -> None:
    if not await safe_ctx_defer(ctx):
        return
    mode = normalize_mode(mode)

    try:
        top_data, border_data = await asyncio.gather(fetch_top_data(), fetch_border_data())
    except requests.exceptions.RequestException as exc:
        log.warning("HiSekai border API request failed: %s", exc)
        await safe_ctx_send(ctx, "目前連不上 HiSekai 榜線 API，請稍後再試。")
        return
    embed = rank_line_embed(border_data, top_data, mode)
    await safe_ctx_send(ctx, embed=embed)


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
        await safe_ctx_send(ctx, "還沒有 PJSK 分析快取；這份程式預設會在啟動時自動建立，請稍等更新完成後再試。")
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
        await safe_ctx_send(ctx, "還沒有 PJSK 分析快取；這份程式預設會在啟動時自動建立，請稍等更新完成後再試。")
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
        await safe_ctx_send(ctx, "還沒有 PJSK 分析快取；這份程式預設會在啟動時自動建立，請稍等更新完成後再試。")
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
            "`/trackrank mode rank` 查指定名次資訊，可輸入 `14,15,16,17,18` 或 `14-18`\n"
            "`/playerrank mode` 查自己目前排名\n"
            "`/line mode` 查活動榜線\n"
            "`/pjskrank` 查技能覆蓋/理論分數/活動pt排行\n"
            "`/pjskchart song difficulty` 查單曲分數細節，綜合力可留空\n"
            "`/pjskchartall song` 查單曲全難度分數，綜合力可留空"
        ),
        color=EMBED_COLOR,
    )
    await safe_ctx_send(ctx, embed=embed)


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




