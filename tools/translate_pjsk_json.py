#!/usr/bin/env python3
"""Annotate decoded Project SEKAI JSON files with readable master-data labels."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_URLS = {
    "tc": "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-tc-diff/main",
    "jp": "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-diff/main",
    "en": "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-en-diff/main",
}

MASTER_FILES = {
    "areas",
    "avatarAccessories",
    "avatarCoordinates",
    "avatarCostumes",
    "avatarMotions",
    "avatarSkinColors",
    "beginnerMissionV2s",
    "bonds",
    "bondsHonorWords",
    "bondsHonors",
    "boostItems",
    "cardEpisodes",
    "cards",
    "characterMissionV2s",
    "characterMissionV2ParameterGroups",
    "characterProfiles",
    "costume3ds",
    "costume3dShopItems",
    "eventItems",
    "events",
    "gameCharacters",
    "gameCharacterUnits",
    "gachas",
    "gachaTickets",
    "honors",
    "liveMissionPeriods",
    "materials",
    "musics",
    "mysekaiBlueprints",
    "mysekaiCanvases",
    "mysekaiFixtures",
    "mysekaiGates",
    "mysekaiGameCharacterUnitGroups",
    "mysekaiItems",
    "mysekaiMaterials",
    "mysekaiPhenomenas",
    "mysekaiRefreshTimePeriods",
    "mysekaiSites",
    "mysekaiTools",
    "normalMissions",
    "practiceTickets",
    "skillPracticeTickets",
    "stamps",
    "virtualLives",
    "virtualLiveSchedules",
    "virtualShops",
}

FIELD_TO_TABLE = {
    "areaId": "areas",
    "avatarAccessoryId": "avatarAccessories",
    "avatarCoordinateId": "avatarCoordinates",
    "avatarCostumeId": "avatarCostumes",
    "avatarMotionId": "avatarMotions",
    "avatarSkinColorId": "avatarSkinColors",
    "beginnerMissionV2Id": "beginnerMissionV2s",
    "bondsGroupId": "bonds",
    "bondsHonorId": "bondsHonors",
    "bondsHonorWordId": "bondsHonorWords",
    "boostItemId": "boostItems",
    "cardEpisodeId": "cardEpisodes",
    "cardId": "cards",
    "characterId": "gameCharacters",
    "costume3dId": "costume3ds",
    "costume3dShopItemId": "costume3dShopItems",
    "eventId": "events",
    "eventItemId": "eventItems",
    "gachaId": "gachas",
    "gachaTicketId": "gachaTickets",
    "gameCharacterId": "gameCharacters",
    "gameCharacterUnitId": "gameCharacterUnits",
    "honorId": "honors",
    "liveMissionPeriodId": "liveMissionPeriods",
    "materialId": "materials",
    "missionId": "characterMissionV2s",
    "musicId": "musics",
    "mysekaiBlueprintId": "mysekaiBlueprints",
    "mysekaiCanvasId": "mysekaiCanvases",
    "mysekaiFixtureId": "mysekaiFixtures",
    "mysekaiGameCharacterUnitGroupId": "mysekaiGameCharacterUnitGroups",
    "mysekaiGateId": "mysekaiGates",
    "mysekaiItemId": "mysekaiItems",
    "mysekaiMaterialId": "mysekaiMaterials",
    "mysekaiPhenomenaId": "mysekaiPhenomenas",
    "mysekaiRefreshTimePeriodId": "mysekaiRefreshTimePeriods",
    "mysekaiSiteId": "mysekaiSites",
    "mysekaiToolId": "mysekaiTools",
    "normalMissionId": "normalMissions",
    "practiceTicketId": "practiceTickets",
    "skillPracticeTicketId": "skillPracticeTickets",
    "stampId": "stamps",
    "virtualLiveId": "virtualLives",
    "virtualLiveScheduleId": "virtualLiveSchedules",
    "virtualShopId": "virtualShops",
}

TIME_KEYS = {
    "now",
    "scheduleDate",
    "lastRefreshedAt",
    "registeredAt",
    "recoveryAt",
    "expiredAt",
    "obtainedAt",
    "approvedAt",
    "requestExpiredAt",
    "startAt",
    "endAt",
    "lastSpinAt",
}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


class MasterDB:
    def __init__(self, locale: str, cache_dir: Path, refresh: bool = False) -> None:
        self.locale = locale
        self.cache_dir = cache_dir
        self.refresh = refresh
        self.tables: dict[str, dict[int, dict[str, Any]]] = {}
        self.raw_tables: dict[str, list[dict[str, Any]]] = {}
        self.required_tables: set[str] = set(MASTER_FILES)

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.locale]

    def table(self, name: str) -> dict[int, dict[str, Any]]:
        if name in self.tables:
            return self.tables[name]

        rows = self.raw_table(name)
        indexed = {}
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("id"), int):
                indexed[row["id"]] = row
            if name == "characterProfiles" and isinstance(row, dict) and isinstance(row.get("characterId"), int):
                indexed[row["characterId"]] = row
        self.tables[name] = indexed
        return indexed

    def raw_table(self, name: str) -> list[dict[str, Any]]:
        if name in self.raw_tables:
            return self.raw_tables[name]

        path = self.cache_dir / self.locale / f"{name}.json"
        if self.refresh or not path.exists():
            self._download(name, path)

        try:
            data = read_json(path)
        except FileNotFoundError:
            data = []
        if not isinstance(data, list):
            data = []
        self.raw_tables[name] = data
        return data

    def _download(self, name: str, path: Path) -> None:
        url = f"{self.base_url}/{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                content = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return
            raise
        path.write_bytes(content)

    def label_for(self, table_name: str, value: Any) -> str | None:
        if not isinstance(value, int):
            return None
        row = self.table(table_name).get(value)
        if not row:
            return None
        return self.row_label(table_name, row)

    def detail_for(self, table_name: str, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, int):
            return None
        row = self.table(table_name).get(value)
        if not row:
            return None

        if table_name == "mysekaiGameCharacterUnitGroups":
            names = self.character_group_names(row)
            detail = {"id": value, "characters": names}
            if names:
                detail["label"] = " + ".join(names)
            return detail

        detail_keys = [
            "id",
            "name",
            "englishName",
            "firstName",
            "givenName",
            "unit",
            "title",
            "seq",
            "startHour",
            "endHour",
            "characterMissionType",
            "assetbundleName",
        ]
        return {key: row[key] for key in detail_keys if key in row}

    def row_label(self, table_name: str, row: dict[str, Any]) -> str:
        if table_name == "mysekaiGameCharacterUnitGroups":
            names = self.character_group_names(row)
            return " + ".join(names) if names else f"id {row.get('id')}"

        if table_name == "gameCharacterUnits":
            character_id = row.get("gameCharacterId")
            unit = row.get("unit")
            name = self.label_for("gameCharacters", character_id)
            return f"{name} ({unit})" if name and unit else name or str(row.get("id"))

        if table_name == "gameCharacters":
            first_name = row.get("firstName") or ""
            given_name = row.get("givenName") or ""
            full_name = f"{first_name}{given_name}".strip()
            return full_name or str(row.get("id"))

        if table_name == "mysekaiRefreshTimePeriods":
            start = row.get("startHour")
            end = row.get("endHour")
            if isinstance(start, int) and isinstance(end, int):
                return f"{start % 24:02d}:00-{end % 24:02d}:00"

        if table_name == "characterMissionV2s":
            mission_type = row.get("characterMissionType")
            parameter = row.get("requirement")
            return f"{mission_type} {parameter}".strip() if parameter is not None else str(mission_type or row.get("id"))

        for key in ("name", "englishName", "title", "assetbundleName", "unit"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
        return f"id {row.get('id')}"

    def character_group_names(self, row: dict[str, Any]) -> list[str]:
        names = []
        for key in sorted(k for k in row if k.startswith("gameCharacterUnitId")):
            unit_id = row.get(key)
            unit_row = self.table("gameCharacterUnits").get(unit_id)
            if not unit_row:
                continue
            name = self.label_for("gameCharacters", unit_row.get("gameCharacterId"))
            names.append(name or f"gameCharacterUnitId {unit_id}")
        return names


def timestamp_label(value: Any) -> str | None:
    if not isinstance(value, int):
        return None
    if value <= 1_000_000_000_000:
        return None
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def compact_table_rows(value: dict[str, Any]) -> list[dict[str, Any]] | None:
    if "__ENUM__" not in value:
        return None
    columns = [
        key
        for key, item in value.items()
        if key != "__ENUM__" and isinstance(item, list)
    ]
    if not columns:
        return None
    row_count = max(len(value[column]) for column in columns)
    rows = []
    enums = value.get("__ENUM__", {})
    for index in range(row_count):
        row = {}
        for column in columns:
            items = value[column]
            item = items[index] if index < len(items) else None
            row[column] = item
            enum_values = enums.get(column)
            if isinstance(enum_values, list) and isinstance(item, int) and 0 <= item < len(enum_values):
                row[f"{column}_label"] = enum_values[item]
        rows.append(row)
    return rows


def annotate(data: Any, master: MasterDB) -> Any:
    if isinstance(data, list):
        return [annotate(item, master) for item in data]

    if not isinstance(data, dict):
        return data

    expanded = compact_table_rows(data)
    if expanded is not None:
        return {
            "__compactTable__": True,
            "rowCount": len(expanded),
            "rows": [annotate(row, master) for row in expanded],
        }

    result: dict[str, Any] = {}
    for key, value in data.items():
        annotated_value = annotate(value, master)
        result[key] = annotated_value

        table_name = FIELD_TO_TABLE.get(key)
        if table_name:
            label = master.label_for(table_name, value)
            if label:
                result[f"{key}_label"] = label
            detail = master.detail_for(table_name, value)
            if detail:
                result[f"{key}_detail"] = detail

        if key.startswith("gameCharacterUnitId") and isinstance(value, int):
            label = master.label_for("gameCharacterUnits", value)
            if label:
                result[f"{key}_label"] = label
                detail = master.detail_for("gameCharacterUnits", value)
                if detail:
                    result[f"{key}_detail"] = detail

        if key in TIME_KEYS or key.endswith("At") or key.endswith("Date"):
            label = timestamp_label(value)
            if label:
                result[f"{key}_iso"] = label

    return result


def output_path_for(input_path: Path, output_dir: Path | None) -> Path:
    name = f"{input_path.stem}_readable{input_path.suffix}"
    return (output_dir / name) if output_dir else input_path.with_name(name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_files", nargs="+", type=Path)
    parser.add_argument("--locale", choices=sorted(BASE_URLS), default="tc")
    parser.add_argument("--cache-dir", type=Path, default=Path("master_cache"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--refresh-master", action="store_true")
    args = parser.parse_args()

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    master = MasterDB(args.locale, args.cache_dir, args.refresh_master)
    for input_path in args.json_files:
        data = read_json(input_path)
        readable = annotate(data, master)
        output_path = output_path_for(input_path, args.output_dir)
        write_json(output_path, readable)
        print(f"wrote {output_path}")

    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
