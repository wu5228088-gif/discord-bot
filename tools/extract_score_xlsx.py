from __future__ import annotations

import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def col_to_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha())
    value = 0
    for ch in letters:
        value = value * 26 + ord(ch.upper()) - ord("A") + 1
    return value


def as_number(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Cell:
    ref: str
    col: int
    value: str


def read_rows(path: Path) -> list[list[Cell]]:
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for item in root.findall("a:si", NS):
                shared.append("".join(t.text or "" for t in item.findall(".//a:t", NS)))

        root = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        rows: list[list[Cell]] = []
        for row in root.findall(".//a:row", NS):
            cells: list[Cell] = []
            for cell in row.findall("a:c", NS):
                ref = cell.attrib.get("r", "")
                value_node = cell.find("a:v", NS)
                formula_node = cell.find("a:f", NS)
                value = ""
                if value_node is not None and value_node.text is not None:
                    value = value_node.text
                    if cell.attrib.get("t") == "s":
                        value = shared[int(value)]
                elif formula_node is not None and formula_node.text:
                    value = "=" + formula_node.text
                cells.append(Cell(ref, col_to_index(ref), value))
            rows.append(cells)
        return rows


def cell_map(row: list[Cell]) -> dict[int, str]:
    return {cell.col: cell.value for cell in row}


def sum_numeric(row: dict[int, str], start_col: int, end_col: int) -> float:
    return sum(as_number(row.get(col, "")) or 0 for col in range(start_col, end_col + 1))


def main() -> None:
    path = Path(sys.argv[1])
    rows = read_rows(path)
    wanted = sys.argv[2:]

    for i, raw_header in enumerate(rows):
        header = cell_map(raw_header)
        name = header.get(1, "")
        if not name or name[0].isdigit():
            continue
        range_cols = [
            col
            for col, value in header.items()
            if re.search(r"\d+~\d+|\(\d+~\d+\)|\(\d+\)", value)
        ]
        if not range_cols:
            continue
        if wanted and not any(token.lower() in name.lower() for token in wanted):
            continue
        start_col, end_col = min(range_cols), max(range_cols)
        weight_rows = [cell_map(rows[i + offset]) for offset in range(1, 6)]
        weighted_row = cell_map(rows[i + 6])
        combo_row = cell_map(rows[i + 8]) if i + 8 < len(rows) else {}
        counts = {
            "0.1": sum_numeric(weight_rows[0], start_col, end_col),
            "0.2": sum_numeric(weight_rows[1], start_col, end_col),
            "1": sum_numeric(weight_rows[2], start_col, end_col),
            "2": sum_numeric(weight_rows[3], start_col, end_col),
            "3": sum_numeric(weight_rows[4], start_col, end_col),
        }
        weighted_total = (
            counts["0.1"] * 0.1
            + counts["0.2"] * 0.2
            + counts["1"]
            + counts["2"] * 2
            + counts["3"] * 3
        )
        combo_total = sum_numeric(combo_row, start_col, end_col)
        fever_ranges = [
            value
            for col, value in sorted(header.items())
            if start_col <= col <= end_col and "fever" in value.lower()
        ]
        skill_ranges = [
            value
            for col, value in sorted(header.items())
            if start_col <= col <= end_col and re.search(r"\bs\d", value.lower())
        ]
        print(name)
        print(f"  columns: {start_col}-{end_col}")
        print(f"  counts: {counts}")
        print(f"  weighted_total: {weighted_total:.1f} (cached AH: {header.get(34, '') or weighted_row.get(34, '')})")
        print(f"  combo_total: {combo_total:.0f} (cached AH/Y/etc may be elsewhere)")
        print(f"  skills: {', '.join(skill_ranges)}")
        print(f"  fever: {', '.join(fever_ranges) or '(none in header)'}")


if __name__ == "__main__":
    main()
