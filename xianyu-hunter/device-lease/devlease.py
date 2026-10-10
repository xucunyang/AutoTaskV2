#!/usr/bin/env python3
"""Device lease manager: coordinate ADB devices across multiple OpenCode sessions.

Usage:
  devlease.py acquire --device DEV --session SID [--ttl SEC] [--task TEXT]
  devlease.py release --device DEV --session SID
  devlease.py renew   --device DEV --session SID [--ttl SEC]
  devlease.py status

Exit codes:
  0 = success (acquired / released / renewed)
  2 = busy (held by another live session)
  3 = not owner (release/renew failed)
  1 = error
"""
import argparse
import json
import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LEASE_DIR = os.path.join(BASE, "leases")


def lease_path(device: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in device)
    return os.path.join(LEASE_DIR, f"lease-{safe}.json")


def read_lease(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_lease(path: str, data: dict):
    os.makedirs(LEASE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=LEASE_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # atomic
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p):
        p.add_argument("--device", required=True, help="device serial or adb address")
        p.add_argument("--session", required=True, help="unique session id (e.g. opencode session or agent name)")

    p_acq = sub.add_parser("acquire")
    add_common(p_acq)
    p_acq.add_argument("--ttl", type=int, default=1200, help="lease seconds, auto-expire (default 1200)")
    p_acq.add_argument("--task", default="", help="what this session is doing (for humans)")

    p_rel = sub.add_parser("release")
    add_common(p_rel)

    p_ren = sub.add_parser("renew")
    add_common(p_ren)
    p_ren.add_argument("--ttl", type=int, default=1200)

    sub.add_parser("status")

    args = ap.parse_args()

    if args.cmd == "status":
        if not os.path.isdir(LEASE_DIR):
            print("(no leases)")
            return
        now = time.time()
        for fn in sorted(os.listdir(LEASE_DIR)):
            if not fn.startswith("lease-"):
                continue
            l = read_lease(os.path.join(LEASE_DIR, fn))
            if not l:
                continue
            remain = int(l.get("expires", 0) - now)
            state = "LIVE" if remain > 0 else "EXPIRED"
            print(f"[{state}] {l.get('device')}  holder={l.get('session')}  "
                  f"remain={remain}s  task={l.get('task','')}")
        return

    path = lease_path(args.device)
    now = time.time()
    cur = read_lease(path)

    if args.cmd == "acquire":
        if cur and cur.get("session") == args.session:
            cur["expires"] = now + args.ttl
            cur["task"] = args.task or cur.get("task", "")
            write_lease(path, cur)
            print(f"RENEWED {args.device} -> {args.session} ttl={args.ttl}s")
            return
        if cur and cur.get("expires", 0) > now:
            remain = int(cur["expires"] - now)
            print(f"BUSY {args.device} held by [{cur.get('session')}] "
                  f"task={cur.get('task','')} remain={remain}s")
            sys.exit(2)
        data = {
            "device": args.device,
            "session": args.session,
            "task": args.task,
            "acquired": now,
            "expires": now + args.ttl,
        }
        write_lease(path, data)
        print(f"ACQUIRED {args.device} -> {args.session} ttl={args.ttl}s")
        return

    if args.cmd == "release":
        if cur and cur.get("session") == args.session:
            if os.path.exists(path):
                os.remove(path)
            print(f"RELEASED {args.device}")
            return
        if cur:
            print(f"NOT_OWNER {args.device} held by [{cur.get('session')}]")
            sys.exit(3)
        print(f"RELEASED {args.device} (no lease)")
        return

    if args.cmd == "renew":
        if cur and cur.get("session") == args.session:
            cur["expires"] = now + args.ttl
            write_lease(path, cur)
            print(f"RENEWED {args.device} ttl={args.ttl}s")
            return
        print(f"NOT_OWNER {args.device}")
        sys.exit(3)


if __name__ == "__main__":
    main()
