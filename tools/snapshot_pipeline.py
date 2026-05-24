#!/usr/bin/env python3
"""Prepare uploaded Project SEKAI snapshots for report generation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tools.translate_pjsk_json import MasterDB, annotate, read_json, write_json


JSON_SUFFIXES = {".json"}
PACKAGE_SUFFIXES = {".bin", ".bundle", ".bytes", ".dat", ".msgpack", ".mpack"}
SSSEKAI_REGIONS = {"jp", "tw", "en", "kr", "cn"}


class SnapshotPipelineError(ValueError):
    """A user-facing error for uploaded snapshot preparation."""


class TolerantMasterDB(MasterDB):
    """Use cached master data, but do not fail the whole upload if one table is missing."""

    def _download(self, name: str, path: Path) -> None:
        try:
            super()._download(name, path)
        except Exception:
            return


def prepare_snapshot_input(
    input_path: Path,
    output_dir: Path,
    *,
    locale: str = "tc",
    cache_dir: Path = Path("master_cache"),
    region: str = "tw",
) -> Path:
    """Return a readable JSON path suitable for the report builders."""

    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = input_path.suffix.lower()
    if suffix in JSON_SUFFIXES:
        return prepare_json_snapshot(input_path, output_dir, locale=locale, cache_dir=cache_dir)

    if suffix in PACKAGE_SUFFIXES or suffix not in JSON_SUFFIXES:
        return decode_package_snapshot(input_path, output_dir, locale=locale, cache_dir=cache_dir, region=region)

    raise SnapshotPipelineError("目前只接受 `.json`；包體/二進位檔請先用伺服器端解碼流程轉成 JSON。")


def prepare_json_snapshot(
    input_path: Path,
    output_dir: Path,
    *,
    locale: str,
    cache_dir: Path,
) -> Path:
    try:
        data = read_json(input_path)
    except json.JSONDecodeError as exc:
        raise SnapshotPipelineError(f"JSON 格式錯誤：{exc}") from exc

    if not isinstance(data, (dict, list)):
        raise SnapshotPipelineError("JSON 最外層必須是 object 或 array。")

    if has_readable_markers(data) and not has_compact_enum(data):
        return input_path

    master = TolerantMasterDB(locale, cache_dir, refresh=False)
    readable = annotate(data, master)
    output_path = output_dir / f"{input_path.stem}_readable.json"
    write_json(output_path, readable)
    return output_path


def has_readable_markers(value: Any, budget: int = 4000) -> bool:
    stack = [value]
    seen = 0
    while stack and seen < budget:
        current = stack.pop()
        seen += 1
        if isinstance(current, dict):
            if current.get("__compactTable__") is True:
                return True
            if any(key.endswith("_label") or key.endswith("_detail") or key.endswith("_iso") for key in current):
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def has_compact_enum(value: Any, budget: int = 4000) -> bool:
    stack = [value]
    seen = 0
    while stack and seen < budget:
        current = stack.pop()
        seen += 1
        if isinstance(current, dict):
            if "__ENUM__" in current:
                return True
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return False


def decode_package_snapshot(
    input_path: Path,
    output_dir: Path,
    *,
    locale: str,
    cache_dir: Path,
    region: str,
) -> Path:
    # PACKAGE/BUNDLE PROCESSING AREA:
    # Discord can receive the uploaded file. The first supported raw format is
    # an encrypted API/CDN response body, decoded by sssekai apidecrypt.
    #
    # Suggested steps for a production server:
    # 1. If the upload is an API/CDN response, run sssekai apidecrypt.
    # 2. If the upload is an obfuscated AssetBundle, run tools/deobfuscate_assetbundle.py.
    # 3. Use sssekai or an equivalent extractor to export the target MessagePack/JSON.
    # 4. Call prepare_json_snapshot() on the exported JSON so IDs are restored by master data.
    #
    # AssetBundle extraction is still intentionally separated from the API
    # response path because it may need game-version-specific asset tooling.
    decoded_json = decode_api_response_with_sssekai(input_path, output_dir, region=region)
    return prepare_json_snapshot(decoded_json, output_dir, locale=locale, cache_dir=cache_dir)


def decode_api_response_with_sssekai(input_path: Path, output_dir: Path, *, region: str) -> Path:
    sssekai = shutil.which("sssekai")
    if not sssekai:
        raise SnapshotPipelineError(
            "這個檔案看起來不是 JSON；要直接讀原始 response 需要先在 bot 主機安裝 sssekai。"
        )

    region = (region or os.getenv("SSSEKAI_REGION") or "tw").lower()
    if region not in SSSEKAI_REGIONS:
        raise SnapshotPipelineError(f"不支援的 sssekai region：{region}")

    output_path = output_dir / f"{input_path.stem}_sssekai.json"
    command = [
        sssekai,
        "apidecrypt",
        "--region",
        region,
        str(input_path.resolve()),
        str(output_path.resolve()),
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        result = subprocess.run(
            command,
            cwd=str(output_dir.resolve()),
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise SnapshotPipelineError("sssekai 解碼逾時，檔案可能不是 API response 或太大。") from exc

    if result.returncode != 0 or not output_path.exists():
        detail = (result.stderr or result.stdout or "").strip()
        if len(detail) > 500:
            detail = detail[:500] + "..."
        raise SnapshotPipelineError(
            "sssekai 無法解碼這個檔案。請確認它是 Project SEKAI API/CDN response；"
            "若是 AssetBundle 包體，仍需要另外的包體處理流程。"
            + (f"\n```text\n{detail}\n```" if detail else "")
        )

    try:
        read_json(output_path)
    except Exception as exc:
        raise SnapshotPipelineError("sssekai 有輸出檔案，但內容不是可讀 JSON。") from exc

    return output_path
