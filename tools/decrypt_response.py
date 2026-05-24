#!/usr/bin/env python3
"""
Diagnose and decrypt high-entropy CDN response bodies.

The observed files look like AES block ciphertext: their sizes are multiples of
16 and their entropy is close to 8 bits/byte. This tool keeps the key material
explicit: pass --key and --iv when you have them, then it decrypts AES-CBC and
tries to decode the plaintext as MessagePack, JSON, or common compressed data.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import json
import math
import pathlib
import sys
import zlib
from typing import Any

import bz2
import lzma
import msgpack
from Crypto.Cipher import AES


MAGICS = {
    b"PK\x03\x04": "zip",
    b"\x1f\x8b": "gzip",
    b"UnityFS": "UnityFS",
    b"{": "json-object",
    b"[": "json-array",
    b"\x89PNG": "png",
    b"OggS": "ogg",
    b"RIFF": "riff",
    b"\x78\x9c": "zlib",
    b"\x78\xda": "zlib",
    b"\x78\x01": "zlib",
    b"\x04\x22\x4d\x18": "lz4-frame",
}


def parse_bytes(value: str, *, name: str) -> bytes:
    """Parse a CLI key/IV value as hex, base64, or UTF-8 text.

    Prefixes remove ambiguity:
      hex:001122...
      b64:ABEi...
      text:literal-key
    """
    raw = value.strip()
    lowered = raw.lower()
    if lowered.startswith("hex:"):
        return bytes.fromhex(raw[4:])
    if lowered.startswith("b64:"):
        return base64.b64decode(raw[4:], validate=True)
    if lowered.startswith("text:"):
        return raw[5:].encode("utf-8")

    hex_value = raw[2:] if raw.lower().startswith("0x") else raw

    if len(hex_value) % 2 == 0:
        try:
            return bytes.fromhex(hex_value)
        except ValueError:
            pass

    try:
        decoded = base64.b64decode(raw, validate=True)
        if decoded:
            return decoded
    except binascii.Error:
        pass

    encoded = raw.encode("utf-8")
    if not encoded:
        raise ValueError(f"{name} is empty")
    return encoded


def entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    return -sum((count / len(data)) * math.log2(count / len(data)) for count in counts if count)


def printable_ratio(data: bytes) -> float:
    if not data:
        return 0.0
    return sum(byte in (9, 10, 13) or 32 <= byte <= 126 for byte in data) / len(data)


def find_magics(data: bytes) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for magic, label in MAGICS.items():
        offset = data.find(magic)
        if offset >= 0:
            hits.append((label, offset))
    return hits


def remove_pkcs7_padding(data: bytes) -> bytes:
    if not data:
        raise ValueError("cannot remove padding from empty plaintext")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > AES.block_size:
        raise ValueError(f"invalid PKCS#7 padding length: {pad_len}")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("invalid PKCS#7 padding bytes")
    return data[:-pad_len]


def maybe_decompress(data: bytes) -> tuple[bytes, str | None]:
    candidates = (
        ("gzip", gzip.decompress),
        ("zlib", zlib.decompress),
        ("bz2", bz2.decompress),
        ("lzma", lzma.decompress),
    )
    for label, func in candidates:
        try:
            return func(data), label
        except Exception:
            continue
    return data, None


def to_jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return {"__bytes_hex__": value.hex()}
    if isinstance(value, dict):
        return {to_jsonable(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    return value


def decode_payload(data: bytes) -> tuple[str, Any]:
    try:
        return "json", json.loads(data.decode("utf-8-sig"))
    except Exception:
        pass

    try:
        unpacked = msgpack.unpackb(data, raw=False, strict_map_key=False)
        return "msgpack", to_jsonable(unpacked)
    except Exception:
        pass

    try:
        unpacked = msgpack.unpackb(data, raw=True, strict_map_key=False)
        return "msgpack-raw", to_jsonable(unpacked)
    except Exception:
        pass

    try:
        return "utf8-text", data.decode("utf-8")
    except Exception:
        return "binary", None


def print_diagnostics(path: pathlib.Path, data: bytes) -> None:
    print(f"file: {path}")
    print(f"size: {len(data)} bytes")
    print(f"size % 16: {len(data) % 16}")
    print(f"entropy: {entropy(data):.4f} bits/byte")
    print(f"printable ratio: {printable_ratio(data):.4f}")
    print(f"first 16 bytes: {data[:16].hex(' ')}")
    print(f"last 16 bytes: {data[-16:].hex(' ')}")

    hits = find_magics(data)
    if hits:
        for label, offset in hits:
            print(f"plaintext magic candidate: {label} at offset {offset}")
    else:
        print("plaintext magic candidate: none")


def write_decoded(output: pathlib.Path, kind: str, decoded: Any, plaintext: bytes) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if kind in {"json", "msgpack", "msgpack-raw"}:
        output.write_text(
            json.dumps(decoded, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        return

    if kind == "utf8-text":
        output.write_text(decoded, encoding="utf-8")
        return

    output.write_bytes(plaintext)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=pathlib.Path, help="encrypted response file")
    parser.add_argument("-o", "--output", type=pathlib.Path, help="decoded output path")
    parser.add_argument("--plaintext-out", type=pathlib.Path, help="write raw decrypted plaintext")
    parser.add_argument("--mode", choices=("cbc", "gcm"), default="cbc", help="AES mode to use; default: cbc")
    parser.add_argument("--key", help="AES key as hex, base64, or UTF-8 text; prefixes: hex:, b64:, text:")
    parser.add_argument("--iv", help="AES-CBC IV / AES-GCM nonce as hex, base64, or UTF-8 text; prefixes: hex:, b64:, text:")
    parser.add_argument("--tag", help="AES-GCM auth tag; if omitted, the last 16 ciphertext bytes are used")
    parser.add_argument("--no-unpad", action="store_true", help="keep PKCS#7 padding")
    args = parser.parse_args()

    data = args.input.read_bytes()
    print_diagnostics(args.input, data)

    if not args.key or not args.iv:
        print("\nNo --key/--iv supplied, so only diagnostics were run.")
        print("For AES-CBC, rerun with: --key <16/24/32 bytes> --iv <16 bytes>")
        print("For AES-GCM, rerun with: --mode gcm --key <16/24/32 bytes> --iv <nonce> [--tag <tag>]")
        print("Use prefixes to avoid ambiguity, for example: --key hex:001122... --iv text:abcdef0123456789")
        return 0

    key = parse_bytes(args.key, name="key")
    iv = parse_bytes(args.iv, name="iv")
    if len(key) not in (16, 24, 32):
        raise SystemExit(f"AES key must be 16, 24, or 32 bytes; got {len(key)}")
    if args.mode == "gcm":
        if args.tag:
            tag = parse_bytes(args.tag, name="tag")
            ciphertext = data
        else:
            if len(data) < 16:
                raise SystemExit("AES-GCM ciphertext is too short to contain a 16-byte tag")
            ciphertext = data[:-16]
            tag = data[-16:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        print("gcm tag: valid")
    else:
        if len(iv) != AES.block_size:
            raise SystemExit(f"AES-CBC IV must be 16 bytes; got {len(iv)}")
        if len(data) % AES.block_size != 0:
            raise SystemExit("ciphertext length is not a multiple of 16 bytes")

        cipher = AES.new(key, AES.MODE_CBC, iv)
        plaintext = cipher.decrypt(data)

    if args.mode == "cbc" and not args.no_unpad:
        try:
            plaintext = remove_pkcs7_padding(plaintext)
            print("padding: valid PKCS#7, removed")
        except ValueError as exc:
            print(f"padding: {exc}; keeping raw decrypted bytes")

    inflated, compression = maybe_decompress(plaintext)
    if compression:
        print(f"compression: {compression}, inflated to {len(inflated)} bytes")
        plaintext = inflated
    else:
        print("compression: none detected")

    kind, decoded = decode_payload(plaintext)
    print(f"decoded as: {kind}")
    print(f"plaintext size: {len(plaintext)} bytes")
    print(f"plaintext first 32 bytes: {plaintext[:32].hex(' ')}")

    if args.plaintext_out:
        args.plaintext_out.write_bytes(plaintext)
        print(f"raw plaintext written: {args.plaintext_out}")

    if args.output:
        write_decoded(args.output, kind, decoded, plaintext)
        print(f"decoded output written: {args.output}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
