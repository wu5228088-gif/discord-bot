#!/usr/bin/env python3
"""Deobfuscate Project SEKAI AssetBundle files.

The known format starts with 10 00 00 00, then the first 128 bytes of the real
AssetBundle stream are stored in 8-byte blocks where the first 5 bytes of each
block are bitwise inverted. If the marker is absent, the file is copied as-is.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil


MARKER = b"\x10\x00\x00\x00"


def deobfuscate_file(src: pathlib.Path, dst: pathlib.Path) -> str:
    data = src.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)

    if not data.startswith(MARKER):
        shutil.copyfile(src, dst)
        return "copied: marker not present"

    body = bytearray(data[4:])
    limit = min(128, len(body))
    for offset in range(0, limit, 8):
        for index in range(offset, min(offset + 5, limit)):
            body[index] = (~body[index]) & 0xFF

    dst.write_bytes(body)
    magic = bytes(body[:8])
    return f"deobfuscated: output magic={magic!r}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=pathlib.Path)
    parser.add_argument("output", type=pathlib.Path)
    args = parser.parse_args()

    if args.input.is_dir():
        args.output.mkdir(parents=True, exist_ok=True)
        for src in args.input.rglob("*"):
            if src.is_file():
                rel = src.relative_to(args.input)
                result = deobfuscate_file(src, args.output / rel)
                print(f"{rel}: {result}")
    else:
        result = deobfuscate_file(args.input, args.output)
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
