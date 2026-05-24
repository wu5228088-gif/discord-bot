from __future__ import annotations

import re
import sys
from fractions import Fraction
from pathlib import Path

from analyze_sus_theory import SusTheoryAnalyzer, base36_char, score_pairs


def main() -> None:
    for arg in sys.argv[1:]:
        path = Path(arg)
        analyzer = SusTheoryAnalyzer(path.read_text(encoding="utf-8-sig"))
        print(path.name)
        for raw in analyzer.text.splitlines():
            line = raw.strip()
            match = re.match(r"^#(\d{3})1(.):\s*(.*)$", line)
            if not match:
                continue
            bar_num = int(match.group(1))
            lane = base36_char(match.group(2))
            if 0 <= lane - 2 < 12:
                continue
            for beat, pair in score_pairs(match.group(3)):
                note_type = base36_char(pair[0])
                width = base36_char(pair[1])
                bar = Fraction(bar_num) + beat
                sec = analyzer.time_at_bar(bar)
                if lane == 0:
                    label = "skill"
                elif note_type == 1:
                    label = "fever_chance"
                else:
                    label = "super_fever"
                print(f"  bar={bar} sec={sec:.3f} lane={lane} type={note_type} width={width} {label} line={line}")


if __name__ == "__main__":
    main()
