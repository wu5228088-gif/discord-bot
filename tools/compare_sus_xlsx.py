from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from analyze_sus_theory import SusTheoryAnalyzer
from compute_score_xlsx import iter_blocks, score_block


def weight_bucket(weight: float) -> str:
    return f"{weight:g}"


def sus_summary(path: Path, difficulty_map: Path) -> dict:
    rows = json.loads(difficulty_map.read_text(encoding="utf-8"))
    difficulties = {
        (int(row["musicId"]), row["musicDifficulty"]): {
            "playLevel": int(row["playLevel"]),
            "totalNoteCount": int(row["totalNoteCount"]),
        }
        for row in rows
    }
    match = re.match(r"(\d+)_(easy|normal|hard|expert|master|append)", path.stem)
    music_id = int(match.group(1)) if match else 0
    diff_key = match.group(2) if match else "master"
    diff = difficulties.get((music_id, diff_key), {"playLevel": 30, "totalNoteCount": None})

    analyzer = SusTheoryAnalyzer(path.read_text(encoding="utf-8-sig"))
    events = analyzer.scoring_events()
    buckets = Counter(weight_bucket(event.weight) for event in events)
    result = analyzer.analyze(diff["playLevel"], diff["totalNoteCount"])
    return {
        "path": path,
        "difficulty": diff["playLevel"],
        "official_combo": diff["totalNoteCount"],
        "event_count": len(events),
        "weighted": sum(event.weight for event in events),
        "buckets": dict(sorted(buckets.items(), key=lambda kv: float(kv[0]))),
        "score": result["score_power_multiplier"],
        "fever": result["fever"],
        "skills": result["skill_coverages"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("xlsx", type=Path)
    parser.add_argument("sus", nargs="+", type=Path)
    parser.add_argument("--difficulty-map", type=Path, default=Path("master_cache/tc/musicDifficulties.json"))
    args = parser.parse_args()

    blocks = list(iter_blocks(args.xlsx))
    by_combo = {block["official_combo"]: block for block in blocks}

    for sus in args.sus:
        summary = sus_summary(sus, args.difficulty_map)
        block = by_combo.get(summary["official_combo"])
        print(sus.name)
        print(
            f"  SUS combo={summary['event_count']} official={summary['official_combo']} "
            f"delta={summary['event_count'] - summary['official_combo']}"
        )
        print(f"  SUS weighted={summary['weighted']:.1f} score={summary['score']:.6f}x")
        if block:
            scored = score_block(block, summary["difficulty"], use_default_fever=False)
            print(
                f"  XLSX combo={block['official_combo']} weighted={block['total_weight']:.1f} "
                f"score={scored['score']:.6f}x"
            )
            print(f"  XLSX buckets={{{', '.join(f'{k}: {v:g}' for k, v in block['counts'].items())}}}")
            deltas = {
                key: summary["buckets"].get(key, 0) - block["counts"].get(key, 0)
                for key in ["0.1", "0.2", "1", "2", "3"]
            }
            print(f"  bucket delta SUS-XLSX={{{', '.join(f'{k}: {v:g}' for k, v in deltas.items())}}}")
        print(f"  SUS buckets={summary['buckets']}")
        if summary["event_count"] != summary["official_combo"]:
            print("  CHECK: combo mismatch, inspect note/tick logic before trusting score")


if __name__ == "__main__":
    main()
