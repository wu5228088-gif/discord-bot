from __future__ import annotations

import argparse
import re
from pathlib import Path

from extract_score_xlsx import as_number, cell_map, read_rows


def parse_range(label: str) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\s*~\s*(\d+)", label)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.search(r"(?<!\d)(\d+)(?!\d)", label)
    if match:
        value = int(match.group(1))
        return value, value
    return None


def range_len(combo_range: tuple[int, int]) -> int:
    start, end = combo_range
    return end - start + 1


def overlap(a: tuple[int, int], b: tuple[int, int]) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]) + 1)


def iter_blocks(path: Path):
    rows = read_rows(path)
    for i, raw_header in enumerate(rows):
        header = cell_map(raw_header)
        name = header.get(1, "")
        if not name or name[0].isdigit():
            continue
        ranges = {col: parse_range(value) for col, value in header.items() if 1 < col < 34}
        header_like_cols = [
            col
            for col, value in header.items()
            if 1 < col < 34 and ("~" in value or "s" in value.lower() or "fever" in value.lower())
        ]
        if len(header_like_cols) < 5:
            continue
        range_cols = [col for col, combo_range in ranges.items() if combo_range is not None and col > 1]
        if not range_cols or i + 6 >= len(rows):
            continue
        start_col, end_col = min(range_cols), max(range_cols)
        weight_rows = [cell_map(rows[i + offset]) for offset in range(1, 6)]
        weighted_row = cell_map(rows[i + 6])
        segments = []
        for col in range(start_col, end_col + 1):
            combo_range = ranges.get(col)
            if combo_range is None:
                continue
            counts = [
                as_number(weight_rows[row].get(col, "")) or 0.0
                for row in range(5)
            ]
            weight = counts[0] * 0.1 + counts[1] * 0.2 + counts[2] + counts[3] * 2 + counts[4] * 3
            label = header.get(col, "")
            segments.append(
                {
                    "label": label,
                    "range": combo_range,
                    "weight": weight,
                    "counts": counts,
                    "combo_count": sum(counts),
                    "skill": re.search(r"\bs\d", label.lower()) is not None,
                    "fever": "fever" in label.lower(),
                }
            )
        total_weight = sum(segment["weight"] for segment in segments)
        official_combo = int(round(sum(segment["combo_count"] for segment in segments)))
        counts = {
            "0.1": sum(segment["counts"][0] for segment in segments),
            "0.2": sum(segment["counts"][1] for segment in segments),
            "1": sum(segment["counts"][2] for segment in segments),
            "2": sum(segment["counts"][3] for segment in segments),
            "3": sum(segment["counts"][4] for segment in segments),
        }
        yield {
            "name": name,
            "segments": segments,
            "counts": counts,
            "total_weight": total_weight,
            "official_combo": official_combo,
            "cached_total_weight": as_number(weighted_row.get(34, "")),
        }


def score_block(block: dict, difficulty: int, use_default_fever: bool) -> dict:
    total_weight = block["total_weight"]
    combo = block["official_combo"]
    base = 4 * (1 + (difficulty - 5) * 0.005)
    has_fever = any(segment["fever"] for segment in block["segments"])
    fever_range = None
    if use_default_fever and not has_fever and combo:
        fever_count = round(combo * 0.10)
        fever_start = (combo - fever_count) // 2 + 1
        fever_range = (fever_start, fever_start + fever_count - 1)

    skill_weight = [0.0] * 6
    fever_weight = 0.0
    score = 0.0

    for segment in block["segments"]:
        start, end = segment["range"]
        n = range_len(segment["range"])
        if n <= 0 or total_weight <= 0:
            continue
        per_combo_weight = segment["weight"] / n
        for combo_before in range(start, end + 1):
            combo_bonus = 1 + min(combo_before // 100, 10) * 0.01
            is_skill = segment["skill"]
            skill_match = re.search(r"\bs(\d)", segment["label"].lower())
            if skill_match:
                skill_weight[int(skill_match.group(1)) - 1] += per_combo_weight
            is_fever = segment["fever"] or (fever_range is not None and fever_range[0] <= combo_before <= fever_range[1])
            if is_fever:
                fever_weight += per_combo_weight
            score += (
                base
                * per_combo_weight
                / total_weight
                * combo_bonus
                * (3.0 if is_skill else 1.0)
                * (1.5 if is_fever else 1.0)
            )

    return {
        "difficulty": difficulty,
        "base": base,
        "score": score,
        "skill_weight": skill_weight,
        "skill_pct": [w / total_weight * 100 if total_weight else 0.0 for w in skill_weight],
        "fever_range": fever_range,
        "fever_weight": fever_weight,
        "fever_pct": fever_weight / total_weight * 100 if total_weight else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("xlsx", type=Path)
    parser.add_argument("--difficulty", action="append", default=[])
    parser.add_argument("--default-fever", action="store_true")
    args = parser.parse_args()

    difficulties: dict[str, int] = {}
    for item in args.difficulty:
        key, value = item.split("=", 1)
        difficulties[key] = int(value)

    for block in iter_blocks(args.xlsx):
        diff = None
        for key, value in difficulties.items():
            if key in block["name"] or key == str(block["official_combo"]):
                diff = value
                break
        if diff is None:
            continue
        result = score_block(block, diff, args.default_fever)
        print(block["name"])
        print(f"  combo={block['official_combo']} weighted={block['total_weight']:.1f} difficulty={diff}")
        print(f"  score={result['score']:.6f} x team_power")
        print(
            "  skills="
            + ", ".join(
                f"s{i + 1}:{result['skill_weight'][i]:.1f} ({result['skill_pct'][i]:.2f}%)"
                for i in range(6)
            )
        )
        print(
            f"  fever={result['fever_weight']:.1f} ({result['fever_pct']:.2f}%)"
            + (f" default_range={result['fever_range']}" if result["fever_range"] else "")
        )


if __name__ == "__main__":
    main()
