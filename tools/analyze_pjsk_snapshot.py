#!/usr/bin/env python3
"""Build readable reports from decoded Project SEKAI snapshots."""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MASTER_CACHE = PROJECT_ROOT / "master_cache"
GRAPH_FONT_FILE = PROJECT_ROOT / "NotoSansTC-Bold.ttf"
MYSEKAI_MAP_ASSET_DIR = PROJECT_ROOT / "assets" / "mysekai_maps"
MYSEKAI_RESOURCE_MAP_TRANSFORM_FILE = MYSEKAI_MAP_ASSET_DIR / "mysekai_resource_map_transforms.json"
ASSET_CACHE_DIR = PROJECT_ROOT / "assets" / "cache"
SEKAI_ASSET_BASE = "https://storage.sekai.best/sekai-jp-assets"
SEKAI_VIEWER_RAW_BASE = "https://raw.githubusercontent.com/Sekai-World/sekai-viewer/dev/src/assets"
TW = timezone(timedelta(hours=8))

DIFFICULTY_ORDER = {
    "easy": 0,
    "normal": 1,
    "hard": 2,
    "expert": 3,
    "master": 4,
    "append": 5,
}

DIFFICULTY_LABELS = {
    "easy": "Easy",
    "normal": "Normal",
    "hard": "Hard",
    "expert": "Expert",
    "master": "Master",
    "append": "Append",
}

DIFFICULTY_COLORS = {
    "Easy": "#19cf74",
    "Normal": "#35c9df",
    "Hard": "#f0c51f",
    "Expert": "#ef3f86",
    "Master": "#b934ef",
    "Append": "#ef75dc",
}

RESULT_PRIORITY = {
    "not_played": 0,
    "not_clear": 1,
    "clear": 2,
    "full_combo": 3,
    "full_perfect": 4,
}

RESULT_LABELS = {
    "not_played": "未記錄",
    "not_clear": "未通關",
    "clear": "Clear",
    "full_combo": "Full Combo",
    "full_perfect": "All Perfect",
}

RARITY_LABELS = {
    "rarity_1": "1★",
    "rarity_2": "2★",
    "rarity_3": "3★",
    "rarity_4": "4★",
    "rarity_birthday": "Birthday",
}

CARD_ROW_FIELDS = [
    "cardId",
    "level",
    "exp",
    "totalExp",
    "skillLevel",
    "skillExpUnknown",
    "skillExp",
    "masterRank",
    "specialTrainingStatus",
    "defaultImage",
    "duplicateCount",
    "obtainedAt",
    "episodes",
]

DEFAULT_CURRENT_RANK_URLS = [
    "https://api.hisekai.org/{server}/event/live/top100",
]

DEFAULT_HISTORY_URLS = [
    "https://api.hisekai.org/{server}/user/{user_id}/profile",
]

DEFAULT_EVENT_LIST_URL = "https://api.hisekai.org/{server}/event/list"
DEFAULT_EVENT_TOP100_URL = "https://api.hisekai.org/{server}/event/{event_id}/top100"
MASTER_DB_BASE_URL = "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-tc-diff/main"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def ms_to_iso(value: Any) -> str:
    if not isinstance(value, int) or value <= 1_000_000_000_000:
        return ""
    return datetime.fromtimestamp(value / 1000, tz=TW).isoformat(timespec="seconds")


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def slug(value: str, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return text or fallback


def ensure_output_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_matplotlib_font() -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)
    import matplotlib.font_manager as fm
    import matplotlib.pyplot as plt

    fallback_fonts = [
        "Microsoft JhengHei",
        "Microsoft YaHei",
        "SimSun",
        "Noto Sans CJK JP",
        "Noto Sans JP",
        "Yu Gothic",
        "Yu Gothic UI",
        "Meiryo",
        "MS Gothic",
        "Noto Sans CJK TC",
        "Noto Sans TC",
        "Arial Unicode MS",
    ]
    if GRAPH_FONT_FILE.exists():
        fm.fontManager.addfont(str(GRAPH_FONT_FILE))
        font_name = fm.FontProperties(fname=str(GRAPH_FONT_FILE)).get_name()
        plt.rcParams["font.family"] = "sans-serif"
        plt.rcParams["font.sans-serif"] = [*fallback_fonts, font_name]
    else:
        plt.rcParams["font.family"] = "sans-serif"
        plt.rcParams["font.sans-serif"] = fallback_fonts
    plt.rcParams["axes.unicode_minus"] = False


class MasterCache:
    def __init__(self, cache_dir: Path, locale: str) -> None:
        self.base = cache_dir / locale
        self._raw: dict[str, list[dict[str, Any]]] = {}
        self._by_id: dict[str, dict[int, dict[str, Any]]] = {}

    def raw(self, table: str) -> list[dict[str, Any]]:
        if table in self._raw:
            return self._raw[table]
        path = self.base / f"{table}.json"
        if not path.exists():
            self.download_table(table, path)
        if not path.exists():
            self._raw[table] = []
            return []
        data = read_json(path)
        if not isinstance(data, list):
            data = []
        self._raw[table] = [item for item in data if isinstance(item, dict)]
        return self._raw[table]

    def download_table(self, table: str, path: Path) -> None:
        url = f"{MASTER_DB_BASE_URL}/{table}.json"
        request = urllib.request.Request(url, headers={"User-Agent": "pjsk-snapshot-analyzer/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                content = response.read()
        except Exception:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def by_id(self, table: str) -> dict[int, dict[str, Any]]:
        if table in self._by_id:
            return self._by_id[table]
        indexed: dict[int, dict[str, Any]] = {}
        for row in self.raw(table):
            row_id = row.get("id")
            if isinstance(row_id, int):
                indexed[row_id] = row
            if table == "characterProfiles" and isinstance(row.get("characterId"), int):
                indexed[row["characterId"]] = row
        self._by_id[table] = indexed
        return indexed

    def row(self, table: str, row_id: Any) -> dict[str, Any]:
        if not isinstance(row_id, int):
            return {}
        return self.by_id(table).get(row_id, {})

    def label(self, table: str, row_id: Any, fallback: str = "") -> str:
        row = self.row(table, row_id)
        if not row:
            return fallback or (f"id {row_id}" if row_id not in (None, "") else "")

        if table == "gameCharacters":
            return f"{row.get('firstName', '')}{row.get('givenName', '')}".strip() or fallback
        if table == "cards":
            return safe_text(row.get("prefix") or row.get("cardSkillName") or row.get("assetbundleName") or fallback)
        if table == "musics":
            return safe_text(row.get("title") or row.get("assetbundleName") or fallback)
        if table in {"mysekaiMaterials", "mysekaiItems", "mysekaiSites", "events"}:
            return safe_text(row.get("name") or row.get("title") or row.get("assetbundleName") or fallback)
        if table == "honors":
            return safe_text(row.get("name") or fallback)
        return safe_text(row.get("name") or row.get("title") or row.get("assetbundleName") or fallback)


def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{
  color-scheme: light;
  --ink: #172026;
  --muted: #62727d;
  --line: #d9e1e5;
  --panel: #f7fafb;
  --accent: #0f7f87;
  --accent-2: #b34b6f;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: "Noto Sans TC", "Microsoft JhengHei", system-ui, sans-serif;
  color: var(--ink);
  background: #fff;
  line-height: 1.55;
}}
main {{ width: min(1180px, calc(100% - 32px)); margin: 28px auto 56px; }}
h1 {{ font-size: 28px; margin: 0 0 18px; }}
h2 {{ font-size: 20px; margin: 34px 0 12px; border-bottom: 1px solid var(--line); padding-bottom: 8px; }}
h3 {{ font-size: 16px; margin: 22px 0 10px; }}
.meta, .note {{ color: var(--muted); }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin: 16px 0; }}
.stat {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; }}
.stat b {{ display: block; font-size: 22px; color: var(--accent); }}
table {{ width: 100%; border-collapse: collapse; margin: 10px 0 18px; font-size: 14px; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 8px 10px; text-align: left; vertical-align: top; }}
th {{ background: var(--panel); font-weight: 700; }}
img {{ max-width: 100%; height: auto; border: 1px solid var(--line); border-radius: 8px; }}
.gallery {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 18px; }}
a {{ color: var(--accent-2); }}
code {{ background: var(--panel); padding: 2px 5px; border-radius: 4px; }}
</style>
</head>
<body><main>
{body}
</main></body>
</html>
"""


def render_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]], limit: int | None = None) -> str:
    shown = rows[:limit] if limit else rows
    head = "".join(f"<th>{html.escape(label)}</th>" for key, label in columns)
    body_parts = []
    for row in shown:
        cells = "".join(f"<td>{html.escape(safe_text(row.get(key, '')))}</td>" for key, label in columns)
        body_parts.append(f"<tr>{cells}</tr>")
    suffix = ""
    if limit and len(rows) > limit:
        suffix = f'<p class="note">另有 {len(rows) - limit} 筆，請看 CSV/JSON。</p>'
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body_parts)}</tbody></table>{suffix}"


def compact_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("rows"), list):
        return [row for row in value["rows"] if isinstance(row, dict)]
    if not isinstance(value, dict) or "__ENUM__" not in value:
        return []

    columns = [key for key, item in value.items() if key != "__ENUM__" and isinstance(item, list)]
    if not columns:
        return []
    row_count = max(len(value[column]) for column in columns)
    enums = value.get("__ENUM__", {})
    rows: list[dict[str, Any]] = []
    for index in range(row_count):
        row: dict[str, Any] = {}
        for column in columns:
            items = value[column]
            item = items[index] if index < len(items) else None
            row[column] = item
            enum_values = enums.get(column)
            if isinstance(enum_values, list) and isinstance(item, int) and 0 <= item < len(enum_values):
                row[f"{column}_label"] = enum_values[item]
        rows.append(row)
    return rows


def find_harvest_maps(data: dict[str, Any]) -> list[dict[str, Any]]:
    resources = data.get("updatedResources")
    if isinstance(resources, dict) and isinstance(resources.get("userMysekaiHarvestMaps"), list):
        return [item for item in resources["userMysekaiHarvestMaps"] if isinstance(item, dict)]
    if isinstance(data.get("userMysekaiHarvestMaps"), list):
        return [item for item in data["userMysekaiHarvestMaps"] if isinstance(item, dict)]
    return []


def resource_label(resource_type: str, resource_id: Any, master: MasterCache) -> str:
    if resource_type == "mysekai_material":
        return master.label("mysekaiMaterials", resource_id)
    if resource_type == "mysekai_item":
        return master.label("mysekaiItems", resource_id)
    if resource_type == "mysekai_fixture":
        return master.label("mysekaiFixtures", resource_id)
    if resource_type == "mysekai_music_record":
        return master.label("musics", resource_id)
    if resource_type == "material":
        return master.label("materials", resource_id)
    return f"{resource_type}:{resource_id}"


def visitor_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    visit = data.get("userMysekaiGateCharacterVisit")
    if not isinstance(visit, dict):
        return []
    rows = []
    for item in visit.get("userMysekaiGateCharacters", []):
        if not isinstance(item, dict):
            continue
        detail = item.get("mysekaiGameCharacterUnitGroupId_detail")
        characters = detail.get("characters") if isinstance(detail, dict) else None
        rows.append(
            {
                "gate": item.get("mysekaiGateId_label") or item.get("mysekaiGateId"),
                "groupId": item.get("mysekaiGameCharacterUnitGroupId"),
                "visitor": item.get("mysekaiGameCharacterUnitGroupId_label") or (detail or {}).get("label"),
                "characters": " + ".join(characters) if isinstance(characters, list) else "",
                "visitCount": item.get("visitCount", 0),
                "isReservation": item.get("isReservation", False),
            }
        )
    rows.sort(key=lambda row: int(row.get("visitCount") or 0), reverse=True)
    return rows


def character_ids_for_group(group_id: Any, master: MasterCache) -> list[int]:
    group = master.row("mysekaiGameCharacterUnitGroups", group_id)
    character_ids: list[int] = []
    if not group:
        return character_ids

    for key in sorted(k for k in group if k.startswith("gameCharacterUnitId")):
        unit_id = group.get(key)
        unit = master.row("gameCharacterUnits", unit_id)
        character_id = unit.get("gameCharacterId")
        if isinstance(character_id, int) and character_id not in character_ids:
            character_ids.append(character_id)
    return character_ids


def cached_download(url: str, cache_name: str) -> Path | None:
    path = ASSET_CACHE_DIR / cache_name
    if path.exists() and path.stat().st_size > 0:
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "pjsk-snapshot-analyzer/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            path.write_bytes(response.read())
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return None
    return path if path.exists() and path.stat().st_size > 0 else None


def sekai_asset_path(relative_path: str) -> Path | None:
    return cached_download(
        f"{SEKAI_ASSET_BASE}/{relative_path}",
        str(Path("sekai-jp-assets") / relative_path),
    )


def material_icon_path(resource_type: str, resource_id: Any, master: MasterCache) -> Path | None:
    if resource_type == "mysekai_material":
        row = master.row("mysekaiMaterials", resource_id)
        bundle = row.get("iconAssetbundleName")
        folder = "material"
    elif resource_type == "mysekai_item":
        row = master.row("mysekaiItems", resource_id)
        bundle = row.get("iconAssetbundleName")
        folder = "item"
    elif resource_type == "mysekai_fixture":
        row = master.row("mysekaiFixtures", resource_id)
        bundle = row.get("assetbundleName")
        if not bundle:
            return None
        return (
            sekai_asset_path(f"mysekai/thumbnail/fixture/{bundle}.png")
            or sekai_asset_path(f"mysekai/thumbnail/fixture/{bundle}_1.png")
        )
    elif resource_type == "mysekai_music_record":
        return music_jacket_path(resource_id, master)
    else:
        return None

    if not bundle:
        return None
    return sekai_asset_path(f"mysekai/thumbnail/{folder}/{bundle}.png")


def music_jacket_path(music_id_or_bundle: Any, master: MasterCache | None = None) -> Path | None:
    bundle = ""
    if isinstance(music_id_or_bundle, str) and music_id_or_bundle.startswith("jacket_"):
        bundle = music_id_or_bundle
    elif master is not None:
        row = master.row("musics", music_id_or_bundle)
        bundle = safe_text(row.get("assetbundleName"))
    if not bundle:
        return None
    return sekai_asset_path(f"music/jacket/{bundle}/{bundle}.png")


def character_icon_path(character_id: int) -> Path | None:
    return cached_download(
        f"{SEKAI_VIEWER_RAW_BASE}/chara_icons/chr_ts_{character_id}.png",
        str(Path("sekai-viewer") / "chara_icons" / f"chr_ts_{character_id}.png"),
    )


def mysekai_harvest_background_path(site_id: Any) -> Path | None:
    if site_id in (None, ""):
        return None
    return sekai_asset_path(f"mysekai/site/sitemap/texture/img_harvest_site_{site_id}.png")


def add_icon(ax: Any, path: Path | None, x: float, y: float, zoom: float = 0.18, fallback: str = "") -> None:
    if path and path.exists():
        try:
            from matplotlib.offsetbox import AnnotationBbox, OffsetImage
            import matplotlib.pyplot as plt

            image = plt.imread(path)
            max_dim = max(image.shape[0], image.shape[1]) if getattr(image, "shape", None) is not None else 128
            normalized_zoom = zoom * min(1.0, 128 / max_dim)
            box = OffsetImage(image, zoom=normalized_zoom)
            ax.add_artist(AnnotationBbox(box, (x, y), frameon=False, pad=0))
            return
        except Exception:
            pass

    ax.scatter([x], [y], s=520, color="#f1f6f7", edgecolors="#0f7f87", linewidths=1.2, zorder=4)
    if fallback:
        ax.text(x, y, fallback[:2], ha="center", va="center", fontsize=8, weight="bold", color="#0f4f56", zorder=5)


def collect_resource_totals(resource_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, Any, str, str], int] = defaultdict(int)
    for row in resource_rows:
        key = (
            safe_text(row.get("resourceType")),
            row.get("resourceId"),
            safe_text(row.get("resourceName")),
            safe_text(row.get("siteName")),
        )
        totals[key] += int(row.get("quantity") or 0)

    combined: dict[tuple[str, Any, str], dict[str, Any]] = {}
    for (resource_type, resource_id, resource_name, site_name), quantity in totals.items():
        key = (resource_type, resource_id, resource_name)
        item = combined.setdefault(
            key,
            {
                "resourceType": resource_type,
                "resourceId": resource_id,
                "resourceName": resource_name,
                "quantity": 0,
                "sites": [],
            },
        )
        item["quantity"] += quantity
        item["sites"].append(f"{site_name}:{quantity}")

    rows = list(combined.values())
    rows.sort(key=lambda row: int(row.get("quantity") or 0), reverse=True)
    return rows


def analyze_harvest_maps(
    maps: list[dict[str, Any]],
    master: MasterCache,
    output_dir: Path,
    draw_maps: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    site_rows: list[dict[str, Any]] = []
    resource_rows: list[dict[str, Any]] = []
    image_names: list[str] = []

    for site in maps:
        site_id = site.get("mysekaiSiteId")
        site_name = safe_text(site.get("mysekaiSiteId_label") or master.label("mysekaiSites", site_id, f"site {site_id}"))
        fixtures = [item for item in site.get("userMysekaiSiteHarvestFixtures", []) if isinstance(item, dict)]
        drops = [item for item in site.get("userMysekaiSiteHarvestResourceDrops", []) if isinstance(item, dict)]

        fixture_counts = Counter(item.get("mysekaiSiteHarvestFixtureId") for item in fixtures)
        status_counts = Counter(item.get("userMysekaiSiteHarvestFixtureStatus") for item in fixtures)
        alive_count = sum(1 for item in fixtures if isinstance(item.get("hp"), int) and item["hp"] > 0)
        depleted_count = sum(1 for item in fixtures if item.get("hp") == 0)

        site_rows.append(
            {
                "siteId": site_id,
                "siteName": site_name,
                "fixtureCount": len(fixtures),
                "aliveCount": alive_count,
                "depletedCount": depleted_count,
                "dropCount": len(drops),
                "fixtureTypes": ", ".join(f"{key}:{count}" for key, count in fixture_counts.most_common()),
                "statuses": ", ".join(f"{key}:{count}" for key, count in status_counts.most_common()),
            }
        )

        resource_counter: Counter[tuple[str, Any, str]] = Counter()
        for drop in drops:
            resource_type = safe_text(drop.get("resourceType"))
            resource_id = drop.get("resourceId")
            label = resource_label(resource_type, resource_id, master)
            quantity = drop.get("quantity")
            resource_counter[(resource_type, resource_id, label)] += int(quantity) if isinstance(quantity, int) else 1
        for (resource_type, resource_id, label), quantity in resource_counter.most_common():
            resource_rows.append(
                {
                    "siteId": site_id,
                    "siteName": site_name,
                    "resourceType": resource_type,
                    "resourceId": resource_id,
                    "resourceName": label,
                    "quantity": quantity,
                }
            )

        if draw_maps:
            image_names.append(draw_harvest_map(site_id, site_name, fixtures, drops, output_dir))

    return site_rows, resource_rows, image_names


def draw_harvest_map(
    site_id: Any,
    site_name: str,
    fixtures: list[dict[str, Any]],
    drops: list[dict[str, Any]],
    output_dir: Path,
) -> str:
    import matplotlib.pyplot as plt

    setup_matplotlib_font()
    image_name = f"mysekai_site_{site_id}_{slug(site_name, 'site')}.png"
    path = output_dir / image_name

    fig, ax = plt.subplots(figsize=(8.5, 8.5), dpi=150)
    ax.set_title(f"{site_name} harvest map", fontsize=14, weight="bold")
    ax.set_xlabel("positionX")
    ax.set_ylabel("positionZ")
    ax.set_aspect("equal", adjustable="box")

    xs_all = [item.get("positionX", 0) for item in fixtures]
    zs_all = [item.get("positionZ", 0) for item in fixtures]
    if not xs_all:
        xs_all = [item.get("positionX", 0) for item in drops]
        zs_all = [item.get("positionZ", 0) for item in drops]

    margin = 3
    if xs_all and zs_all:
        x_min, x_max = min(xs_all) - margin, max(xs_all) + margin
        z_min, z_max = min(zs_all) - margin, max(zs_all) + margin
    else:
        x_min, x_max, z_min, z_max = -10, 10, -10, 10

    background_path = find_mysekai_map_background(site_id, site_name)
    if background_path:
        image = plt.imread(background_path)
        ax.imshow(image, extent=[x_min, x_max, z_min, z_max], origin="lower", alpha=0.9)
        ax.set_facecolor("#eef4f0")
    else:
        ax.grid(True, color="#d8e0e5", linewidth=0.7)

    fixture_ids = sorted({item.get("mysekaiSiteHarvestFixtureId") for item in fixtures}, key=lambda x: str(x))
    palette = plt.get_cmap("tab20", max(len(fixture_ids), 1))
    colors = {fixture_id: palette(index % palette.N) for index, fixture_id in enumerate(fixture_ids)}

    for fixture_id in fixture_ids:
        group = [item for item in fixtures if item.get("mysekaiSiteHarvestFixtureId") == fixture_id]
        xs = [item.get("positionX", 0) for item in group]
        zs = [item.get("positionZ", 0) for item in group]
        sizes = [max(45, min(150, int(item.get("hp") or 0) + 45)) for item in group]
        edge_colors = ["#2f3a40" if (item.get("hp") or 0) > 0 else "#a6b0b7" for item in group]
        ax.scatter(
            xs,
            zs,
            s=sizes,
            c=[colors[fixture_id]],
            edgecolors=edge_colors,
            linewidths=0.8,
            alpha=0.88,
            label=f"fixture {fixture_id}",
        )
        for item in group:
            ax.text(
                item.get("positionX", 0),
                item.get("positionZ", 0),
                safe_text(fixture_id),
                fontsize=6.5,
                ha="center",
                va="center",
                color="#111",
            )

    drop_counts = Counter((item.get("positionX"), item.get("positionZ")) for item in drops)
    for (x, z), count in drop_counts.items():
        ax.text(
            x + 0.35,
            z + 0.35,
            f"+{count}",
            fontsize=7,
            ha="left",
            va="bottom",
            color="#b34b6f",
            weight="bold",
        )

    ax.set_xlim(x_min, x_max)
    ax.set_ylim(z_min, z_max)

    handles, labels = ax.get_legend_handles_labels()
    if handles:
        ax.legend(
            handles[:12],
            labels[:12],
            loc="upper right",
            fontsize=7,
            frameon=True,
            title="first 12 fixture IDs",
            title_fontsize=7,
        )

    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return image_name


def find_mysekai_map_background(site_id: Any, site_name: str) -> Path | None:
    if site_id in (None, ""):
        return None

    site_slug = slug(site_name, "site")
    candidates = [
        f"topdown_site_{site_id}.png",
        f"topdown_{site_id}.png",
        f"mysekai_site_{site_id}.png",
        f"site_{site_id}.png",
        f"{site_id}.png",
        f"{site_slug}.png",
        f"topdown_site_{site_id}.jpg",
        f"topdown_{site_id}.jpg",
        f"mysekai_site_{site_id}.jpg",
        f"site_{site_id}.jpg",
        f"{site_id}.jpg",
        f"{site_slug}.jpg",
    ]
    for name in candidates:
        path = MYSEKAI_MAP_ASSET_DIR / name
        if path.exists():
            return path
    return None


def find_combined_mysekai_resource_background() -> Path | None:
    for name in (
        "mysekai_resource_base.webp",
        "mysekai_resource_base.png",
        "mysekai_resource_base.jpg",
        "mysekai_resource_map_base.webp",
        "mysekai_resource_map_base.png",
        "mysekai_resource_map_base.jpg",
    ):
        path = MYSEKAI_MAP_ASSET_DIR / name
        if path.exists():
            return path
    return None


def mysekai_site_quadrant(site_id: Any, site_name: str) -> tuple[float, float, float, float] | None:
    try:
        numeric_id = int(site_id)
    except (TypeError, ValueError):
        numeric_id = None
    by_id = {
        5: (0.0, 1.0, 1.0, 2.0),  # 最初的草地: left top
        7: (0.0, 0.0, 1.0, 1.0),  # 繽紛的花田: left bottom
        6: (1.0, 1.0, 2.0, 2.0),  # 心願的沙灘: right top
        8: (1.0, 0.0, 2.0, 1.0),  # 被遺忘之地: right bottom
    }
    if numeric_id in by_id:
        return by_id[numeric_id]
    if "最初" in site_name or "草地" in site_name:
        return by_id[5]
    if "花田" in site_name:
        return by_id[7]
    if "沙灘" in site_name:
        return by_id[6]
    if "遺忘" in site_name:
        return by_id[8]
    return None


def mysekai_position_bounds(
    fixtures: list[dict[str, Any]],
    drops: list[dict[str, Any]],
    margin: float = 4.0,
) -> tuple[float, float, float, float]:
    xs = [item.get("positionX", 0) for item in fixtures] or [item.get("positionX", 0) for item in drops]
    zs = [item.get("positionZ", 0) for item in fixtures] or [item.get("positionZ", 0) for item in drops]
    if not xs or not zs:
        return -25.0, 25.0, -25.0, 25.0
    return float(min(xs) - margin), float(max(xs) + margin), float(min(zs) - margin), float(max(zs) + margin)


DEFAULT_MYSEKAI_RESOURCE_MAP_TRANSFORMS: dict[int, dict[str, Any]] = {
    # The combined Resona map uses a different orientation than the raw
    # harvest coordinate plane for the two left-side sites.
    5: {"rotate_ccw": True},
    7: {"rotate_ccw": True},
    # The right-side sites cover less of their rendered quadrants than their
    # raw min/max bounds imply, so keep the points closer to the site center.
    6: {"scale": 0.72},
    8: {"scale": 0.74},
}
_mysekai_resource_map_transforms_cache: dict[int, dict[str, Any]] | None = None
_mysekai_resource_map_transforms_mtime_ns: int | None = None
_mysekai_resource_map_transforms_error: str | None = None


def load_mysekai_resource_map_transforms() -> dict[int, dict[str, Any]]:
    global _mysekai_resource_map_transforms_cache, _mysekai_resource_map_transforms_error, _mysekai_resource_map_transforms_mtime_ns
    try:
        current_mtime_ns = MYSEKAI_RESOURCE_MAP_TRANSFORM_FILE.stat().st_mtime_ns
    except OSError:
        current_mtime_ns = None
    if (
        _mysekai_resource_map_transforms_cache is not None
        and _mysekai_resource_map_transforms_mtime_ns == current_mtime_ns
    ):
        return _mysekai_resource_map_transforms_cache

    transforms = {site_id: dict(config) for site_id, config in DEFAULT_MYSEKAI_RESOURCE_MAP_TRANSFORMS.items()}
    _mysekai_resource_map_transforms_error = None
    if MYSEKAI_RESOURCE_MAP_TRANSFORM_FILE.exists():
        try:
            with MYSEKAI_RESOURCE_MAP_TRANSFORM_FILE.open("r", encoding="utf-8") as handle:
                raw_config = json.load(handle)
            site_config = raw_config.get("sites", raw_config) if isinstance(raw_config, dict) else {}
            if isinstance(site_config, dict):
                for raw_site_id, config in site_config.items():
                    if not isinstance(config, dict):
                        continue
                    try:
                        site_id = int(raw_site_id)
                    except (TypeError, ValueError):
                        continue
                    transforms[site_id] = {**transforms.get(site_id, {}), **config}
        except (OSError, json.JSONDecodeError) as exc:
            _mysekai_resource_map_transforms_error = f"{type(exc).__name__}: {exc}"

    _mysekai_resource_map_transforms_cache = transforms
    _mysekai_resource_map_transforms_mtime_ns = current_mtime_ns
    return transforms


def write_mysekai_resource_map_transform_debug(output_dir: Path) -> None:
    transforms = load_mysekai_resource_map_transforms()
    try:
        stat = MYSEKAI_RESOURCE_MAP_TRANSFORM_FILE.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, TW).isoformat()
        mtime_ns = stat.st_mtime_ns
        exists = True
    except OSError:
        mtime = None
        mtime_ns = None
        exists = False
    payload = {
        "path": str(MYSEKAI_RESOURCE_MAP_TRANSFORM_FILE),
        "exists": exists,
        "mtime": mtime,
        "mtimeNs": mtime_ns,
        "error": _mysekai_resource_map_transforms_error,
        "transforms": {str(site_id): config for site_id, config in sorted(transforms.items())},
    }
    try:
        with (output_dir / "mysekai_resource_map_transforms_used.json").open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def mysekai_resource_map_transform(site_id: Any) -> dict[str, Any]:
    try:
        numeric_id = int(site_id)
    except (TypeError, ValueError):
        return {}
    return load_mysekai_resource_map_transforms().get(numeric_id, {})


def normalized_pair(value: Any, fallback: tuple[float, float]) -> tuple[float, float]:
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return float(value[0]), float(value[1])
        except (TypeError, ValueError):
            return fallback
    return fallback


def float_config(value: Any, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def transform_mysekai_position(
    x: Any,
    z: Any,
    bounds: tuple[float, float, float, float],
    quadrant: tuple[float, float, float, float],
    site_id: Any = None,
) -> tuple[float, float]:
    x_min, x_max, z_min, z_max = bounds
    qx0, qy0, qx1, qy1 = quadrant
    width = max(x_max - x_min, 1.0)
    height = max(z_max - z_min, 1.0)
    nx = (float(x) - x_min) / width
    nz = (float(z) - z_min) / height
    transform = mysekai_resource_map_transform(site_id)
    if transform.get("rotate_ccw"):
        nx, nz = 1.0 - nz, nx
    center_x, center_y = normalized_pair(transform.get("center"), (0.5, 0.5))
    center_x = float_config(transform.get("centerX"), center_x)
    center_y = float_config(transform.get("centerY"), center_y)
    scale = float_config(transform.get("scale"), 1.0)
    scale_x = float_config(transform.get("scaleX"), scale)
    scale_y = float_config(transform.get("scaleY"), scale)
    nx = center_x + (nx - center_x) * scale_x
    nz = center_y + (nz - center_y) * scale_y
    offset_x, offset_y = normalized_pair(transform.get("offset"), (0.0, 0.0))
    offset_x = float_config(transform.get("offsetX"), offset_x)
    offset_y = float_config(transform.get("offsetY"), offset_y)
    nx += offset_x
    nz += offset_y
    return qx0 + nx * (qx1 - qx0), qy0 + nz * (qy1 - qy0)


def draw_material_summary(resource_rows: list[dict[str, Any]], output_dir: Path) -> str:
    if not resource_rows:
        return ""

    import matplotlib.pyplot as plt

    setup_matplotlib_font()
    image_name = "mysekai_materials_by_site.png"
    path = output_dir / image_name

    site_names: list[str] = []
    by_site: defaultdict[str, Counter[str]] = defaultdict(Counter)
    totals: Counter[str] = Counter()

    for row in resource_rows:
        site_name = safe_text(row.get("siteName") or row.get("siteId") or "unknown")
        resource_name = safe_text(row.get("resourceName") or row.get("resourceId") or "unknown")
        try:
            quantity = int(row.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if site_name not in site_names:
            site_names.append(site_name)
        by_site[site_name][resource_name] += quantity
        totals[resource_name] += quantity

    top_resources = [name for name, _ in totals.most_common(10)]
    has_other = any(name not in top_resources for name in totals)
    categories = top_resources + (["其他"] if has_other else [])
    colors = [
        "#0f7f87",
        "#b34b6f",
        "#d88a30",
        "#5d6f7d",
        "#4b8e4f",
        "#8c6bb1",
        "#c2563d",
        "#2f6f9f",
        "#6a7b3e",
        "#a56c32",
        "#9aa4aa",
    ]

    fig, ax = plt.subplots(figsize=(10.5, 5.2), dpi=150)
    bottoms = [0] * len(site_names)
    for index, category in enumerate(categories):
        values: list[int] = []
        for site_name in site_names:
            if category == "其他":
                values.append(sum(count for name, count in by_site[site_name].items() if name not in top_resources))
            else:
                values.append(by_site[site_name][category])
        ax.bar(site_names, values, bottom=bottoms, label=category, color=colors[index % len(colors)])
        bottoms = [bottom + value for bottom, value in zip(bottoms, values)]

    for index, total in enumerate(bottoms):
        ax.text(index, total, str(total), ha="center", va="bottom", fontsize=9)

    ax.set_title("MySekai materials by site", weight="bold")
    ax.set_ylabel("quantity")
    ax.grid(axis="y", color="#d8e0e5", linewidth=0.7)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=7, frameon=True)
    ax.tick_params(axis="x", rotation=18)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)
    return image_name


def draw_mysekai_current_summary(
    visitors: list[dict[str, Any]],
    resource_rows: list[dict[str, Any]],
    master: MasterCache,
    output_dir: Path,
) -> str:
    import matplotlib.pyplot as plt

    setup_matplotlib_font()
    image_name = "mysekai_current_summary.png"
    path = output_dir / image_name

    by_site: dict[tuple[Any, str], list[dict[str, Any]]] = defaultdict(list)
    for row in resource_rows:
        by_site[(row.get("siteId"), safe_text(row.get("siteName") or row.get("siteId")))].append(row)

    max_resources = max((len(rows) for rows in by_site.values()), default=1)
    card_rows = max(3, (max_resources + 4) // 5)
    fig_height = max(13.5, 4.2 + card_rows * 1.25)
    fig = plt.figure(figsize=(15.5, fig_height), dpi=150)
    grid = fig.add_gridspec(3, 2, height_ratios=[0.58, 1.18, 1.18], hspace=0.065, wspace=0.035)
    ax_header = fig.add_subplot(grid[0, :])
    site_axes = [
        fig.add_subplot(grid[1, 0]),
        fig.add_subplot(grid[1, 1]),
        fig.add_subplot(grid[2, 0]),
        fig.add_subplot(grid[2, 1]),
    ]

    ax_header.set_axis_off()
    ax_header.set_xlim(0, 1)
    ax_header.set_ylim(0, 1)
    ax_header.text(0.03, 0.82, "當前拜訪角色", fontsize=24, weight="bold", color="#172026", va="top")

    seen_characters: list[int] = []
    for visitor in visitors:
        for character_id in character_ids_for_group(visitor.get("groupId"), master):
            if character_id not in seen_characters:
                seen_characters.append(character_id)

    if not seen_characters:
        ax_header.text(0.5, 0.35, "沒有拜訪資料", ha="center", va="center", fontsize=16, color="#62727d")
    for index, character_id in enumerate(seen_characters[:12]):
        x = 0.075 + index * 0.075
        y = 0.34
        add_icon(ax_header, character_icon_path(character_id), x, y, zoom=0.22, fallback=master.label("gameCharacters", character_id))
        ax_header.text(x, 0.045, master.label("gameCharacters", character_id)[:5], ha="center", va="center", fontsize=9.5, color="#172026")

    for ax in site_axes:
        ax.set_axis_off()
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)

    for ax, ((site_id, site_name), rows) in zip(site_axes, by_site.items()):
        ax.add_patch(plt.Rectangle((0.0, 0.0), 1.0, 1.0, facecolor="#f5fbfd", edgecolor="#d9e1e5", alpha=0.9, linewidth=1.0))
        ax.text(0.035, 0.955, site_name, fontsize=17, weight="bold", color="#172026", va="top")

        preview = mysekai_harvest_background_path(site_id)
        if preview:
            try:
                image = plt.imread(preview)
                ax.imshow(image, extent=[0.27, 0.73, 0.70, 0.945], alpha=0.9, zorder=2)
            except Exception:
                pass

        rows = sorted(rows, key=lambda row: int(row.get("quantity") or 0), reverse=True)
        cols = 5
        start_y = 0.62
        y_step = 0.19 if len(rows) <= 15 else max(0.125, 0.60 / max(1, ((len(rows) + cols - 1) // cols)))
        for index, row in enumerate(rows):
            col = index % cols
            line = index // cols
            x = 0.07 + col * 0.185
            y = start_y - line * y_step
            icon_path = material_icon_path(safe_text(row.get("resourceType")), row.get("resourceId"), master)
            add_icon(ax, icon_path, x, y, zoom=0.20, fallback=safe_text(row.get("resourceName")))
            ax.text(x + 0.068, y, safe_text(row.get("quantity", 0)), fontsize=18, weight="bold", color="#5d6f7d", va="center")

    fig.patch.set_facecolor("#eaf7ff")
    fig.subplots_adjust(left=0.018, right=0.982, top=0.985, bottom=0.018)
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return image_name


def draw_mysekai_resource_map(
    maps: list[dict[str, Any]],
    master: MasterCache,
    output_dir: Path,
) -> str:
    import matplotlib.pyplot as plt

    setup_matplotlib_font()
    image_name = "mysekai_resource_map.png"
    path = output_dir / image_name
    write_mysekai_resource_map_transform_debug(output_dir)

    combined_bg = find_combined_mysekai_resource_background()
    if combined_bg and combined_bg.exists():
        image = plt.imread(combined_bg)
        aspect = image.shape[1] / image.shape[0] if getattr(image, "shape", None) is not None else 1.25
        fig = plt.figure(figsize=(14, 14 / aspect), dpi=150)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.imshow(image, extent=[0, 2, 0, 2], origin="upper", aspect="auto")
        ax.set_aspect("auto")
        ax.set_xlim(0, 2)
        ax.set_ylim(0, 2)
        ax.set_axis_off()

        for site in maps[:4]:
            site_id = site.get("mysekaiSiteId")
            site_name = safe_text(site.get("mysekaiSiteId_label") or master.label("mysekaiSites", site_id, f"site {site_id}"))
            quadrant = mysekai_site_quadrant(site_id, site_name)
            if not quadrant:
                continue
            drops = [item for item in site.get("userMysekaiSiteHarvestResourceDrops", []) if isinstance(item, dict)]
            fixtures = [item for item in site.get("userMysekaiSiteHarvestFixtures", []) if isinstance(item, dict)]
            bounds = mysekai_position_bounds(fixtures, drops)

            by_position: dict[tuple[Any, Any], Counter[tuple[str, Any, str]]] = defaultdict(Counter)
            for drop in drops:
                resource_type = safe_text(drop.get("resourceType"))
                resource_id = drop.get("resourceId")
                label = resource_label(resource_type, resource_id, master)
                quantity = int(drop.get("quantity") or 1)
                by_position[(drop.get("positionX", 0), drop.get("positionZ", 0))][(resource_type, resource_id, label)] += quantity

            for (x, z), counter in by_position.items():
                shown_items = [
                    (key, quantity)
                    for key, quantity in counter.most_common()
                    if not (key[0] == "mysekai_material" and key[1] in {1, 6})
                ]
                if not shown_items:
                    continue
                base_x, base_y = transform_mysekai_position(x, z, bounds, quadrant, site_id)
                for index, ((resource_type, resource_id, label), quantity) in enumerate(shown_items):
                    col = index % 3
                    line = index // 3
                    xx = base_x + (col - 1) * 0.032
                    yy = base_y - line * 0.034
                    add_icon(ax, material_icon_path(resource_type, resource_id, master), xx, yy, zoom=0.08, fallback=label)
                    ax.text(
                        xx + 0.018,
                        yy + 0.018,
                        f"{quantity}",
                        fontsize=6.2,
                        weight="bold",
                        color="#172026",
                        bbox={"boxstyle": "round,pad=0.08", "fc": "#e9fdff", "ec": "#76d8de", "alpha": 0.92},
                        zorder=6,
                    )

        fig.savefig(path, facecolor="#ffffff", pad_inches=0)
        plt.close(fig)
        return image_name

    fig, axes = plt.subplots(2, 2, figsize=(13, 10), dpi=150)
    axes_flat = list(axes.flatten())
    for ax in axes_flat:
        ax.set_axis_off()

    for ax, site in zip(axes_flat, maps[:4]):
        site_id = site.get("mysekaiSiteId")
        site_name = safe_text(site.get("mysekaiSiteId_label") or master.label("mysekaiSites", site_id, f"site {site_id}"))
        drops = [item for item in site.get("userMysekaiSiteHarvestResourceDrops", []) if isinstance(item, dict)]
        fixtures = [item for item in site.get("userMysekaiSiteHarvestFixtures", []) if isinstance(item, dict)]

        xs = [item.get("positionX", 0) for item in fixtures] or [item.get("positionX", 0) for item in drops]
        zs = [item.get("positionZ", 0) for item in fixtures] or [item.get("positionZ", 0) for item in drops]
        margin = 4
        x_min, x_max = (min(xs) - margin, max(xs) + margin) if xs else (-25, 25)
        z_min, z_max = (min(zs) - margin, max(zs) + margin) if zs else (-25, 25)

        bg_path = find_mysekai_map_background(site_id, site_name) or mysekai_harvest_background_path(site_id)
        if bg_path and bg_path.exists():
            try:
                image = plt.imread(bg_path)
                ax.imshow(image, extent=[x_min, x_max, z_min, z_max], origin="lower", alpha=0.95)
            except Exception:
                ax.set_facecolor("#f1f6f7")
        else:
            ax.set_facecolor("#f1f6f7")
            ax.grid(True, color="#d8e0e5", linewidth=0.6)

        by_position: dict[tuple[Any, Any], Counter[tuple[str, Any, str]]] = defaultdict(Counter)
        for drop in drops:
            resource_type = safe_text(drop.get("resourceType"))
            resource_id = drop.get("resourceId")
            label = resource_label(resource_type, resource_id, master)
            quantity = int(drop.get("quantity") or 1)
            by_position[(drop.get("positionX", 0), drop.get("positionZ", 0))][(resource_type, resource_id, label)] += quantity

        for (x, z), counter in by_position.items():
            shown_items = [
                (key, quantity)
                for key, quantity in counter.most_common()
                if not (key[0] == "mysekai_material" and key[1] in {1, 6})
            ]
            if not shown_items:
                continue
            for index, ((resource_type, resource_id, label), quantity) in enumerate(shown_items):
                col = index % 3
                line = index // 3
                xx = float(x) + (col - 1) * 1.75
                zz = float(z) - line * 1.75
                add_icon(ax, material_icon_path(resource_type, resource_id, master), xx, zz, zoom=0.075, fallback=label)
                ax.text(
                    xx + 1.0,
                    zz + 1.0,
                    f"x{quantity}",
                    fontsize=7.2,
                    weight="bold",
                    color="#172026",
                    bbox={"boxstyle": "round,pad=0.12", "fc": "#ffffff", "ec": "#d9e1e5", "alpha": 0.9},
                    zorder=6,
                )

        ax.set_title(site_name, fontsize=14, weight="bold")
        ax.set_xlim(x_min, x_max)
        ax.set_ylim(z_min, z_max)
        ax.set_aspect("equal", adjustable="box")
        ax.set_axis_on()
        ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)

    fig.suptitle("四張地圖資源標示", fontsize=22, weight="bold")
    fig.tight_layout()
    fig.savefig(path, facecolor="#ffffff")
    plt.close(fig)
    return image_name


def draw_music_status_query(
    music_rows: list[dict[str, Any]],
    output_dir: Path,
    *,
    mode: str = "difficulty",
    value: str = "master",
    page: int = 1,
    per_page: int = 60,
) -> str:
    import matplotlib.pyplot as plt

    setup_matplotlib_font()
    mode = (mode or "difficulty").lower()
    value = safe_text(value or "").strip()
    page = max(1, int(page or 1))
    per_page = min(100, max(20, int(per_page or 60)))

    if mode in {"level", "playlevel", "難度"}:
        filtered = [row for row in music_rows if safe_text(row.get("playLevel")) == value]
        title = f"Lv {value} 歌曲通關狀況"
        file_part = f"level_{slug(value, 'all')}"
        filtered.sort(
            key=lambda row: (
                DIFFICULTY_ORDER.get(safe_text(row.get("difficultyKey") or row.get("difficulty")).lower(), 99),
                safe_text(row.get("title")),
            )
        )
    else:
        normalized = value.lower()
        filtered = [row for row in music_rows if safe_text(row.get("difficultyKey") or row.get("difficulty")).lower() == normalized]
        title = f"{DIFFICULTY_LABELS.get(normalized, value)} 歌曲通關狀況"
        file_part = f"difficulty_{slug(normalized, 'all')}"
        filtered.sort(key=lambda row: (int(row.get("playLevel") or 0), safe_text(row.get("title"))))

    status_order = ["未記錄", "未通關", "Clear", "Full Combo", "All Perfect"]
    status_colors = {
        "未記錄": "#64637a",
        "未通關": "#8a879b",
        "Clear": "#45c7d8",
        "Full Combo": "#f4cf43",
        "All Perfect": "#ed78d8",
    }

    status_aliases = {
        "not_played": "未記錄",
        "not clear": "未通關",
        "not_clear": "未通關",
        "clear": "Clear",
        "full combo": "Full Combo",
        "full_combo": "Full Combo",
        "all perfect": "All Perfect",
        "full_perfect": "All Perfect",
    }

    def display_status(row: dict[str, Any]) -> str:
        raw = safe_text(row.get("bestStatus") or "未記錄").strip()
        return status_aliases.get(raw.lower(), raw)

    total = len(filtered)
    start = (page - 1) * per_page
    page_rows = filtered[start : start + per_page]
    max_page = max(1, (total + per_page - 1) // per_page)
    if not page_rows and total:
        page = max_page
        start = (page - 1) * per_page
        page_rows = filtered[start : start + per_page]

    displayed_statuses = [display_status(row) for row in filtered]
    table_counts = Counter(displayed_statuses)
    clear_count = sum(status in {"Clear", "Full Combo", "All Perfect"} for status in displayed_statuses)
    full_combo_count = sum(status in {"Full Combo", "All Perfect"} for status in displayed_statuses)
    all_perfect_count = table_counts["All Perfect"]
    summary_parts = []
    if table_counts["未記錄"]:
        summary_parts.append(f"未記錄 {table_counts['未記錄']}")
    if table_counts["未通關"]:
        summary_parts.append(f"未通關 {table_counts['未通關']}")
    summary_parts.extend(
        [
            f"Clear {clear_count}",
            f"Full Combo {full_combo_count}",
            f"All Perfect {all_perfect_count}",
        ]
    )
    summary = "  ".join(summary_parts)

    fig_height = max(5.4, 1.7 + len(page_rows) * 0.62)
    image_name = f"music_status_{file_part}_p{page}.png"
    fig, ax = plt.subplots(figsize=(12, fig_height), dpi=150)
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    fig.patch.set_facecolor("#f7fbff")

    ax.text(0.035, 0.965, title, fontsize=24, weight="bold", color="#172026", va="top")
    ax.text(0.035, 0.91, f"共 {total} 首  Page {page}/{max_page}  {summary}", fontsize=12.5, color="#45445f", va="top")

    master = MasterCache(DEFAULT_MASTER_CACHE, "tc")
    y = 0.84
    y_step = 0.76 / max(1, len(page_rows))
    for row in page_rows:
        ax.add_patch(
            plt.Rectangle(
                (0.025, y - y_step * 0.43),
                0.95,
                y_step * 0.82,
                facecolor="#ffffff",
                edgecolor="#d9e1e5",
                linewidth=0.9,
                alpha=0.96,
                zorder=0,
            )
        )
        jacket_path = music_jacket_path(row.get("assetbundleName") or row.get("musicId"), master)
        add_icon(ax, jacket_path, 0.073, y, zoom=0.105, fallback=safe_text(row.get("musicId")))

        status = display_status(row)
        title_text = safe_text(row.get("title") or row.get("musicId"))
        ax.text(0.13, y + y_step * 0.12, title_text[:34], fontsize=12.5, weight="bold", color="#172026", va="center")
        ax.text(
            0.13,
            y - y_step * 0.18,
            f"{row.get('difficulty') or row.get('difficultyKey')}  Lv.{row.get('playLevel') or '-'}  Score {row.get('highScore') or 0}",
            fontsize=10.5,
            color="#62727d",
            va="center",
        )
        ax.text(
            0.88,
            y,
            status,
            fontsize=11,
            weight="bold",
            ha="center",
            va="center",
            color="#172026",
            bbox={"boxstyle": "round,pad=0.38", "fc": status_colors.get(status, "#45c7d8"), "ec": "none", "alpha": 0.95},
        )
        y -= y_step

    if not page_rows:
        ax.text(0.5, 0.5, "沒有符合條件的歌曲資料", fontsize=18, ha="center", va="center", color="#62727d")

    fig.tight_layout()
    fig.savefig(output_dir / image_name, facecolor=fig.get_facecolor())
    plt.close(fig)
    return image_name


def draw_music_status_query(
    music_rows: list[dict[str, Any]],
    output_dir: Path,
    *,
    mode: str = "difficulty",
    value: str = "master",
    page: int = 1,
    per_page: int = 30,
) -> str:
    import matplotlib.pyplot as plt

    setup_matplotlib_font()
    mode = (mode or "difficulty").lower()
    value = safe_text(value or "").strip()
    page = max(1, int(page or 1))
    per_page = min(30, max(12, int(per_page or 30)))

    if mode in {"level", "playlevel", "等級"}:
        filtered = [row for row in music_rows if safe_text(row.get("playLevel")) == value]
        title = f"Lv {value} 歌曲通關狀況"
        file_part = f"level_{slug(value, 'all')}"
        filtered.sort(
            key=lambda row: (
                DIFFICULTY_ORDER.get(safe_text(row.get("difficultyKey") or row.get("difficulty")).lower(), 99),
                safe_text(row.get("title")),
            )
        )
    else:
        normalized = value.lower()
        filtered = [row for row in music_rows if safe_text(row.get("difficultyKey") or row.get("difficulty")).lower() == normalized]
        title = f"{DIFFICULTY_LABELS.get(normalized, value)} 歌曲通關狀況"
        file_part = f"difficulty_{slug(normalized, 'all')}"
        filtered.sort(key=lambda row: (int(row.get("playLevel") or 0), safe_text(row.get("title"))))

    status_order = ["未記錄", "未通關", "Clear", "Full Combo", "All Perfect"]
    status_colors = {
        "未記錄": "#64637a",
        "未通關": "#8a879b",
        "Clear": "#45c7d8",
        "Full Combo": "#ef78c8",
        "All Perfect": "#49d3c6",
    }
    status_aliases = {
        "not_played": "未記錄",
        "not clear": "未通關",
        "not_clear": "未通關",
        "clear": "Clear",
        "full combo": "Full Combo",
        "full_combo": "Full Combo",
        "all perfect": "All Perfect",
        "full_perfect": "All Perfect",
    }

    def display_status(row: dict[str, Any]) -> str:
        raw = safe_text(row.get("bestStatus") or "未記錄").strip()
        return status_aliases.get(raw.lower(), raw)

    total = len(filtered)
    max_page = max(1, (total + per_page - 1) // per_page)
    if page > max_page:
        page = max_page
    start = (page - 1) * per_page
    page_rows = filtered[start : start + per_page]
    displayed_statuses = [display_status(row) for row in filtered]
    table_counts = Counter(displayed_statuses)
    clear_count = sum(status in {"Clear", "Full Combo", "All Perfect"} for status in displayed_statuses)
    full_combo_count = sum(status in {"Full Combo", "All Perfect"} for status in displayed_statuses)
    summary_parts = []
    if table_counts["未記錄"]:
        summary_parts.append(f"未記錄 {table_counts['未記錄']}")
    if table_counts["未通關"]:
        summary_parts.append(f"未通關 {table_counts['未通關']}")
    summary_parts.extend(
        [
            f"Clear {clear_count}",
            f"Full Combo {full_combo_count}",
            f"All Perfect {table_counts['All Perfect']}",
        ]
    )
    summary = "  ".join(summary_parts)

    image_name = f"music_status_{file_part}_p{page}.png"
    fig, ax = plt.subplots(figsize=(9.2, 14.8), dpi=150)
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    fig.patch.set_facecolor("#f6fcff")

    ax.text(0.055, 0.975, title, fontsize=21, weight="bold", color="#172026", va="top")
    ax.text(0.055, 0.943, f"共 {total} 首  Page {page}/{max_page}  {summary}", fontsize=10.5, color="#45445f", va="top")

    master = MasterCache(DEFAULT_MASTER_CACHE, "tc")
    cols = 3
    card_w = 0.275
    card_h = 0.067
    x_gap = 0.035
    y_gap = 0.018
    start_x = 0.055
    start_y = 0.895
    for index, row in enumerate(page_rows):
        col = index % cols
        line = index // cols
        x0 = start_x + col * (card_w + x_gap)
        y0 = start_y - line * (card_h + y_gap)
        difficulty_label = safe_text(row.get("difficulty") or row.get("difficultyKey"))
        difficulty_color = DIFFICULTY_COLORS.get(difficulty_label, "#bd4ef0")
        ax.add_patch(
            plt.Rectangle(
                (x0, y0 - card_h),
                card_w,
                card_h,
                facecolor="#ffffff",
                edgecolor=difficulty_color,
                linewidth=1.4,
                alpha=0.96,
                zorder=0,
            )
        )
        level = safe_text(row.get("playLevel") or "-")
        ax.text(
            x0 - 0.006,
            y0 - 0.012,
            level,
            fontsize=7.8,
            weight="bold",
            ha="center",
            va="center",
            color="#ffffff",
            bbox={"boxstyle": "circle,pad=0.18", "fc": difficulty_color, "ec": "none", "alpha": 0.95},
            zorder=4,
        )
        jacket_path = music_jacket_path(row.get("assetbundleName") or row.get("musicId"), master)
        add_icon(ax, jacket_path, x0 + 0.039, y0 - card_h / 2, zoom=0.36, fallback=safe_text(row.get("musicId")))

        status = display_status(row)
        title_text = safe_text(row.get("title") or row.get("musicId"))
        short_title = title_text if len(title_text) <= 12 else title_text[:11] + "..."
        ax.text(x0 + 0.083, y0 - 0.022, short_title, fontsize=8.8, weight="bold", color="#172026", va="center")
        ax.text(
            x0 + 0.083,
            y0 - 0.047,
            status.upper() if status in {"Full Combo", "All Perfect"} else status,
            fontsize=7.8,
            color=status_colors.get(status, "#45c7d8"),
            weight="bold",
            va="center",
        )
        ax.text(
            x0 + card_w - 0.012,
            y0 - 0.021,
            difficulty_label.upper(),
            fontsize=7.2,
            weight="bold",
            ha="right",
            va="center",
            color="#ffffff",
            bbox={"boxstyle": "round,pad=0.18", "fc": difficulty_color, "ec": "none"},
        )

    if not page_rows:
        ax.text(0.5, 0.5, "沒有符合條件的歌曲資料", fontsize=18, ha="center", va="center", color="#62727d")

    fig.tight_layout()
    fig.savefig(output_dir / image_name, facecolor=fig.get_facecolor())
    plt.close(fig)
    return image_name


def draw_music_status_query_pages(
    music_rows: list[dict[str, Any]],
    output_dir: Path,
    *,
    mode: str = "difficulty",
    value: str = "master",
    per_page: int = 30,
) -> list[str]:
    mode = (mode or "difficulty").lower()
    value = safe_text(value or "").strip()
    if mode in {"level", "playlevel", "等級"}:
        total = sum(1 for row in music_rows if safe_text(row.get("playLevel")) == value)
    else:
        normalized = value.lower()
        total = sum(1 for row in music_rows if safe_text(row.get("difficultyKey") or row.get("difficulty")).lower() == normalized)
    per_page = min(30, max(12, int(per_page or 30)))
    max_page = max(1, (total + per_page - 1) // per_page)
    return [
        draw_music_status_query(music_rows, output_dir, mode=mode, value=value, page=page, per_page=per_page)
        for page in range(1, max_page + 1)
    ]


def draw_profile_events_image(event_rows: list[dict[str, Any]], output_dir: Path) -> str:
    import matplotlib.pyplot as plt

    setup_matplotlib_font()
    image_name = "profile_event_rankings.png"
    path = output_dir / image_name

    scored = []
    wl_rows = []
    for row in event_rows:
        score_text = safe_text(row.get("score")).replace(",", "")
        try:
            score = int(score_text)
        except ValueError:
            score = -1
        item = dict(row)
        item["_scoreInt"] = score
        if score >= 0:
            scored.append(item)
        if safe_text(row.get("chapter")) or "world" in safe_text(row.get("eventName")).lower() or "World Link" in safe_text(row.get("description")):
            wl_rows.append(item)

    scored.sort(key=lambda row: int(row.get("_scoreInt") or 0), reverse=True)
    shown_left = scored[:]
    seen_events = {(safe_text(row.get("eventName")), safe_text(row.get("chapter"))) for row in shown_left}
    for row in event_rows:
        key = (safe_text(row.get("eventName")), safe_text(row.get("chapter")))
        if key in seen_events:
            continue
        if not (row.get("rank") or row.get("rankUpperBound") or row.get("score")):
            continue
        item = dict(row)
        item["_scoreInt"] = -1
        shown_left.append(item)
        seen_events.add(key)
    shown_left = shown_left[:30]
    shown_right = wl_rows[:30]

    fig, axes = plt.subplots(1, 2, figsize=(15, 11), dpi=150)
    titles = ["活動紀錄前 30 筆", "World Link / 章節紀錄"]
    datasets = [shown_left, shown_right]
    for ax, title, rows in zip(axes, titles, datasets):
        ax.set_axis_off()
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.text(0.02, 0.98, title, fontsize=20, weight="bold", va="top")
        if not rows:
            ax.text(0.5, 0.5, "沒有可用資料", ha="center", va="center", fontsize=15, color="#62727d")
            continue
        for index, row in enumerate(rows[:30]):
            y = 0.91 - index * 0.029
            rank = safe_text(row.get("rank") or (f"Top {row.get('rankUpperBound')}" if row.get("rankUpperBound") else "-"))
            score = safe_text(row.get("score") or "-")
            event_name = safe_text(row.get("eventName") or row.get("eventId") or "-")
            chapter = f" Ch.{row.get('chapter')}" if row.get("chapter") else ""
            ax.text(0.02, y, f"#{index + 1}", fontsize=10.5, weight="bold", va="center")
            ax.text(0.10, y, event_name[:26] + chapter, fontsize=9.2, va="center")
            ax.text(0.68, y, score, fontsize=9.8, weight="bold", va="center", ha="right")
            ax.text(0.98, y, rank, fontsize=9.2, va="center", ha="right", color="#b34b6f")

    fig.tight_layout()
    fig.savefig(path, facecolor="#ffffff")
    plt.close(fig)
    return image_name


def draw_profile_overview_image(
    data: dict[str, Any],
    music_summary: dict[str, Any],
    master: MasterCache,
    output_dir: Path,
) -> str:
    import matplotlib.pyplot as plt

    setup_matplotlib_font()
    image_name = "profile_overview.png"
    path = output_dir / image_name

    user = data.get("userGamedata") if isinstance(data.get("userGamedata"), dict) else {}
    profile = data.get("userProfile") if isinstance(data.get("userProfile"), dict) else {}
    characters = [row for row in data.get("userCharacters", []) if isinstance(row, dict)]
    characters.sort(key=lambda row: int(row.get("characterId") or 0))

    fig = plt.figure(figsize=(15, 10), dpi=150)
    grid = fig.add_gridspec(2, 2, height_ratios=[0.38, 0.62], width_ratios=[0.9, 1.1], hspace=0.16, wspace=0.12)
    ax_info = fig.add_subplot(grid[0, 0])
    ax_music = fig.add_subplot(grid[1, 0])
    ax_right = fig.add_subplot(grid[:, 1])
    for ax in (ax_info, ax_music, ax_right):
        ax.set_axis_off()
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)

    ax_info.text(0.02, 0.88, safe_text(user.get("name") or "-"), fontsize=28, weight="bold")
    ax_info.text(0.02, 0.70, f"ID: {user.get('userId', '-')}", fontsize=13)
    ax_info.text(0.02, 0.56, f"Rank: {user.get('rank', '-')}", fontsize=17, weight="bold", color="#45445f")
    ax_info.text(0.02, 0.40, f"簽名：{profile.get('word', '')}", fontsize=13)

    by_diff = music_summary.get("byDifficulty", {})
    statuses = [("Clear", "CLEAR"), ("Full Combo", "FULL COMBO"), ("All Perfect", "ALL PERFECT")]
    difficulties = ["Easy", "Normal", "Hard", "Expert", "Master", "Append"]
    y = 0.90
    for status_key, label in statuses:
        ax_music.text(0.02, y, label, fontsize=16, weight="bold", color="#45445f")
        y -= 0.08
        for index, diff in enumerate(difficulties):
            x = 0.04 + index * 0.155
            count = int((by_diff.get(diff, {}) or {}).get(status_key, 0))
            ax_music.text(
                x,
                y,
                diff.upper(),
                fontsize=9,
                weight="bold",
                ha="center",
                color="#ffffff",
                bbox={"boxstyle": "round,pad=0.25", "fc": DIFFICULTY_COLORS.get(diff, "#27c2d8"), "ec": "none"},
            )
            ax_music.text(x, y - 0.065, str(count), fontsize=14, ha="center", weight="bold")
        y -= 0.18

    mvp = 0
    superstar = 0
    for row in compact_rows(data.get("compactUserMusicResults")):
        mvp += int(row.get("mvpCount") or 0)
        superstar += int(row.get("superStarCount") or 0)
    challenge_results = [row for row in data.get("userChallengeLiveSoloResults", []) if isinstance(row, dict)]
    best_challenge = max((int(row.get("highScore") or 0) for row in challenge_results), default=0)

    ax_right.text(0.02, 0.96, "多人 LIVE", fontsize=18, weight="bold", color="#45445f")
    ax_right.text(0.05, 0.88, f"MVP  {mvp} 回", fontsize=18, weight="bold")
    ax_right.text(0.42, 0.88, f"SUPER STAR  {superstar} 回", fontsize=18, weight="bold")
    ax_right.text(0.02, 0.77, "挑戰 LIVE", fontsize=18, weight="bold", color="#45445f")
    ax_right.text(0.05, 0.69, f"最高分  {best_challenge}", fontsize=18, weight="bold")
    ax_right.text(0.02, 0.58, "角色等級", fontsize=18, weight="bold", color="#45445f")

    for index, row in enumerate(characters[:26]):
        col = index % 4
        line = index // 4
        x = 0.075 + col * 0.23
        yy = 0.49 - line * 0.078
        character_id = row.get("characterId")
        if isinstance(character_id, int):
            add_icon(ax_right, character_icon_path(character_id), x, yy, zoom=0.30)
        ax_right.text(x + 0.115, yy, safe_text(row.get("characterRank") or 0), fontsize=14, weight="bold", va="center")

    fig.tight_layout()
    fig.savefig(path, facecolor="#ffffff")
    plt.close(fig)
    return image_name


def build_mysekai_report(args: argparse.Namespace) -> int:
    input_path = args.json_file
    output_dir = ensure_output_dir(args.output_dir)
    master = MasterCache(args.master_cache, args.locale)
    data = read_json(input_path)
    if not isinstance(data, dict):
        raise ValueError("MySekai JSON top-level value must be an object.")

    maps = find_harvest_maps(data)
    visitors = visitor_rows(data)
    site_rows, resource_rows, image_names = analyze_harvest_maps(maps, master, output_dir, not args.no_maps)
    material_chart = draw_material_summary(resource_rows, output_dir)
    current_summary_image = draw_mysekai_current_summary(visitors, resource_rows, master, output_dir)
    resource_map_image = draw_mysekai_resource_map(maps, master, output_dir)

    summary = {
        "source": str(input_path),
        "harvestMapCount": len(maps),
        "siteCount": len(site_rows),
        "visitorCount": len(visitors),
        "materialChart": material_chart,
        "currentSummaryImage": current_summary_image,
        "resourceMapImage": resource_map_image,
        "sites": site_rows,
        "resources": resource_rows,
        "visitors": visitors,
    }

    write_json(output_dir / "mysekai_summary.json", summary)
    write_csv(
        output_dir / "mysekai_sites.csv",
        site_rows,
        ["siteId", "siteName", "fixtureCount", "aliveCount", "depletedCount", "dropCount", "fixtureTypes", "statuses"],
    )
    write_csv(
        output_dir / "mysekai_resources.csv",
        resource_rows,
        ["siteId", "siteName", "resourceType", "resourceId", "resourceName", "quantity"],
    )
    write_csv(
        output_dir / "mysekai_visitors.csv",
        visitors,
        ["gate", "groupId", "visitor", "characters", "visitCount", "isReservation"],
    )

    stats = [
        ("地圖數", len(maps)),
        ("採集點", sum(int(row.get("fixtureCount") or 0) for row in site_rows)),
        ("掉落筆數", sum(int(row.get("dropCount") or 0) for row in site_rows)),
        ("拜訪組合", len(visitors)),
    ]
    stat_html = "".join(f"<div class=\"stat\"><span>{html.escape(label)}</span><b>{value}</b></div>" for label, value in stats)

    images_html = "".join(
        f"<figure><img src=\"{html.escape(name)}\" alt=\"{html.escape(name)}\"><figcaption>{html.escape(name)}</figcaption></figure>"
        for name in image_names
    )
    material_html = ""
    if material_chart:
        material_html = (
            f"<figure><img src=\"{html.escape(material_chart)}\" "
            f"alt=\"MySekai materials by site\"><figcaption>{html.escape(material_chart)}</figcaption></figure>"
        )
    summary_images_html = "".join(
        f"<figure><img src=\"{html.escape(name)}\" alt=\"{html.escape(name)}\"><figcaption>{html.escape(name)}</figcaption></figure>"
        for name in (current_summary_image, resource_map_image)
        if name
    )

    body = f"""
<h1>MySekai Snapshot Report</h1>
<p class="meta">Source: <code>{html.escape(str(input_path))}</code></p>
<section class="stats">{stat_html}</section>
<h2>Discord 回傳圖片</h2>
<div class="gallery">{summary_images_html}</div>
<h2>拜訪角色</h2>
{render_table(visitors, [('visitor', '拜訪組合'), ('characters', '角色'), ('visitCount', '拜訪次數'), ('gate', '大門')], limit=80)}
<h2>採集地圖摘要</h2>
{render_table(site_rows, [('siteId', 'Site ID'), ('siteName', '地點'), ('fixtureCount', '採集點'), ('aliveCount', 'HP>0'), ('depletedCount', 'HP=0'), ('dropCount', '掉落筆數')])}
<h2>資源統計</h2>
{render_table(resource_rows, [('siteName', '地點'), ('resourceName', '資源'), ('resourceType', '類型'), ('resourceId', 'ID'), ('quantity', '數量')], limit=120)}
<h2>四張地圖材料統計</h2>
<div class="gallery">{material_html}</div>
<h2>地圖</h2>
<div class="gallery">{images_html}</div>
<p class="note">CSV/JSON: <code>mysekai_sites.csv</code>, <code>mysekai_resources.csv</code>, <code>mysekai_visitors.csv</code>, <code>mysekai_summary.json</code></p>
"""
    (output_dir / "mysekai_report.html").write_text(html_page("MySekai Snapshot Report", body), encoding="utf-8")

    print(f"wrote {output_dir / 'mysekai_report.html'}")
    return 0


def user_id_from_snapshot(data: dict[str, Any], input_path: Path) -> int | None:
    for key in ("userGamedata", "userRegistration", "userProfile"):
        value = data.get(key)
        if isinstance(value, dict) and isinstance(value.get("userId"), int):
            return value["userId"]
    match = re.search(r"(\d{6,})", input_path.stem)
    return int(match.group(1)) if match else None


def decode_card_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if not isinstance(row, list):
        return {}
    decoded = {field: row[index] if index < len(row) else None for index, field in enumerate(CARD_ROW_FIELDS)}
    decoded["raw"] = row
    return decoded


def episode_summary(episodes: Any) -> tuple[int, int, str]:
    if not isinstance(episodes, list):
        return 0, 0, ""
    total = 0
    read = 0
    labels = []
    for episode in episodes:
        if not isinstance(episode, list) or len(episode) < 2:
            continue
        total += 1
        status = safe_text(episode[1])
        if status == "already_read":
            read += 1
        labels.append(f"{episode[0]}:{status}")
    return read, total, ", ".join(labels)


def analyze_cards(data: dict[str, Any], master: MasterCache) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in data.get("userCards", []):
        decoded = decode_card_row(raw)
        card_id = decoded.get("cardId")
        card = master.row("cards", card_id)
        character_id = card.get("characterId")
        rarity = card.get("cardRarityType")
        read_count, episode_count, episode_statuses = episode_summary(decoded.get("episodes"))

        rows.append(
            {
                "cardId": card_id,
                "cardName": master.label("cards", card_id),
                "characterId": character_id,
                "character": master.label("gameCharacters", character_id),
                "rarity": RARITY_LABELS.get(safe_text(rarity), safe_text(rarity)),
                "attr": card.get("attr", ""),
                "level": decoded.get("level"),
                "skillLevel": decoded.get("skillLevel"),
                "skillExp": decoded.get("skillExp"),
                "masterRank": decoded.get("masterRank"),
                "specialTrainingStatus": decoded.get("specialTrainingStatus"),
                "defaultImage": decoded.get("defaultImage"),
                "duplicateCount": decoded.get("duplicateCount"),
                "obtainedAt": ms_to_iso(decoded.get("obtainedAt")),
                "episodesRead": read_count,
                "episodesTotal": episode_count,
                "episodeStatuses": episode_statuses,
            }
        )

    rows.sort(key=lambda row: (safe_text(row.get("character")), safe_text(row.get("rarity")), int(row.get("cardId") or 0)))
    summary = {
        "ownedCards": len(rows),
        "rarity": Counter(row["rarity"] for row in rows),
        "specialTrainingStatus": Counter(row["specialTrainingStatus"] for row in rows),
        "defaultImage": Counter(row["defaultImage"] for row in rows),
        "masterRank": Counter(row["masterRank"] for row in rows),
        "skillLevel": Counter(row["skillLevel"] for row in rows),
        "episodeRead": sum(int(row.get("episodesRead") or 0) for row in rows),
        "episodeTotal": sum(int(row.get("episodesTotal") or 0) for row in rows),
    }
    summary = {key: dict(value) if isinstance(value, Counter) else value for key, value in summary.items()}
    return rows, summary


def draw_card_summary(card_summary: dict[str, Any], output_dir: Path) -> str:
    import matplotlib.pyplot as plt

    setup_matplotlib_font()
    image_name = "cards_by_rarity.png"
    rarity_counts = card_summary.get("rarity", {})
    labels = list(rarity_counts.keys())
    values = [rarity_counts[label] for label in labels]

    fig, ax = plt.subplots(figsize=(7.5, 4.3), dpi=150)
    ax.bar(labels, values, color=["#0f7f87", "#8c6bb1", "#d88a30", "#b34b6f", "#5d6f7d"][: len(labels)])
    ax.set_title("Owned cards by rarity", weight="bold")
    ax.set_ylabel("cards")
    ax.grid(axis="y", color="#d8e0e5", linewidth=0.7)
    for index, value in enumerate(values):
        ax.text(index, value, str(value), ha="center", va="bottom", fontsize=9)
    fig.tight_layout()
    fig.savefig(output_dir / image_name)
    plt.close(fig)
    return image_name


def result_status(row: dict[str, Any]) -> str:
    label = safe_text(row.get("playResult_label") or row.get("playResult"))
    if row.get("fullPerfectFlg") is True or label == "full_perfect":
        return "full_perfect"
    if row.get("fullComboFlg") is True or label == "full_combo":
        return "full_combo"
    if label == "clear":
        return "clear"
    if label == "not_clear":
        return "not_clear"
    return label if label in RESULT_PRIORITY else "not_played"


def normalize_difficulty(row: dict[str, Any]) -> str:
    value = row.get("musicDifficultyType_label")
    if value:
        return safe_text(value)
    raw = row.get("musicDifficultyType")
    if isinstance(raw, int):
        by_index = ["easy", "normal", "hard", "expert", "master", "append"]
        if 0 <= raw < len(by_index):
            return by_index[raw]
    return safe_text(raw or "unknown")


def honor_mission_progress(data: dict[str, Any]) -> dict[str, int]:
    progress: dict[str, int] = {}
    for row in data.get("userHonorMissions", []):
        if not isinstance(row, dict):
            continue
        mission_type = safe_text(row.get("honorMissionType"))
        value = row.get("progress")
        if mission_type and isinstance(value, int):
            progress[mission_type] = value
    return progress


def analyze_music(data: dict[str, Any], master: MasterCache) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    result_rows = compact_rows(data.get("compactUserMusicResults"))
    difficulty_levels: dict[tuple[int, str], int] = {}
    user_music_ids = {row.get("musicId") for row in data.get("userMusics", []) if isinstance(row, dict) and isinstance(row.get("musicId"), int)}
    user_music_ids.update(row.get("musicId") for row in result_rows if isinstance(row.get("musicId"), int))
    if not user_music_ids:
        user_music_ids = {row.get("id") for row in master.raw("musics") if isinstance(row.get("id"), int)}
    for item in master.raw("musicDifficulties"):
        music_id = item.get("musicId")
        difficulty = safe_text(item.get("musicDifficulty"))
        level = item.get("playLevel")
        if isinstance(music_id, int) and music_id in user_music_ids and difficulty and isinstance(level, int):
            difficulty_levels[(music_id, difficulty)] = level

    best_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    detail_by_music_id: dict[int, dict[str, Any]] = {}
    play_types: defaultdict[tuple[int, str], set[str]] = defaultdict(set)

    for row in result_rows:
        music_id = row.get("musicId")
        if not isinstance(music_id, int):
            continue
        detail = row.get("musicId_detail")
        if isinstance(detail, dict) and music_id not in detail_by_music_id:
            detail_by_music_id[music_id] = detail
        difficulty = normalize_difficulty(row)
        key = (music_id, difficulty)
        status = result_status(row)
        current = best_by_key.get(key)
        high_score = int(row.get("highScore") or 0)
        play_types[key].add(safe_text(row.get("playType_label") or row.get("playType")))
        if not current or RESULT_PRIORITY[status] > RESULT_PRIORITY[current["bestStatus"]]:
            best_by_key[key] = {"bestStatus": status, "highScore": high_score}
        elif current and high_score > int(current.get("highScore") or 0):
            current["highScore"] = high_score

    rows: list[dict[str, Any]] = []
    for music_id, difficulty in sorted(difficulty_levels, key=lambda key: (key[0], DIFFICULTY_ORDER.get(key[1], 99))):
        result = best_by_key.get((music_id, difficulty), {"bestStatus": "not_played", "highScore": 0})
        status = result["bestStatus"]
        music_master = master.row("musics", music_id)
        music_detail = detail_by_music_id.get(music_id, {})
        rows.append(
            {
                "musicId": music_id,
                "title": music_detail.get("title") or master.label("musics", music_id),
                "assetbundleName": music_detail.get("assetbundleName") or music_master.get("assetbundleName", ""),
                "difficulty": DIFFICULTY_LABELS.get(difficulty, difficulty),
                "difficultyKey": difficulty,
                "playLevel": difficulty_levels.get((music_id, difficulty), ""),
                "bestStatus": RESULT_LABELS.get(status, status),
                "clear": status in {"clear", "full_combo", "full_perfect"},
                "fullCombo": status in {"full_combo", "full_perfect"},
                "allPerfect": status == "full_perfect",
                "highScore": result.get("highScore", 0),
                "playTypes": ", ".join(sorted(play_types[(music_id, difficulty)])),
            }
        )

    rows.sort(key=lambda row: (int(row["musicId"]), DIFFICULTY_ORDER.get(safe_text(row["difficulty"]).lower(), 99)))

    by_difficulty: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        difficulty_name = safe_text(row["difficulty"])
        if row["clear"]:
            by_difficulty[difficulty_name]["Clear"] += 1
        if row["fullCombo"]:
            by_difficulty[difficulty_name]["Full Combo"] += 1
        if row["allPerfect"]:
            by_difficulty[difficulty_name]["All Perfect"] += 1
        if not row["clear"]:
            by_difficulty[difficulty_name][safe_text(row["bestStatus"])] += 1
    honor_progress = honor_mission_progress(data)
    fc_missions = {
        "Easy": "easy_full_combo",
        "Normal": "normal_full_combo",
        "Hard": "hard_full_combo",
        "Expert": "expert_full_combo",
        "Master": "master_full_combo",
        "Append": "append_full_combo",
    }
    for difficulty_name, mission_type in fc_missions.items():
        if mission_type in honor_progress:
            by_difficulty[difficulty_name]["Full Combo"] = honor_progress[mission_type]
    if "master_full_perfect" in honor_progress:
        by_difficulty["Master"]["All Perfect"] = honor_progress["master_full_perfect"]
    summary = {
        "musicDifficultyRows": len(rows),
        "clear": sum(counter.get("Clear", 0) for counter in by_difficulty.values()),
        "fullCombo": sum(counter.get("Full Combo", 0) for counter in by_difficulty.values()),
        "allPerfect": sum(counter.get("All Perfect", 0) for counter in by_difficulty.values()),
        "byDifficulty": {key: dict(counter) for key, counter in by_difficulty.items()},
    }
    return rows, summary


def parse_event_honor(description: str, honor_name: str) -> dict[str, Any] | None:
    text = description or ""
    rank_text = f"{text} {honor_name}"
    has_rank_word = bool(re.search(r"(拿下|前\s*[0-9,]+\s*名|TOP\s*[0-9,]+|TOP[0-9,]+)", rank_text, flags=re.IGNORECASE))
    if not has_rank_word:
        return None

    event_name = ""
    match = re.search(r"活動[「\"]([^」\"]+)[」\"]", text)
    if match:
        event_name = match.group(1).strip()
    if not event_name:
        return None

    chapter = ""
    chapter_match = re.search(r"章節\s*(\d+)", text)
    if chapter_match:
        chapter = chapter_match.group(1)
    elif "綜合" in text:
        chapter = "overall"

    rank_upper_bound = ""
    for pattern in (r"前\s*([0-9,]+)\s*名", r"TOP\s*[0-9,]+\s*-\s*([0-9,]+)", r"TOP\s*([0-9,]+)", r"TOP([0-9,]+)"):
        rank_match = re.search(pattern, rank_text, flags=re.IGNORECASE)
        if rank_match:
            rank_upper_bound = rank_match.group(1).replace(",", "")
            break

    if not event_name and not rank_upper_bound:
        return None
    return {
        "eventName": event_name,
        "chapter": chapter,
        "rankUpperBound": rank_upper_bound,
        "description": description,
    }


def event_honor_rows(data: dict[str, Any], master: MasterCache) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in data.get("userHonors", []):
        if isinstance(raw, dict):
            honor_id = raw.get("honorId")
            level = raw.get("level")
            obtained_at = raw.get("obtainedAt")
        elif isinstance(raw, list):
            honor_id = raw[0] if len(raw) > 0 else None
            level = raw[1] if len(raw) > 1 else None
            obtained_at = raw[2] if len(raw) > 2 else None
        else:
            continue

        honor = master.row("honors", honor_id)
        if not honor:
            continue
        levels = honor.get("levels")
        description = ""
        if isinstance(levels, list) and levels:
            selected = levels[min(max(int(level or 1) - 1, 0), len(levels) - 1)] if isinstance(level, int) else levels[0]
            if isinstance(selected, dict):
                description = safe_text(selected.get("description") or selected.get("name"))
        parsed = parse_event_honor(description, safe_text(honor.get("name")))
        if not parsed:
            continue
        rows.append(
            {
                "eventName": parsed["eventName"],
                "chapter": parsed["chapter"],
                "rankUpperBound": parsed["rankUpperBound"],
                "score": "",
                "rank": "",
                "source": "honor_title",
                "honorId": honor_id,
                "honorName": honor.get("name"),
                "obtainedAt": ms_to_iso(obtained_at),
                "description": parsed["description"],
            }
        )
    rows.sort(key=lambda row: row.get("obtainedAt", ""))
    return rows


def in_file_event_rows(data: dict[str, Any], master: MasterCache) -> list[dict[str, Any]]:
    rows = []
    for event in data.get("userEvents", []):
        if not isinstance(event, dict):
            continue
        event_id = event.get("eventId")
        rows.append(
            {
                "eventId": event_id,
                "eventName": event.get("eventId_label") or master.label("events", event_id),
                "chapter": "",
                "rank": "",
                "rankUpperBound": "",
                "score": event.get("eventPoint", ""),
                "source": "snapshot_userEvents",
                "obtainedAt": "",
                "description": "Score is eventPoint in this snapshot; rank is not stored here.",
            }
        )
    return rows


@dataclass
class FetchResult:
    url: str
    ok: bool
    status_code: int | None
    message: str


def fetch_json_url(url: str, timeout: int = 15) -> tuple[Any | None, FetchResult]:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": "pjsk-snapshot-analyzer/1.0"})
        if response.status_code != 200:
            return None, FetchResult(url, False, response.status_code, response.text[:200])
        return response.json(), FetchResult(url, True, response.status_code, "ok")
    except Exception as exc:  # noqa: BLE001 - report script should keep trying candidates.
        return None, FetchResult(url, False, None, str(exc))


def iter_event_objects(data: Any, context: dict[str, Any] | None = None) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    context = dict(context or {})
    if isinstance(data, dict):
        next_context = dict(context)
        if any(key in data for key in ("id", "event_id", "name", "start_at", "closed_at")) and any(
            key in data for key in ("player_top_100_rankings", "rankings", "player_rankings")
        ):
            next_context.update(
                {
                    "eventId": data.get("id") or data.get("event_id"),
                    "eventName": data.get("name"),
                    "startAt": data.get("start_at"),
                }
            )
        if "rank" in data and "score" in data:
            yield data, next_context
        for value in data.values():
            yield from iter_event_objects(value, next_context)
    elif isinstance(data, list):
        for item in data:
            yield from iter_event_objects(item, context)


def profile_id_from_player(player: dict[str, Any]) -> int | None:
    for path in (
        ("last_player_info", "profile", "id"),
        ("profile", "id"),
        ("player", "id"),
        ("user", "id"),
    ):
        value: Any = player
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if isinstance(value, int):
            return value
    for key in ("id", "userId", "user_id", "profile_id"):
        value = player.get(key)
        if isinstance(value, int):
            return value
    return None


def normalize_hisekai_rows(data: Any, user_id: int, source: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for player, context in iter_event_objects(data):
        player_id = profile_id_from_player(player)
        if player_id is not None and player_id != user_id:
            continue
        event_id = player.get("eventId") or player.get("event_id") or context.get("eventId")
        event_name = player.get("eventName") or player.get("event_name") or context.get("eventName")
        if player_id is None and not (event_id or event_name):
            continue
        rank = player.get("rank")
        score = player.get("score") or player.get("eventPoint") or player.get("event_point")
        if rank in (None, "") and score in (None, ""):
            continue
        rows.append(
            {
                "eventId": event_id,
                "eventName": event_name,
                "chapter": player.get("chapter") or player.get("chapterId") or "",
                "rank": rank if rank is not None else "",
                "rankUpperBound": "",
                "score": score if score is not None else "",
                "source": source,
                "obtainedAt": player.get("last_played_at") or context.get("startAt") or "",
                "description": "",
            }
        )
    return rows


def parse_api_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def event_list_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict) and isinstance(item.get("id"), int)]
    if isinstance(data, dict):
        for key in ("events", "data", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict) and isinstance(item.get("id"), int)]
    return []


def fetch_top100_history_rows(args: argparse.Namespace, user_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    event_list_url = args.event_list_url.format(server=args.server, user_id=user_id)
    data, result = fetch_json_url(event_list_url, timeout=args.timeout)
    logs = [result.__dict__]
    if data is None:
        return [], logs

    now = datetime.now(timezone.utc)
    events = []
    for event in event_list_items(data):
        start_at = parse_api_time(event.get("start_at"))
        if start_at and start_at > now:
            continue
        events.append(event)

    events.sort(key=lambda item: int(item.get("id") or 0), reverse=True)
    if args.max_events > 0:
        events = events[: args.max_events]

    rows: list[dict[str, Any]] = []
    for event in events:
        event_id = event.get("id")
        url = args.event_top100_url.format(server=args.server, user_id=user_id, event_id=event_id)
        top_data, top_result = fetch_json_url(url, timeout=args.timeout)
        logs.append(top_result.__dict__)
        if top_data is None:
            continue
        matched = normalize_hisekai_rows(top_data, user_id, url)
        for row in matched:
            row["eventId"] = row.get("eventId") or event_id
            row["eventName"] = row.get("eventName") or event.get("name")
            row["source"] = "hisekai_top100_history"
        rows.extend(matched)
    return rows, logs


def fetch_hisekai_rows(args: argparse.Namespace, user_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    logs: list[dict[str, Any]] = []

    urls = []
    if args.current_rank_url:
        urls.extend(args.current_rank_url)
    else:
        urls.extend(DEFAULT_CURRENT_RANK_URLS)
    if args.history_url:
        urls.extend(args.history_url)
    else:
        urls.extend(DEFAULT_HISTORY_URLS)

    seen: set[str] = set()
    for template in urls:
        url = template.format(server=args.server, user_id=user_id)
        if url in seen:
            continue
        seen.add(url)
        data, result = fetch_json_url(url, timeout=args.timeout)
        logs.append(result.__dict__)
        if data is None:
            continue
        rows.extend(normalize_hisekai_rows(data, user_id, url))

    if args.fetch_top100_history:
        history_rows, history_logs = fetch_top100_history_rows(args, user_id)
        rows.extend(history_rows)
        logs.extend(history_logs)
    return rows, logs


def build_profile_report(args: argparse.Namespace) -> int:
    input_path = args.json_file
    output_dir = ensure_output_dir(args.output_dir)
    master = MasterCache(args.master_cache, args.locale)
    data = read_json(input_path)
    if not isinstance(data, dict):
        raise ValueError("Profile JSON top-level value must be an object.")

    user_id = user_id_from_snapshot(data, input_path)
    player_name = ""
    if isinstance(data.get("userGamedata"), dict):
        player_name = safe_text(data["userGamedata"].get("name"))

    card_rows, card_summary = analyze_cards(data, master)
    music_rows, music_summary = analyze_music(data, master)
    event_rows = in_file_event_rows(data, master) + event_honor_rows(data, master)
    fetch_logs: list[dict[str, Any]] = []

    if args.fetch_hisekai:
        if not user_id:
            print("warning: user id not found; skipped HiSekai fetch", file=sys.stderr)
        else:
            fetched_rows, fetch_logs = fetch_hisekai_rows(args, user_id)
            event_rows = fetched_rows + event_rows

    # Prefer exact fetched rows before honor-title fallback when sorting.
    event_rows.sort(key=lambda row: (safe_text(row.get("eventName")), safe_text(row.get("chapter")), safe_text(row.get("source"))))

    write_csv(
        output_dir / "profile_events.csv",
        event_rows,
        ["eventId", "eventName", "chapter", "rank", "rankUpperBound", "score", "source", "honorId", "honorName", "obtainedAt", "description"],
    )
    write_csv(
        output_dir / "profile_cards.csv",
        card_rows,
        [
            "cardId",
            "cardName",
            "character",
            "rarity",
            "attr",
            "level",
            "skillLevel",
            "skillExp",
            "masterRank",
            "specialTrainingStatus",
            "defaultImage",
            "duplicateCount",
            "obtainedAt",
            "episodesRead",
            "episodesTotal",
            "episodeStatuses",
        ],
    )
    write_csv(
        output_dir / "profile_music_status.csv",
        music_rows,
        [
            "musicId",
            "title",
            "assetbundleName",
            "difficulty",
            "difficultyKey",
            "playLevel",
            "bestStatus",
            "clear",
            "fullCombo",
            "allPerfect",
            "highScore",
            "playTypes",
        ],
    )

    summary = {
        "source": str(input_path),
        "userId": user_id,
        "playerName": player_name,
        "events": {"rows": len(event_rows), "fetchLogs": fetch_logs},
        "cards": card_summary,
        "music": music_summary,
    }
    write_json(output_dir / "profile_summary.json", summary)
    if fetch_logs:
        write_json(output_dir / "hisekai_fetch_log.json", fetch_logs)

    card_graph = draw_card_summary(card_summary, output_dir)
    music_graph = draw_music_status_query(music_rows, output_dir, mode="difficulty", value="master")
    events_graph = draw_profile_events_image(event_rows, output_dir)
    overview_graph = draw_profile_overview_image(data, music_summary, master, output_dir)

    stats = [
        ("玩家", player_name or user_id or ""),
        ("卡片", len(card_rows)),
        ("活動/稱號列", len(event_rows)),
        ("Clear", music_summary["clear"]),
        ("Full Combo", music_summary["fullCombo"]),
        ("All Perfect", music_summary["allPerfect"]),
    ]
    stat_html = "".join(f"<div class=\"stat\"><span>{html.escape(label)}</span><b>{html.escape(safe_text(value))}</b></div>" for label, value in stats)

    body = f"""
<h1>Player Snapshot Report</h1>
<p class="meta">Source: <code>{html.escape(str(input_path))}</code></p>
<section class="stats">{stat_html}</section>
<h2>活動分數與排名</h2>
<p class="note">`snapshot_userEvents` 只含分數；`honor_title` 是稱號可推得的排名區間；啟用 <code>--fetch-hisekai</code> 且 API 有回資料時才會出現精確排名與分數。</p>
{render_table(event_rows, [('eventName', '活動'), ('chapter', '章節'), ('rank', '排名'), ('rankUpperBound', '排名上限'), ('score', '分數'), ('source', '來源')], limit=120)}
<h2>卡片狀態</h2>
<img src="{html.escape(card_graph)}" alt="cards by rarity">
<h2>Suite 圖片摘要</h2>
<div class="gallery">
<figure><img src="{html.escape(music_graph)}" alt="music status"><figcaption>{html.escape(music_graph)}</figcaption></figure>
<figure><img src="{html.escape(events_graph)}" alt="event rankings"><figcaption>{html.escape(events_graph)}</figcaption></figure>
<figure><img src="{html.escape(overview_graph)}" alt="profile overview"><figcaption>{html.escape(overview_graph)}</figcaption></figure>
</div>
{render_table(card_rows, [('cardId', 'ID'), ('cardName', '卡片'), ('character', '角色'), ('rarity', '稀有度'), ('level', 'Lv'), ('skillLevel', '技能'), ('masterRank', '大師'), ('specialTrainingStatus', '特訓'), ('defaultImage', '圖面')], limit=80)}
<h2>歌曲狀態</h2>
{render_table(music_rows, [('musicId', 'ID'), ('title', '歌曲'), ('difficulty', '難度'), ('bestStatus', '最佳狀態'), ('highScore', '最高分'), ('playTypes', '模式')], limit=120)}
<p class="note">CSV/JSON: <code>profile_events.csv</code>, <code>profile_cards.csv</code>, <code>profile_music_status.csv</code>, <code>profile_summary.json</code></p>
"""
    (output_dir / "profile_report.html").write_text(html_page("Player Snapshot Report", body), encoding="utf-8")

    print(f"wrote {output_dir / 'profile_report.html'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-cache", type=Path, default=DEFAULT_MASTER_CACHE)
    parser.add_argument("--locale", default="tc")

    subparsers = parser.add_subparsers(dest="command", required=True)

    mysekai = subparsers.add_parser("mysekai", help="Summarize MySekai harvest maps and visitors.")
    mysekai.add_argument("json_file", type=Path)
    mysekai.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / "mysekai")
    mysekai.add_argument("--no-maps", action="store_true", help="Skip PNG map rendering.")
    mysekai.set_defaults(func=build_mysekai_report)

    profile = subparsers.add_parser("profile", help="Summarize profile snapshot events, cards, and music results.")
    profile.add_argument("json_file", type=Path)
    profile.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / "profile")
    profile.add_argument("--fetch-hisekai", action="store_true", help="Try HiSekai endpoints for exact ranking rows.")
    profile.add_argument("--server", default="tw")
    profile.add_argument("--timeout", type=int, default=15)
    profile.add_argument("--current-rank-url", action="append", help="URL template with {server} and {user_id}.")
    profile.add_argument("--history-url", action="append", help="URL template with {server} and {user_id}.")
    profile.add_argument("--fetch-top100-history", action="store_true", help="Scan event/list and event/{event_id}/top100 for exact top100 rows.")
    profile.add_argument("--max-events", type=int, default=0, help="Limit top100 history scan; 0 scans all started events.")
    profile.add_argument("--event-list-url", default=DEFAULT_EVENT_LIST_URL, help="URL template with {server}.")
    profile.add_argument("--event-top100-url", default=DEFAULT_EVENT_TOP100_URL, help="URL template with {server} and {event_id}.")
    profile.set_defaults(func=build_profile_report)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
