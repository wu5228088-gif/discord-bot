#!/usr/bin/env python3
"""Print a compact summary of a decoded PJSK JSON response."""

from __future__ import annotations

import argparse
import json
import pathlib
from collections.abc import Mapping, Sequence


def describe(value) -> str:
    if isinstance(value, Mapping):
        keys = list(value.keys())
        if "__ENUM__" in value:
            return f"compact-table object, keys={len(keys)}, columns={', '.join(keys[:8])}"
        return f"object, keys={len(keys)}, first={', '.join(map(str, keys[:8]))}"
    if isinstance(value, list):
        sample = value[0] if value else None
        if isinstance(sample, Mapping):
            sample_keys = ", ".join(map(str, list(sample.keys())[:8]))
            return f"array[{len(value)}] of objects, first keys={sample_keys}"
        return f"array[{len(value)}] of {type(sample).__name__ if sample is not None else 'empty'}"
    return f"{type(value).__name__}: {value!r}"


def compact_table_rows(value) -> int | None:
    if not isinstance(value, Mapping) or "__ENUM__" not in value:
        return None
    lengths = [len(item) for key, item in value.items() if key != "__ENUM__" and isinstance(item, Sequence) and not isinstance(item, (str, bytes))]
    return max(lengths) if lengths else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file", type=pathlib.Path)
    parser.add_argument("--limit", type=int, default=80)
    args = parser.parse_args()

    data = json.loads(args.json_file.read_text(encoding="utf-8-sig"))
    if not isinstance(data, Mapping):
        print(describe(data))
        return 0

    print(f"file: {args.json_file}")
    print(f"top-level keys: {len(data)}")
    for index, (key, value) in enumerate(data.items()):
        if index >= args.limit:
            print(f"... {len(data) - args.limit} more")
            break
        rows = compact_table_rows(value)
        suffix = f", rows~{rows}" if rows is not None else ""
        print(f"- {key}: {describe(value)}{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
