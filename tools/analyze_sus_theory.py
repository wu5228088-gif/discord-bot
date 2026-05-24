from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Iterable


NO_NOTE = -1

WEIGHTS = {
    "tap": 1.0,
    "gold_tap": 2.0,
    "flick": 1.0,
    "gold_flick": 3.0,
    "hold_start": 1.0,
    "hold_end": 1.0,
    "hold_tick": 0.1,
    "hold_judge": 0.1,
    "gold_hold_judge": 0.2,
    "trace": 0.1,
    "gold_trace": 0.2,
    "traceflick": 1.0,
    "gold_traceflick": 3.0,
}

TAP_CRITICAL = {2, 6, 8}
TAP_TRACE = {5, 6}
TAP_CANCEL = {7, 8}

SLIDE_START = 1
SLIDE_END = 2
SLIDE_RELAY = 3
SLIDE_BEZIER = 4
SLIDE_INVISIBLE = 5

FLICK_DIRECTIONS = {1, 3, 4}


def base36_char(value: str) -> int:
    c = value[0]
    if c.isdigit():
        return int(c)
    return ord(c.lower()) - ord("a") + 10


def score_pairs(data: str) -> Iterable[tuple[Fraction, str]]:
    data = data.strip()
    length = len(data)
    for i in range(0, length - 1, 2):
        pair = data[i : i + 2].lower()
        if pair != "00":
            yield Fraction(i, length), pair


@dataclass
class Note:
    kind: str
    bar: Fraction
    lane: int
    width: int
    note_type: int
    channel: int | None = None
    decoration: bool = False
    tap_idx: int = NO_NOTE
    directional_idx: int = NO_NOTE
    next_idx: int = NO_NOTE
    head_idx: int = NO_NOTE


@dataclass
class ScoringEvent:
    bar: Fraction
    kind: str
    weight: float


class SusTheoryAnalyzer:
    def __init__(self, text: str):
        self.text = text
        self.notes: list[Note] = []
        self.active: list[int] = []
        self.skill_bars: list[Fraction] = []
        self.fever_start_bars: list[Fraction] = []
        self.fever_end_bars: list[Fraction] = []
        self.bpm_events: list[tuple[Fraction, Fraction]] = [(Fraction(0), Fraction(120))]
        self.bar_lengths: list[tuple[Fraction, Fraction]] = [(Fraction(0), Fraction(4))]
        self._parse()
        self._link_notes()

    def _parse(self) -> None:
        bpm_defs: dict[int, Fraction] = {}
        for raw in self.text.splitlines():
            line = raw.strip()
            if not line:
                continue

            if m := re.match(r"^#BPM(..):\s*([0-9.]+)", line):
                bpm_defs[int(m.group(1), 36)] = Fraction(m.group(2))
                continue

            if not (m := re.match(r"^#(\w+):\s*(.*)$", line)):
                continue
            header, data = m.groups()

            if m_bar := re.match(r"^(\d{3})02$", header):
                self.bar_lengths.append((Fraction(int(m_bar.group(1))), Fraction(data.strip())))
                continue

            if m_bpm := re.match(r"^(\d{3})08$", header):
                bar_num = int(m_bpm.group(1))
                for beat, pair in score_pairs(data):
                    bpm = bpm_defs.get(int(pair, 36))
                    if bpm is not None:
                        self.bpm_events.append((Fraction(bar_num) + beat, bpm))
                continue

            if m_tap := re.match(r"^(\d{3})1(.)$", header):
                bar_num = int(m_tap.group(1))
                lane = base36_char(m_tap.group(2))
                for beat, pair in score_pairs(data):
                    note_type = base36_char(pair[0])
                    width = base36_char(pair[1])
                    bar = Fraction(bar_num) + beat
                    if not (0 <= lane - 2 < 12):
                        if lane == 0:
                            self.skill_bars.append(bar)
                        elif note_type == 1:
                            self.fever_start_bars.append(bar)
                        else:
                            self.fever_end_bars.append(bar)
                        continue
                    self.notes.append(Note("tap", bar, lane, width, note_type))
                continue

            if m_slide := re.match(r"^(\d{3})[34](.)(.)$", header):
                bar_num = int(m_slide.group(1))
                lane = base36_char(m_slide.group(2))
                channel = base36_char(m_slide.group(3))
                for beat, pair in score_pairs(data):
                    note_type = base36_char(pair[0])
                    width = base36_char(pair[1])
                    if 0 <= lane - 2 < 12:
                        self.notes.append(Note("slide", Fraction(bar_num) + beat, lane, width, note_type, channel))
                continue

            if m_dir := re.match(r"^(\d{3})5(.)$", header):
                bar_num = int(m_dir.group(1))
                lane = base36_char(m_dir.group(2))
                for beat, pair in score_pairs(data):
                    note_type = base36_char(pair[0])
                    width = base36_char(pair[1])
                    if 0 <= lane - 2 < 12:
                        self.notes.append(Note("directional", Fraction(bar_num) + beat, lane, width, note_type))
                continue

            if m_deco := re.match(r"^(\d{3})9(.)(.)$", header):
                bar_num = int(m_deco.group(1))
                lane = base36_char(m_deco.group(2))
                channel = base36_char(m_deco.group(3))
                for beat, pair in score_pairs(data):
                    note_type = base36_char(pair[0])
                    width = base36_char(pair[1])
                    if 0 <= lane - 2 < 12:
                        self.notes.append(
                            Note("slide", Fraction(bar_num) + beat, lane, width, note_type, channel, True)
                        )

        self.bpm_events = sorted({bar: bpm for bar, bpm in self.bpm_events}.items())
        self.bar_lengths = sorted({bar: length for bar, length in self.bar_lengths}.items())
        self.skill_bars = sorted(set(self.skill_bars))
        self.fever_start_bars = sorted(set(self.fever_start_bars))
        self.fever_end_bars = sorted(set(self.fever_end_bars))

    def _link_notes(self) -> None:
        self.notes.sort(key=lambda n: n.bar)
        deleted = [False] * len(self.notes)
        indexes: dict[Fraction, list[int]] = {}
        for i, note in enumerate(self.notes):
            indexes.setdefault(note.bar, []).append(i)

        slide_groups: dict[tuple[int | None, bool], list[int]] = {}
        for i, note in enumerate(self.notes):
            if note.kind == "slide":
                slide_groups.setdefault((note.channel, note.decoration), []).append(i)

        for i, note in enumerate(self.notes):
            if deleted[i] or note.kind != "directional":
                continue
            for j in indexes.get(note.bar, []):
                tap = self.notes[j]
                if (
                    not deleted[j]
                    and tap.kind == "tap"
                    and tap.lane == note.lane
                    and tap.width == note.width
                ):
                    deleted[j] = True
                    note.tap_idx = j

        for i, note in enumerate(self.notes):
            if deleted[i] or note.kind != "slide":
                continue
            if note.head_idx == NO_NOTE:
                note.head_idx = i

            # #9 slide chains are visual decorations. Viewer converters may keep
            # a reference to nearby taps for drawing, but those decorative chains
            # must not consume the actual scoring tap/directional notes.
            if not note.decoration:
                for j in list(indexes.get(note.bar, [])):
                    tap = self.notes[j]
                    if (
                        not deleted[j]
                        and tap.kind == "tap"
                        and tap.lane == note.lane
                        and tap.width == note.width
                    ):
                        deleted[j] = True
                        note.tap_idx = j

                for j in list(indexes.get(note.bar, [])):
                    directional = self.notes[j]
                    if (
                        not deleted[j]
                        and directional.kind == "directional"
                        and directional.lane == note.lane
                        and directional.width == note.width
                    ):
                        deleted[j] = True
                        note.directional_idx = j
                        if directional.tap_idx != NO_NOTE:
                            note.tap_idx = directional.tap_idx

        for group in slide_groups.values():
            head_idx = NO_NOTE
            for pos, idx in enumerate(group):
                note = self.notes[idx]
                if head_idx == NO_NOTE or note.note_type == SLIDE_START:
                    head_idx = idx
                note.head_idx = head_idx
                if note.note_type != SLIDE_END and pos + 1 < len(group):
                    nxt = self.notes[group[pos + 1]]
                    note.next_idx = group[pos + 1]
                    nxt.head_idx = head_idx

        self.active = [i for i in range(len(self.notes)) if not deleted[i]]

    def _tap_type(self, idx: int) -> int | None:
        return self.notes[idx].note_type if idx != NO_NOTE else None

    def _is_critical(self, idx: int) -> bool:
        note = self.notes[idx]
        if note.kind == "tap":
            return note.note_type in TAP_CRITICAL
        if note.kind == "directional":
            return self._tap_type(note.tap_idx) in TAP_CRITICAL
        if note.kind == "slide":
            if self._tap_type(note.tap_idx) in TAP_CRITICAL:
                return True
            if note.directional_idx != NO_NOTE and self._is_critical(note.directional_idx):
                return True
            if note.head_idx != NO_NOTE:
                head = self.notes[note.head_idx]
                return self._tap_type(head.tap_idx) in TAP_CRITICAL or (
                    head.directional_idx != NO_NOTE and self._is_critical(head.directional_idx)
                )
        return False

    def _is_trace(self, idx: int) -> bool:
        note = self.notes[idx]
        if note.kind == "tap":
            return note.note_type in TAP_TRACE
        if note.kind == "directional":
            return self._tap_type(note.tap_idx) in TAP_TRACE
        if note.kind == "slide":
            return self._tap_type(note.tap_idx) in TAP_TRACE or (
                note.directional_idx != NO_NOTE and self._is_trace(note.directional_idx)
            )
        return False

    def _has_scoring_flick_direction(self, idx: int) -> bool:
        note = self.notes[idx]
        if note.kind == "directional":
            return note.note_type in FLICK_DIRECTIONS
        if note.kind == "slide" and note.directional_idx != NO_NOTE:
            return self.notes[note.directional_idx].note_type in FLICK_DIRECTIONS
        return False

    def _slide_chain(self, start_idx: int) -> list[int]:
        chain = []
        seen = set()
        idx = start_idx
        while idx != NO_NOTE and idx not in seen:
            seen.add(idx)
            chain.append(idx)
            idx = self.notes[idx].next_idx
        return chain

    def _slide_is_path(self, idx: int) -> bool:
        # Mirrors the viewer parsers: relay/invisible nodes normally shape the
        # green hold path, unless a tap type 3 at the same slot explicitly removes
        # that waypoint.
        note = self.notes[idx]
        if note.note_type == 0:
            return False
        if note.note_type not in (SLIDE_RELAY, SLIDE_INVISIBLE):
            return True
        if self._has_scoring_flick_direction(idx):
            return True
        return self._tap_type(note.tap_idx) != 3

    def _slide_edge_is_cancelled(self, idx: int) -> bool:
        note = self.notes[idx]
        return (
            note.kind == "slide"
            and note.note_type in (SLIDE_START, SLIDE_END)
            and self._tap_type(note.tap_idx) in TAP_CANCEL
            and not self._has_scoring_flick_direction(idx)
        )

    def _bar_length_at(self, bar: Fraction) -> Fraction:
        current = self.bar_lengths[0][1]
        for event_bar, length in self.bar_lengths:
            if event_bar <= bar:
                current = length
            else:
                break
        return current

    def _eighth_grid_between(self, start: Fraction, end: Fraction) -> Iterable[Fraction]:
        # Tick points are anchored to the chart grid: quarter white lines and their midpoints.
        # They are not re-anchored to each hold start.
        bar = start.__floor__()
        while Fraction(bar) <= end:
            bar_start = Fraction(bar)
            bar_len = self._bar_length_at(bar_start)
            divisions = int(bar_len * 2) if (bar_len * 2).denominator == 1 else 8
            for k in range(divisions):
                point = bar_start + Fraction(k, divisions)
                if start < point < end:
                    yield point
            bar += 1

    def _is_on_eighth_grid(self, point: Fraction) -> bool:
        bar_start = Fraction(point.__floor__())
        bar_len = self._bar_length_at(bar_start)
        divisions = int(bar_len * 2) if (bar_len * 2).denominator == 1 else 8
        return ((point - bar_start) * divisions).denominator == 1

    def scoring_events(self) -> list[ScoringEvent]:
        events: list[ScoringEvent] = []

        for idx in self.active:
            note = self.notes[idx]
            if note.kind == "slide" and note.decoration:
                continue
            if self._slide_edge_is_cancelled(idx):
                continue
            critical = self._is_critical(idx)
            trace = self._is_trace(idx)
            has_directional = self._has_scoring_flick_direction(idx)

            kind: str | None = None
            if note.kind == "tap":
                if note.note_type in TAP_CANCEL:
                    continue
                if trace:
                    kind = "gold_trace" if critical else "trace"
                elif critical:
                    kind = "gold_tap"
                elif note.note_type == 3:
                    kind = "flick"
                else:
                    kind = "tap"
            elif has_directional:
                if trace:
                    kind = "gold_traceflick" if critical else "traceflick"
                else:
                    kind = "gold_flick" if critical else "flick"
            elif note.kind == "slide":
                if trace:
                    kind = "gold_trace" if critical else "trace"
                elif note.note_type == SLIDE_START:
                    kind = "gold_tap" if critical else "hold_start"
                elif note.note_type == SLIDE_END:
                    kind = "gold_tap" if critical else "hold_end"
                elif note.note_type == SLIDE_RELAY:
                    kind = "gold_hold_judge" if critical else "hold_judge"

            if kind is not None:
                events.append(ScoringEvent(note.bar, kind, WEIGHTS[kind]))

        # Generate regular hold ticks wherever the visible green hold path crosses
        # the chart's global 1/8 grid. Start/end judgements themselves do not count.
        for idx in self.active:
            note = self.notes[idx]
            if note.kind != "slide" or note.decoration or note.note_type != SLIDE_START:
                continue
            chain = [i for i in self._slide_chain(idx) if self._slide_is_path(i)]
            if len(chain) < 2:
                continue
            for start_idx, end_idx in zip(chain, chain[1:]):
                start = self.notes[start_idx].bar
                end = self.notes[end_idx].bar
                for tick in self._eighth_grid_between(start, end):
                    events.append(ScoringEvent(tick, "hold_tick", WEIGHTS["hold_tick"]))
            for path_idx in chain[1:-1]:
                point = self.notes[path_idx].bar
                if self._is_on_eighth_grid(point):
                    events.append(ScoringEvent(point, "hold_tick", WEIGHTS["hold_tick"]))

        return sorted(events, key=lambda e: e.bar)

    def time_at_bar(self, bar: Fraction) -> float:
        bpm_events = [event for event in self.bpm_events if event[0] <= bar]
        bar_events = [event for event in self.bar_lengths if event[0] <= bar]
        current_bpm = bpm_events[0][1]
        current_bar_len = bar_events[0][1]
        current_bar = Fraction(0)
        seconds = Fraction(0)

        change_points = sorted({b for b, _ in self.bpm_events + self.bar_lengths if Fraction(0) < b <= bar} | {bar})
        for point in change_points:
            delta = point - current_bar
            seconds += delta * current_bar_len * 60 / current_bpm
            current_bar = point
            for b, bpm in self.bpm_events:
                if b == point:
                    current_bpm = bpm
            for b, bar_len in self.bar_lengths:
                if b == point:
                    current_bar_len = bar_len
        return float(seconds)

    def analyze(
        self,
        difficulty_level: int,
        official_combo: int | None = None,
        skill_duration: float = 5.0,
        skill_multiplier: float = 3.0,
        fever_multiplier: float = 1.5,
        fever_combo_ratio: float = 0.10,
    ) -> dict:
        events = self.scoring_events()
        timed = [(event, self.time_at_bar(event.bar)) for event in events]
        skill_starts = [self.time_at_bar(bar) for bar in self.skill_bars[:6]]
        windows = [(start, start + skill_duration) for start in skill_starts]
        score_event_count = len(events)
        combo_count = official_combo or score_event_count

        def combo_at_score_index(score_index: int) -> int:
            if combo_count <= 0 or score_event_count <= 0:
                return 0
            if combo_count == score_event_count:
                return score_index
            # Some charts, especially visual-heavy APPEND charts, contain score events
            # that do not advance combo. The SUS alone does not reliably identify all
            # of them, so use the official combo total to keep combo bonus and fever
            # placement on the game's combo scale while still scoring every event.
            return max(1, min(combo_count, round(score_index * combo_count / score_event_count)))

        fever_chance_sec = self.time_at_bar(self.fever_start_bars[0]) if self.fever_start_bars else None
        super_fever_sec = self.time_at_bar(self.fever_end_bars[0]) if self.fever_end_bars else None
        fever_count = max(1, int(score_event_count * fever_combo_ratio)) if score_event_count else 0
        fever_start_sec = super_fever_sec
        fever_end_sec = None
        if fever_start_sec is not None:
            fever_notes = [(score_index, event, sec) for score_index, (event, sec) in enumerate(timed, start=1) if sec >= fever_start_sec]
            if fever_notes:
                fever_end_note = fever_notes[min(len(fever_notes), fever_count) - 1]
                fever_start_combo = combo_at_score_index(fever_notes[0][0])
                fever_end_combo = combo_at_score_index(fever_end_note[0])
                fever_end_sec = fever_end_note[2]
            else:
                fever_start_combo = score_event_count + 1
                fever_end_combo = score_event_count
        else:
            fever_start_combo = 0
            fever_end_combo = 0

        base_multiplier = 4 * (1 + (difficulty_level - 5) * 0.005)
        total_weight = sum(event.weight for event in events)
        score_multiplier = 0.0
        skill_weight_by_window = [0.0 for _ in windows]
        skill_weight_by_window_min = [0.0 for _ in windows]
        skill_weight_by_window_max = [0.0 for _ in windows]
        skill_fever_weight_by_window = [0.0 for _ in windows]
        skill_score_terms = [0.0 for _ in windows]
        skill_score_terms_min = [0.0 for _ in windows]
        skill_score_terms_max = [0.0 for _ in windows]
        base_score_term = 0.0
        score_multiplier_min = 0.0
        score_multiplier_max = 0.0
        fever_weight = 0.0

        for score_index, (event, sec) in enumerate(timed, start=1):
            combo = combo_at_score_index(score_index)
            combo_bonus = 1 + min(combo // 100, 10) * 0.01
            note_share = base_multiplier * event.weight / total_weight if total_weight else 0.0
            skill_indexes = []
            skill_indexes_min = []
            skill_indexes_max = []
            for i, (start, end) in enumerate(windows):
                if start <= sec < end:
                    skill_indexes.append(i)
                    skill_weight_by_window[i] += event.weight
                if start < sec < end:
                    skill_indexes_min.append(i)
                    skill_weight_by_window_min[i] += event.weight
                if start <= sec <= end:
                    skill_indexes_max.append(i)
                    skill_weight_by_window_max[i] += event.weight
            in_fever = (
                fever_start_sec is not None
                and fever_end_sec is not None
                and fever_start_sec <= sec <= fever_end_sec
            )
            if in_fever:
                fever_weight += event.weight
            fever_factor = fever_multiplier if in_fever else 1.0
            base_score = note_share * combo_bonus * fever_factor
            base_score_term += base_score
            # Game skill windows do not normally overlap. If a custom chart ever
            # does overlap them, apply only one skill multiplier to avoid double
            # counting the same note's skill bonus.
            if skill_indexes:
                i = skill_indexes[0]
                skill_score_terms[i] += base_score
                if in_fever:
                    skill_fever_weight_by_window[i] += event.weight
            score_multiplier += base_score * (skill_multiplier if skill_indexes else 1.0)
            if skill_indexes_min:
                skill_score_terms_min[skill_indexes_min[0]] += base_score
            if skill_indexes_max:
                skill_score_terms_max[skill_indexes_max[0]] += base_score
            score_multiplier_min += base_score * (skill_multiplier if skill_indexes_min else 1.0)
            score_multiplier_max += base_score * (skill_multiplier if skill_indexes_max else 1.0)

        kind_counter = Counter(event.kind for event in events)
        skill_coverages = [
            {
                "index": i + 1,
                "start_sec": start,
                "end_sec": end,
                "covered_weight": skill_weight_by_window[i],
                "covered_weight_min": skill_weight_by_window_min[i],
                "covered_weight_max": skill_weight_by_window_max[i],
                "fever_covered_weight": skill_fever_weight_by_window[i],
                "coverage_pct": skill_weight_by_window[i] / total_weight * 100 if total_weight else 0.0,
                "coverage_pct_min": skill_weight_by_window_min[i] / total_weight * 100 if total_weight else 0.0,
                "coverage_pct_max": skill_weight_by_window_max[i] / total_weight * 100 if total_weight else 0.0,
                "fever_overlap_pct": (
                    skill_fever_weight_by_window[i] / skill_weight_by_window[i] * 100
                    if skill_weight_by_window[i]
                    else 0.0
                ),
            }
            for i, (start, end) in enumerate(windows)
        ]
        return {
            "difficulty_level": difficulty_level,
            "base_power_multiplier": base_multiplier,
            "official_combo": official_combo,
            "total_events": score_event_count,
            "score_event_count": score_event_count,
            "combo_count_used": combo_count,
            "score_event_combo_delta": score_event_count - combo_count if official_combo else 0,
            "total_weight": total_weight,
            "score_power_multiplier": score_multiplier,
            "score_power_multiplier_min": score_multiplier_min,
            "score_power_multiplier_max": score_multiplier_max,
            "score_base_power_multiplier": base_score_term,
            "skill_score_terms": skill_score_terms,
            "skill_score_terms_min": skill_score_terms_min,
            "skill_score_terms_max": skill_score_terms_max,
            "fever": {
                "combo_start": fever_start_combo if fever_end_combo else None,
                "combo_end": fever_end_combo if fever_end_combo else None,
                "combo_count": (
                    fever_end_combo - fever_start_combo + 1
                    if fever_start_combo is not None and fever_end_combo
                    else fever_count
                ),
                "fever_chance_bar": str(self.fever_start_bars[0]) if self.fever_start_bars else None,
                "super_fever_bar": str(self.fever_end_bars[0]) if self.fever_end_bars else None,
                "fever_chance_sec": fever_chance_sec,
                "super_fever_sec": super_fever_sec,
                "start_sec": fever_start_sec,
                "end_sec": fever_end_sec,
                "covered_weight": fever_weight,
                "coverage_pct": fever_weight / total_weight * 100 if total_weight else 0.0,
            },
            "skill_bars": [str(bar) for bar in self.skill_bars[:6]],
            "skill_coverages": skill_coverages,
            "kind_counts": dict(sorted(kind_counter.items())),
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--difficulty-map", type=Path, default=Path("master_cache/tc/musicDifficulties.json"))
    args = parser.parse_args()

    difficulties = {}
    if args.difficulty_map.exists():
        rows = json.loads(args.difficulty_map.read_text(encoding="utf-8"))
        for row in rows:
            difficulties[(int(row["musicId"]), row["musicDifficulty"])] = {
                "playLevel": int(row["playLevel"]),
                "totalNoteCount": int(row["totalNoteCount"]),
            }

    for path in args.files:
        stem = path.stem
        m = re.match(r"(\d+)_(easy|normal|hard|expert|master|append)", stem)
        music_id = int(m.group(1)) if m else 0
        diff_key = m.group(2) if m else "master"
        diff_row = difficulties.get((music_id, diff_key), {"playLevel": 30, "totalNoteCount": None})
        result = SusTheoryAnalyzer(path.read_text(encoding="utf-8-sig")).analyze(
            diff_row["playLevel"],
            diff_row["totalNoteCount"],
        )
        print(json.dumps({"file": str(path), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
