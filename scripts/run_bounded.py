#!/usr/bin/env python3
"""Give an operational command and all of its children a bounded lifetime."""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


class Interrupted(Exception):
    def __init__(self, signum: int):
        self.signum = signum


def stop_group(child: subprocess.Popen, grace: float) -> None:
    def signal_group(signum: int) -> bool:
        try:
            os.killpg(child.pid, signum)
            return True
        except ProcessLookupError:
            return False

    if not signal_group(signal.SIGTERM):
        child.wait()
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        child.poll()  # Reap the leader, including when descendants outlive it.
        if not signal_group(0):
            return
        time.sleep(0.05)
    signal_group(signal.SIGKILL)
    child.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--grace-seconds", type=float, default=5)
    parser.add_argument("--label", default="Operational command")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.seconds <= 0 or args.grace_seconds < 0 or not command:
        parser.error("positive --seconds, nonnegative grace, and a command are required")

    def interrupted(signum: int, _frame: object) -> None:
        raise Interrupted(signum)

    child = subprocess.Popen(command, start_new_session=True)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    try:
        result = child.wait(timeout=args.seconds)
        return result if result >= 0 else 128 - result
    except subprocess.TimeoutExpired:
        print(f"{args.label} exceeded {args.seconds:g}s; terminating its process group. "
              "Pending requests will be retried.", file=sys.stderr, flush=True)
        return 124
    except Interrupted as error:
        return 128 + error.signum
    finally:
        # A wrapper must not leave descendants holding the publication lock,
        # even if its direct child exited successfully before its children.
        for signum in (signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, signal.SIG_IGN)
        stop_group(child, args.grace_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
