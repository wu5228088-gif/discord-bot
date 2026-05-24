#!/usr/bin/env python3
"""Run the CDN Frida hook and save binary dumps sent by the script."""

from __future__ import annotations

import argparse
import pathlib
import sys
import time

import frida


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-p", "--package", default="com.hermes.mk.asia")
    parser.add_argument("-s", "--script", type=pathlib.Path, default=pathlib.Path("frida_hooks/cdn_decrypt_hook.js"))
    parser.add_argument("-o", "--out-dir", type=pathlib.Path, default=pathlib.Path("frida_cdn_dumps"))
    parser.add_argument("--attach", action="store_true", help="attach to a running process instead of spawning")
    parser.add_argument("--frontmost", action="store_true", help="attach to the current foreground app")
    parser.add_argument("--wait", type=int, default=60, help="seconds to wait for --attach target; default: 60")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    source = args.script.read_text(encoding="utf-8")
    device = frida.get_usb_device(timeout=5)
    spawned = False

    if args.frontmost:
        app = device.get_frontmost_application()
        if app is None:
            print("unable to detect frontmost application", file=sys.stderr)
            return 2
        if app.pid == 0:
            print(f"frontmost app has no pid yet: {app.identifier} ({app.name})", file=sys.stderr)
            return 2
        pid = app.pid
        session = device.attach(pid)
        print(f"attached to frontmost app {app.identifier} ({app.name}) pid={pid}")
    elif args.attach:
        deadline = time.time() + args.wait
        target = None
        last_status = None
        while time.time() < deadline:
            processes = device.enumerate_processes()
            target = next((p for p in processes if p.name == args.package), None)
            if target is not None:
                break
            partial = [p for p in processes if args.package in p.name or "hermes" in p.name.lower() or "mk" == p.name.lower()]
            if partial:
                names = ", ".join(f"{p.pid}:{p.name}" for p in partial[:10])
                status = f"waiting for {args.package}; found only sub/related process(es): {names}"
            else:
                status = f"waiting for {args.package}..."
            if status != last_status:
                print(status)
                last_status = status
            time.sleep(1)

        if target is None:
            processes = device.enumerate_processes()
            print(f"unable to find running process: {args.package}", file=sys.stderr)
            print("visible processes:", file=sys.stderr)
            for process in sorted(processes, key=lambda p: p.name.lower())[:80]:
                print(f"  {process.pid:>6}  {process.name}", file=sys.stderr)
            return 2

        pid = target.pid
        session = device.attach(pid)
        print(f"attached to {target.name} pid={pid}")
    else:
        pid = device.spawn([args.package])
        session = device.attach(pid)
        spawned = True
        print(f"spawned {args.package} pid={pid}")

    def on_message(message, data):
        if message.get("type") == "send":
            payload = message.get("payload")
            if isinstance(payload, dict) and payload.get("type") == "dump" and data:
                stamp = int(time.time() * 1000)
                name = f"{stamp}_{payload.get('tag', 'dump')}_{payload.get('len', len(data))}.bin"
                safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in name)
                path = args.out_dir / safe_name
                path.write_bytes(data)
                print(f"[host dump] {path} ({len(data)} bytes)")
                return
            print(f"[send] {payload}")
            return
        if message.get("type") == "error":
            print("[script error]", message.get("stack") or message, file=sys.stderr)
            return
        print("[message]", message)

    script = session.create_script(source)
    script.on("message", on_message)
    script.load()

    if spawned:
        device.resume(pid)
        print("resumed")

    print("hook running; press Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("stopping")
    finally:
        session.detach()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
