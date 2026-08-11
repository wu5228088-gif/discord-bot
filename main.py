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
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlsplit

import discord
import requests
from discord import app_commands
from discord.ext import commands


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("BOT_DATA_DIR") or os.getenv("RENDER_DISK_PATH") or BASE_DIR)
ID_FILE = DATA_DIR / "idfile.json"

TW = timezone(timedelta(hours=8))
REQUEST_CONNECT_TIMEOUT = float(os.getenv("REQUEST_CONNECT_TIMEOUT", "5"))
REQUEST_READ_TIMEOUT = float(os.getenv("REQUEST_READ_TIMEOUT", "10"))
REQUEST_TIMEOUT = (REQUEST_CONNECT_TIMEOUT, REQUEST_READ_TIMEOUT)
EMBED_COLOR = 0x6BFF3D

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
SESSION.headers.update({"User-Agent": "hisekai-discord-bot-lite/1.0"})

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("hisekai_bot_lite")


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
    global DATA_DIR, ID_FILE

    DATA_DIR = Path(os.getenv("BOT_DATA_DIR") or os.getenv("RENDER_DISK_PATH") or BASE_DIR)
    ID_FILE = DATA_DIR / "idfile.json"


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


def load_bound_ids() -> Dict[str, Dict[str, str]]:
    data = load_json(ID_FILE, {})
    return data if isinstance(data, dict) else {}


def save_bound_ids(data: Dict[str, Dict[str, str]]) -> None:
    save_json(ID_FILE, data)


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
    for attempt in range(2):
        try:
            response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
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


def now_text() -> str:
    return datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S")


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
    embed.set_footer(text=f"{title_prefix}｜最後更新於: {now_text()}")
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


@bot.event
async def on_ready() -> None:
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


@bot.hybrid_command(name="help", description="顯示指令列表")
async def help_command(ctx: commands.Context) -> None:
    embed = discord.Embed(
        title="指令列表",
        description=(
            "`/bind id` 綁定玩家 ID\n"
            "`/trackrank mode rank` 查指定名次資訊，可輸入 `14,15,16,17,18` 或 `14-18`\n"
            "`/playerrank mode` 查自己目前排名\n"
            "`/line mode` 查活動榜線"
        ),
        color=EMBED_COLOR,
    )
    await ctx.send(embed=embed)


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



