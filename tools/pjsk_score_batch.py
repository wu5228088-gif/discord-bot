from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests

from tools.analyze_sus_theory import SusTheoryAnalyzer


DIFFICULTY_ORDER = ["easy", "normal", "hard", "expert", "master", "append"]
ASSETS_HOST = "https://assets-direct.unipjsk.com"
BONUS_MULTIPLIERS = {1: 5, 2: 10, 3: 15, 4: 20, 5: 25, 6: 27, 7: 29, 8: 31, 9: 33, 10: 35}
NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def default_length_xlsx() -> Path:
    return Path(os.getenv("PJSK_LENGTH_XLSX", r"C:\Users\USER\Desktop\PJSK_純數字計算表.xlsx"))


def default_length_overrides(data_dir: Path | None = None) -> Path:
    root = data_dir or Path(os.getenv("BOT_DATA_DIR") or Path.cwd())
    return Path(os.getenv("PJSK_LENGTH_OVERRIDES", str(root / "pjsk_length_overrides.csv")))


def default_master_dir() -> Path:
    env_path = os.getenv("PJSK_SCORE_MASTER_DIR")
    if env_path:
        return Path(env_path)
    local = Path(".local") / "unipjsk_master"
    if local.exists():
        return local
    return Path("master_cache") / "tc"


def cache_path(data_dir: Path) -> Path:
    return data_dir / "pjsk_score_analysis.json"


def csv_path(data_dir: Path) -> Path:
    return data_dir / "reports" / "pjsk_score_ranking.csv"


def sus_cache_dir(data_dir: Path) -> Path:
    return data_dir / "cache" / "pjsk_sus"


def col_to_index(ref: str) -> int:
    value = 0
    for ch in ref:
        if ch.isalpha():
            value = value * 26 + ord(ch.upper()) - ord("A") + 1
    return value


def read_xlsx_rows(path: Path) -> list[dict[int, str]]:
    if not path.exists():
        return []
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("a:si", NS):
                shared.append("".join(t.text or "" for t in item.findall(".//a:t", NS)))

        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        rows: list[dict[int, str]] = []
        for row in root.findall(".//a:row", NS):
            values: dict[int, str] = {}
            for cell in row.findall("a:c", NS):
                ref = cell.attrib.get("r", "")
                col = col_to_index(ref)
                value = ""
                value_node = cell.find("a:v", NS)
                inline_node = cell.find("a:is", NS)
                if value_node is not None and value_node.text is not None:
                    value = value_node.text
                    if cell.attrib.get("t") == "s":
                        value = shared[int(value)]
                elif inline_node is not None:
                    value = "".join(t.text or "" for t in inline_node.findall(".//a:t", NS))
                values[col] = value
            rows.append(values)
        return rows


def as_float(value: str | None) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except ValueError:
        return None


def normalize_title(value: str) -> str:
    return "".join(str(value).split()).lower()


def apply_length_overrides(
    by_difficulty: dict[tuple[str, str], float],
    by_title_values: dict[str, list[float]],
    override_csv: Path | None,
) -> None:
    if not override_csv or not override_csv.exists():
        return
    with override_csv.open(encoding="utf-8-sig", newline="") as fp:
        for row in csv.DictReader(fp):
            title = (row.get("title") or row.get("曲名") or "").strip()
            difficulty = (row.get("difficulty") or row.get("難度") or "").strip().lower()
            multiplier = as_float(row.get("length_multiplier") or row.get("長度倍率"))
            if not title or multiplier is None:
                continue
            normalized = normalize_title(title)
            if difficulty in DIFFICULTY_ORDER:
                by_difficulty[(normalized, difficulty)] = multiplier
            by_title_values[normalized] = [multiplier]


def load_length_multipliers(
    path: Path | None = None,
    override_csv: Path | None = None,
) -> tuple[dict[tuple[str, str], float], dict[str, float]]:
    rows = read_xlsx_rows(path or default_length_xlsx())
    by_difficulty: dict[tuple[str, str], float] = {}
    by_title_values: dict[str, list[float]] = {}
    for row in rows:
        title = (row.get(1) or "").strip()
        difficulty = (row.get(2) or "").strip().lower()
        multiplier = as_float(row.get(9))
        if not title or difficulty not in DIFFICULTY_ORDER or multiplier is None:
            continue
        normalized = normalize_title(title)
        by_difficulty[(normalized, difficulty)] = multiplier
        by_title_values.setdefault(normalized, []).append(multiplier)
    apply_length_overrides(by_difficulty, by_title_values, override_csv)
    # Length multiplier is song-level for this use case. Keep the exact
    # difficulty match first, and fall back to the first row for the same title.
    by_title = {title: values[0] for title, values in by_title_values.items() if values}
    return by_difficulty, by_title


def load_length_multipliers_with_ids(
    path: Path | None = None,
    override_csv: Path | None = None,
) -> tuple[dict[tuple[str, str], float], dict[str, float], dict[tuple[int, str], float], dict[int, float]]:
    by_difficulty, by_title = load_length_multipliers(path, None)
    by_id_difficulty: dict[tuple[int, str], float] = {}
    by_id_values: dict[int, list[float]] = {}
    if override_csv and override_csv.exists():
        with override_csv.open(encoding="utf-8-sig", newline="") as fp:
            for row in csv.DictReader(fp):
                multiplier = as_float(row.get("length_multiplier"))
                if multiplier is None:
                    continue
                title = (row.get("title") or "").strip()
                difficulty = (row.get("difficulty") or "").strip().lower()
                music_id_raw = (row.get("music_id") or row.get("id") or "").strip()
                try:
                    music_id = int(music_id_raw) if music_id_raw else None
                except ValueError:
                    music_id = None
                if difficulty in DIFFICULTY_ORDER:
                    if title:
                        by_difficulty[(normalize_title(title), difficulty)] = multiplier
                    if music_id is not None:
                        by_id_difficulty[(music_id, difficulty)] = multiplier
                if title:
                    by_title[normalize_title(title)] = multiplier
                if music_id is not None:
                    by_id_values[music_id] = [multiplier]
    by_id = {music_id: values[0] for music_id, values in by_id_values.items() if values}
    return by_difficulty, by_title, by_id_difficulty, by_id


def load_master(master_dir: Path) -> tuple[dict[int, dict[str, Any]], list[dict[str, Any]]]:
    with (master_dir / "musics.json").open(encoding="utf-8") as fp:
        musics = {int(row["id"]): row for row in json.load(fp)}
    with (master_dir / "musicDifficulties.json").open(encoding="utf-8") as fp:
        difficulties = json.load(fp)
    difficulties.sort(key=lambda row: (int(row["musicId"]), DIFFICULTY_ORDER.index(row["musicDifficulty"])))
    return musics, difficulties


def sus_url(music_id: int, difficulty: str, assets_host: str = ASSETS_HOST) -> str:
    return f"{assets_host.rstrip('/')}/startapp/music/music_score/{music_id:04d}_01/{difficulty}"


def download_sus(
    music_id: int,
    difficulty: str,
    target: Path,
    *,
    force: bool = False,
    assets_host: str = ASSETS_HOST,
    timeout: int = 30,
) -> bool:
    if target.exists() and not force:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(sus_url(music_id, difficulty, assets_host), timeout=timeout)
    response.raise_for_status()
    text = response.text
    if "#" not in text[:1000]:
        raise ValueError(f"Downloaded data does not look like SUS: {music_id:04d} {difficulty}")
    target.write_text(text, encoding="utf-8")
    return True


def cached_sus_keys(cache_dir: Path) -> set[tuple[int, str]]:
    keys: set[tuple[int, str]] = set()
    for path in cache_dir.glob("*.sus"):
        match = re.match(r"^(\d{4})_(\w+)\.sus$", path.name)
        if match and match.group(2) in DIFFICULTY_ORDER:
            keys.add((int(match.group(1)), match.group(2)))
    return keys


def resolve_length_multiplier(
    music: dict[str, Any],
    difficulty_name: str,
    length_lookup: tuple[dict[tuple[str, str], float], dict[str, float]]
    | tuple[dict[tuple[str, str], float], dict[str, float], dict[tuple[int, str], float], dict[int, float]],
) -> tuple[float | None, str | None]:
    if len(length_lookup) == 4:
        length_by_difficulty, length_by_title, length_by_id_difficulty, length_by_id = length_lookup
    else:
        length_by_difficulty, length_by_title = length_lookup
        length_by_id_difficulty = {}
        length_by_id = {}

    normalized_title = normalize_title(music["title"])
    music_id = int(music["id"])
    length_multiplier = length_by_id_difficulty.get((music_id, difficulty_name))
    length_source = "music_id+difficulty" if length_multiplier is not None else None
    if length_multiplier is None:
        length_multiplier = length_by_difficulty.get((normalized_title, difficulty_name))
        length_source = "difficulty" if length_multiplier is not None else None
    if length_multiplier is None:
        length_multiplier = length_by_id.get(music_id)
        length_source = "music_id" if length_multiplier is not None else None
    if length_multiplier is None:
        length_multiplier = length_by_title.get(normalized_title)
        length_source = "title" if length_multiplier is not None else None
    return length_multiplier, length_source


def analyze_chart(
    sus_file: Path,
    difficulty: dict[str, Any],
    music: dict[str, Any],
    length_lookup: tuple[dict[tuple[str, str], float], dict[str, float]]
    | tuple[dict[tuple[str, str], float], dict[str, float], dict[tuple[int, str], float], dict[int, float]],
) -> dict[str, Any]:
    analyzer = SusTheoryAnalyzer(sus_file.read_text(encoding="utf-8"))
    difficulty_name = difficulty["musicDifficulty"]
    official_combo = int(difficulty.get("totalNoteCount") or 0)
    result = analyzer.analyze(int(difficulty["playLevel"]), official_combo=official_combo)
    result_no_fever = analyzer.analyze(
        int(difficulty["playLevel"]),
        official_combo=official_combo,
        fever_multiplier=1.0,
    )
    music_id = int(music["id"])
    length_multiplier, length_source = resolve_length_multiplier(music, difficulty_name, length_lookup)
    combo_match = official_combo == int(result["combo_count_used"])
    return {
        "music_id": music_id,
        "title": music["title"],
        "difficulty": difficulty_name,
        "level": int(difficulty["playLevel"]),
        "official_combo": official_combo,
        "parsed_combo": int(result["combo_count_used"]),
        "score_event_count": int(result["score_event_count"]),
        "score_event_combo_delta": int(result["score_event_combo_delta"]),
        "combo_match": combo_match,
        "total_weight": round(float(result["total_weight"]), 4),
        "score_power_multiplier": float(result["score_power_multiplier"]),
        "score_power_multiplier_min": float(result["score_power_multiplier_min"]),
        "score_power_multiplier_max": float(result["score_power_multiplier_max"]),
        "score_base_power_multiplier": float(result["score_base_power_multiplier"]),
        "skill_score_terms": [float(value) for value in result["skill_score_terms"]],
        "skill_score_terms_min": [float(value) for value in result["skill_score_terms_min"]],
        "skill_score_terms_max": [float(value) for value in result["skill_score_terms_max"]],
        "score_power_multiplier_no_fever": float(result_no_fever["score_power_multiplier"]),
        "score_power_multiplier_min_no_fever": float(result_no_fever["score_power_multiplier_min"]),
        "score_power_multiplier_max_no_fever": float(result_no_fever["score_power_multiplier_max"]),
        "score_base_power_multiplier_no_fever": float(result_no_fever["score_base_power_multiplier"]),
        "skill_score_terms_no_fever": [float(value) for value in result_no_fever["skill_score_terms"]],
        "skill_score_terms_min_no_fever": [float(value) for value in result_no_fever["skill_score_terms_min"]],
        "skill_score_terms_max_no_fever": [float(value) for value in result_no_fever["skill_score_terms_max"]],
        "base_power_multiplier": float(result["base_power_multiplier"]),
        "length_multiplier": length_multiplier,
        "length_multiplier_source": length_source,
        "skill_coverages": result["skill_coverages"],
        "fever": result["fever"],
        "kind_counts": result["kind_counts"],
        "sus_file": str(sus_file),
    }


def build_analysis(
    data_dir: Path,
    *,
    master_dir: Path,
    length_xlsx: Path | None = None,
    length_overrides: Path | None = None,
    force_download: bool = False,
    difficulties: set[str] | None = None,
    limit: int | None = None,
    assets_host: str = ASSETS_HOST,
    sleep_sec: float = 0.03,
) -> dict[str, Any]:
    musics, master_difficulties = load_master(master_dir)
    length_lookup = load_length_multipliers_with_ids(length_xlsx, length_overrides or default_length_overrides(data_dir))
    charts: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    cache_dir = sus_cache_dir(data_dir)
    previous_charts: dict[tuple[int, str], dict[str, Any]] = {}
    if not force_download:
        previous_payload = load_analysis(data_dir)
        if previous_payload:
            for chart in previous_payload.get("charts", []):
                try:
                    previous_charts[(int(chart["music_id"]), str(chart["difficulty"]))] = chart
                except (KeyError, TypeError, ValueError):
                    continue

    wanted = [row for row in master_difficulties if difficulties is None or row["musicDifficulty"] in difficulties]
    known_keys = {(int(row["musicId"]), row["musicDifficulty"]) for row in wanted}
    all_master_keys = {(int(row["musicId"]), row["musicDifficulty"]) for row in master_difficulties}
    for music_id, difficulty_name in sorted(cached_sus_keys(cache_dir)):
        if difficulties is not None and difficulty_name not in difficulties:
            continue
        if (music_id, difficulty_name) in all_master_keys:
            continue
        music = musics.get(music_id)
        if not music:
            continue
        wanted.append(
            {
                "musicId": music_id,
                "musicDifficulty": difficulty_name,
                "playLevel": 0,
                "totalNoteCount": 0,
            }
        )
        known_keys.add((music_id, difficulty_name))
    if limit:
        wanted = wanted[:limit]

    for index, difficulty in enumerate(wanted, start=1):
        music_id = int(difficulty["musicId"])
        music = musics.get(music_id)
        if not music:
            continue
        difficulty_name = difficulty["musicDifficulty"]
        sus_file = cache_dir / f"{music_id:04d}_{difficulty_name}.sus"
        cached_chart = previous_charts.get((music_id, difficulty_name))
        if cached_chart is not None:
            reused_chart = dict(cached_chart)
            length_multiplier, length_source = resolve_length_multiplier(music, difficulty_name, length_lookup)
            reused_chart["title"] = music["title"]
            reused_chart["level"] = int(difficulty["playLevel"])
            reused_chart["official_combo"] = int(
                difficulty.get("totalNoteCount") or reused_chart.get("official_combo") or 0
            )
            reused_chart["combo_match"] = reused_chart["official_combo"] == int(reused_chart.get("parsed_combo") or 0)
            reused_chart["length_multiplier"] = length_multiplier
            reused_chart["length_multiplier_source"] = length_source
            charts.append(reused_chart)
            print(f"正在處理 [{index}/{len(wanted)}]: ID {music_id:04d} - {difficulty_name}（沿用快取）")
            continue
        try:
            downloaded = download_sus(
                music_id,
                difficulty_name,
                sus_file,
                force=force_download,
                assets_host=assets_host,
            )
            if downloaded and sleep_sec > 0:
                time.sleep(sleep_sec)
            charts.append(analyze_chart(sus_file, difficulty, music, length_lookup))
        except Exception as exc:  # noqa: BLE001 - batch output should keep going and report per-chart failures.
            errors.append(
                {
                    "music_id": music_id,
                    "title": music.get("title", ""),
                    "difficulty": difficulty_name,
                    "error": str(exc),
                }
            )

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "assets_host": assets_host,
        "length_xlsx": str(length_xlsx or default_length_xlsx()),
        "length_overrides": str(length_overrides or default_length_overrides(data_dir)),
        "chart_count": len(charts),
        "error_count": len(errors),
        "charts": charts,
        "errors": errors,
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_path(data_dir).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(payload, data_dir)
    write_missing_length_template(payload, data_dir)
    return payload


def load_analysis(data_dir: Path) -> dict[str, Any] | None:
    path = cache_path(data_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_skill_multipliers(skill_multipliers: float | list[float] | tuple[float, ...] | None = None) -> list[float]:
    if skill_multipliers is None:
        return [3.0] * 6
    if isinstance(skill_multipliers, (int, float)):
        return [float(skill_multipliers)] * 6
    values = [float(value) for value in skill_multipliers]
    if not values:
        return [3.0] * 6
    if len(values) == 1:
        return values * 6
    return (values + [values[-1]] * 6)[:6]


def score_power_multiplier_for_chart(
    chart: dict[str, Any],
    skill_multipliers: float | list[float] | tuple[float, ...] | None = None,
    *,
    use_fever: bool = True,
) -> float:
    multipliers = normalize_skill_multipliers(skill_multipliers)
    suffix = "" if use_fever else "_no_fever"
    base = chart.get(f"score_base_power_multiplier{suffix}")
    terms = chart.get(f"skill_score_terms{suffix}")
    if base is None or not terms:
        default_score = float(chart.get(f"score_power_multiplier{suffix}", chart["score_power_multiplier"]))
        if multipliers == [3.0] * 6:
            return default_score
        return default_score
    return float(base) + sum(float(term) * (multipliers[i] - 1.0) for i, term in enumerate(terms[:6]))


def score_power_multiplier_range_for_chart(
    chart: dict[str, Any],
    skill_multipliers: float | list[float] | tuple[float, ...] | None = None,
    *,
    use_fever: bool = True,
) -> tuple[float, float]:
    multipliers = normalize_skill_multipliers(skill_multipliers)
    suffix = "" if use_fever else "_no_fever"
    base = chart.get(f"score_base_power_multiplier{suffix}")
    min_terms = chart.get(f"skill_score_terms_min{suffix}")
    max_terms = chart.get(f"skill_score_terms_max{suffix}")
    if base is None or not min_terms or not max_terms:
        value = score_power_multiplier_for_chart(chart, multipliers, use_fever=use_fever)
        return value, value
    low = float(base) + sum(float(term) * (multipliers[i] - 1.0) for i, term in enumerate(min_terms[:6]))
    high = float(base) + sum(float(term) * (multipliers[i] - 1.0) for i, term in enumerate(max_terms[:6]))
    return min(low, high), max(low, high)


def calculate_event_points(
    chart: dict[str, Any],
    team_power: int,
    event_multiplier: float,
    bonus: int = 5,
    skill_multipliers: float | list[float] | tuple[float, ...] | None = None,
    active_bonus_power_multiplier: float = 0.0,
    use_fever: bool = True,
) -> dict[str, Any]:
    bonus_multiplier = BONUS_MULTIPLIERS.get(int(bonus), BONUS_MULTIPLIERS[5])
    score_power_multiplier = score_power_multiplier_for_chart(chart, skill_multipliers, use_fever=use_fever)
    score_power_multiplier_min, score_power_multiplier_max = score_power_multiplier_range_for_chart(
        chart,
        skill_multipliers,
        use_fever=use_fever,
    )
    score_power_multiplier_total = score_power_multiplier + active_bonus_power_multiplier
    score_power_multiplier_min_total = score_power_multiplier_min + active_bonus_power_multiplier
    score_power_multiplier_max_total = score_power_multiplier_max + active_bonus_power_multiplier
    score = score_power_multiplier_total * team_power
    score_min = score_power_multiplier_min_total * team_power
    score_max = score_power_multiplier_max_total * team_power
    length_multiplier = chart.get("length_multiplier")
    length = float(length_multiplier) if length_multiplier is not None else 1.0
    base_pt = math.floor(length * (123 + math.floor(score / 17000)) * event_multiplier)
    base_pt_min = math.floor(length * (123 + math.floor(score_min / 17000)) * event_multiplier)
    base_pt_max = math.floor(length * (123 + math.floor(score_max / 17000)) * event_multiplier)
    return {
        "score": score,
        "score_min": score_min,
        "score_max": score_max,
        "score_power_multiplier": score_power_multiplier_total,
        "score_power_multiplier_min": score_power_multiplier_min_total,
        "score_power_multiplier_max": score_power_multiplier_max_total,
        "note_score_power_multiplier": score_power_multiplier,
        "active_bonus_power_multiplier": active_bonus_power_multiplier,
        "use_fever": use_fever,
        "base_pt": base_pt,
        "base_pt_min": base_pt_min,
        "base_pt_max": base_pt_max,
        "event_pt": base_pt * bonus_multiplier,
        "event_pt_min": base_pt_min * bonus_multiplier,
        "event_pt_max": base_pt_max * bonus_multiplier,
        "bonus_multiplier": bonus_multiplier,
        "length_multiplier": length_multiplier,
        "length_missing": length_multiplier is None,
    }


def rank_charts(
    analysis: dict[str, Any],
    *,
    team_power: int,
    event_multiplier: float,
    bonus: int = 5,
    skill_multipliers: float | list[float] | tuple[float, ...] | None = None,
    active_bonus_power_multiplier: float = 0.0,
    use_fever: bool = True,
    difficulty: str = "all",
    sort_by: str = "event_pt",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for chart in analysis.get("charts", []):
        if difficulty != "all" and chart["difficulty"] != difficulty:
            continue
        calc = calculate_event_points(
            chart,
            team_power,
            event_multiplier,
            bonus,
            skill_multipliers,
            active_bonus_power_multiplier,
            use_fever,
        )
        skill_total = sum(float(row["covered_weight"]) for row in chart.get("skill_coverages", []))
        rows.append(
            {
                **chart,
                **calc,
                "skill_coverage_pct_total": skill_total / chart["total_weight"] * 100 if chart["total_weight"] else 0.0,
            }
        )
    rows.sort(
        key=lambda row: (
            row.get(f"{sort_by}_max", row.get(sort_by, 0)),
            row.get("score_power_multiplier_max", row["score_power_multiplier"]),
        ),
        reverse=True,
    )
    return rows


def find_chart(analysis: dict[str, Any], query: str, difficulty: str) -> dict[str, Any] | None:
    query_norm = normalize_title(query)
    for chart in analysis.get("charts", []):
        if chart["difficulty"] != difficulty:
            continue
        if str(chart["music_id"]) == query or normalize_title(chart["title"]) == query_norm:
            return chart
    matches = [
        chart
        for chart in analysis.get("charts", [])
        if chart["difficulty"] == difficulty and query_norm in normalize_title(chart["title"])
    ]
    return matches[0] if len(matches) == 1 else None


def write_csv(analysis: dict[str, Any], data_dir: Path) -> Path:
    path = csv_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "music_id",
        "title",
        "difficulty",
        "level",
        "official_combo",
        "parsed_combo",
        "combo_match",
        "score_event_count",
        "score_event_combo_delta",
        "total_weight",
        "score_power_multiplier",
        "score_power_multiplier_min",
        "score_power_multiplier_max",
        "score_base_power_multiplier",
        "length_multiplier",
        "skill_coverage_pct_total",
        "fever_coverage_pct",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        for chart in analysis.get("charts", []):
            skill_total = sum(float(row["covered_weight"]) for row in chart.get("skill_coverages", []))
            writer.writerow(
                {
                    "music_id": chart["music_id"],
                    "title": chart["title"],
                    "difficulty": chart["difficulty"],
                    "level": chart["level"],
                    "official_combo": chart["official_combo"],
                    "parsed_combo": chart["parsed_combo"],
                    "combo_match": chart["combo_match"],
                    "score_event_count": chart.get("score_event_count"),
                    "score_event_combo_delta": chart.get("score_event_combo_delta"),
                    "total_weight": chart["total_weight"],
                    "score_power_multiplier": chart["score_power_multiplier"],
                    "score_power_multiplier_min": chart.get("score_power_multiplier_min"),
                    "score_power_multiplier_max": chart.get("score_power_multiplier_max"),
                    "score_base_power_multiplier": chart.get("score_base_power_multiplier"),
                    "length_multiplier": chart.get("length_multiplier"),
                    "skill_coverage_pct_total": skill_total / chart["total_weight"] * 100 if chart["total_weight"] else 0.0,
                    "fever_coverage_pct": chart.get("fever", {}).get("coverage_pct"),
                }
            )
    return path


def write_missing_length_template(analysis: dict[str, Any], data_dir: Path) -> Path:
    path = data_dir / "pjsk_length_overrides.csv"
    existing: dict[str, str] = {}
    existing_rows: dict[tuple[str, str], dict[str, str]] = {}
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as fp:
            for row in csv.DictReader(fp):
                music_id = (row.get("music_id") or "").strip()
                title = (row.get("title") or "").strip()
                difficulty = (row.get("difficulty") or "").strip().lower()
                if title:
                    existing[normalize_title(title)] = row.get("length_multiplier") or ""
                if row.get("length_multiplier"):
                    existing_rows[(music_id or normalize_title(title), difficulty)] = {
                        "music_id": music_id,
                        "title": title,
                        "difficulty": difficulty,
                        "length_multiplier": row.get("length_multiplier") or "",
                        "missing_difficulties": row.get("missing_difficulties") or "",
                    }

    missing_titles: dict[str, dict[str, Any]] = {}
    for chart in analysis.get("charts", []):
        if chart.get("length_multiplier") is not None:
            continue
        normalized = normalize_title(chart["title"])
        missing_titles.setdefault(
            normalized,
            {"music_id": chart["music_id"], "title": chart["title"], "difficulties": set()},
        )
        missing_titles[normalized]["difficulties"].add(chart["difficulty"])

    rows = list(existing_rows.values())
    for normalized, item in sorted(missing_titles.items(), key=lambda pair: (pair[1]["music_id"], pair[1]["title"])):
        key = (str(item["music_id"]), "")
        if key in existing_rows:
            continue
        rows.append(
            {
                "music_id": item["music_id"],
                "title": item["title"],
                "difficulty": "",
                "length_multiplier": existing.get(normalized, ""),
                "missing_difficulties": "/".join(d for d in DIFFICULTY_ORDER if d in item["difficulties"]),
            }
        )

    with path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=["music_id", "title", "difficulty", "length_multiplier", "missing_difficulties"],
        )
        writer.writeheader()
        writer.writerows(rows)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Download and analyze PJSK SUS score data.")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("BOT_DATA_DIR") or Path.cwd()))
    parser.add_argument("--master-dir", type=Path, default=default_master_dir())
    parser.add_argument("--length-xlsx", type=Path, default=default_length_xlsx())
    parser.add_argument("--length-overrides", type=Path)
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--difficulty", choices=["all", *DIFFICULTY_ORDER], default="all")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    difficulties = None if args.difficulty == "all" else {args.difficulty}
    payload = build_analysis(
        args.data_dir,
        master_dir=args.master_dir,
        length_xlsx=args.length_xlsx,
        length_overrides=args.length_overrides,
        force_download=args.force_download,
        difficulties=difficulties,
        limit=args.limit,
    )
    print(f"analyzed={payload['chart_count']} errors={payload['error_count']}")
    print(cache_path(args.data_dir))
    print(csv_path(args.data_dir))


if __name__ == "__main__":
    main()
