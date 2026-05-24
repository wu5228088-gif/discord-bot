#!/usr/bin/env python3
"""Analyze a Project SEKAI upload, including raw encrypted API responses."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.analyze_pjsk_snapshot import DEFAULT_MASTER_CACHE, build_mysekai_report, build_profile_report
from tools.snapshot_pipeline import prepare_snapshot_input


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-cache", type=Path, default=DEFAULT_MASTER_CACHE)
    parser.add_argument("--locale", default="tc")
    parser.add_argument("--region", default="tw", help="sssekai API decrypt region: jp/tw/en/kr/cn")

    subparsers = parser.add_subparsers(dest="command", required=True)

    mysekai = subparsers.add_parser("mysekai", help="Decode/annotate and analyze a MySekai upload.")
    mysekai.add_argument("input_file", type=Path)
    mysekai.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / "upload_mysekai")
    mysekai.add_argument("--no-maps", action="store_true")
    mysekai.set_defaults(func=run_mysekai)

    profile = subparsers.add_parser("profile", help="Decode/annotate and analyze a suite/profile upload.")
    profile.add_argument("input_file", type=Path)
    profile.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "reports" / "upload_profile")
    profile.add_argument("--fetch-hisekai", action="store_true")
    profile.add_argument("--server", default="tw")
    profile.add_argument("--timeout", type=int, default=15)
    profile.add_argument("--fetch-top100-history", action="store_true")
    profile.add_argument("--max-events", type=int, default=40)
    profile.set_defaults(func=run_profile)

    return parser


def prepare(args: argparse.Namespace) -> Path:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    return prepare_snapshot_input(
        args.input_file,
        args.output_dir,
        locale=args.locale,
        cache_dir=args.master_cache,
        region=args.region,
    )


def run_mysekai(args: argparse.Namespace) -> int:
    json_file = prepare(args)
    report_args = SimpleNamespace(
        json_file=json_file,
        output_dir=args.output_dir,
        master_cache=args.master_cache,
        locale=args.locale,
        no_maps=args.no_maps,
    )
    return build_mysekai_report(report_args)


def run_profile(args: argparse.Namespace) -> int:
    json_file = prepare(args)
    report_args = SimpleNamespace(
        json_file=json_file,
        output_dir=args.output_dir,
        master_cache=args.master_cache,
        locale=args.locale,
        fetch_hisekai=args.fetch_hisekai,
        server=args.server,
        timeout=args.timeout,
        current_rank_url=None,
        history_url=None,
        fetch_top100_history=args.fetch_top100_history,
        max_events=max(args.max_events, 0),
        event_list_url="https://api.hisekai.org/{server}/event/list",
        event_top100_url="https://api.hisekai.org/{server}/event/{event_id}/top100",
    )
    return build_profile_report(report_args)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
