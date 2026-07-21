#!/usr/bin/env python3
"""Start Sarracenia without a blocking reverse-DNS lookup of this Mac."""

from __future__ import annotations

import os
import socket
from collections import namedtuple


_original_getfqdn = socket.getfqdn
_local_name = (
    os.environ.get("RADARSAT_SR3_HOSTNAME", "").strip()
    or socket.gethostname().split(".", 1)[0]
    or "radar-sat"
)


def _stable_getfqdn(name: str = "") -> str:
    # sr3 uses the no-argument form only to construct local queue/process names.
    # Resolving this Mac's `.local` hostname can block for minutes when mDNS is
    # unavailable; named remote lookups retain the standard socket behaviour.
    return _original_getfqdn(name) if name else _local_name


socket.getfqdn = _stable_getfqdn

from sarracenia import sr as sr_module  # noqa: E402


# psutil reports the Homebrew macOS interpreter as ``Python``.  Sarracenia
# 3.2's process discovery checks for the lowercase substring ``python`` and
# otherwise discards the process before examining its instance.py command.
# Normalize only Python interpreter names before delegating to the upstream
# filter so status/sanity can see the subscribers it started.
_original_filter_sr_proc = sr_module.sr_GlobalState._filter_sr_proc
_BasicProcessMemory = namedtuple("_BasicProcessMemory", "rss vms uss")


def _filter_sr_proc_case_insensitive_python(self, process: dict) -> None:
    command = process.get("cmdline") or []
    if (
        len(command) > 1
        and os.path.basename(command[1]) == "sr3_entry.py"
    ):
        # Wrapper invocations are short-lived managers, never flow workers.
        # Excluding every concurrent invocation keeps a supervisor sanity pass
        # from appearing as a sixth process (or as a transient stray) in status.
        return
    name = process.get("name", "")
    if "python" in name.lower():
        process["name"] = name.lower()
    # macOS can deny the task-inspection call behind memory_full_info() even
    # for another process owned by the same user.  Upstream then dereferences
    # None and silently drops that process.  Basic RSS/VMS remain available;
    # leave unknown USS at zero rather than reporting an invented value.
    if process.get("memory_full_info") is None and process.get("pid"):
        try:
            memory = sr_module.psutil.Process(process["pid"]).memory_info()
            process["memory_full_info"] = _BasicProcessMemory(
                rss=memory.rss,
                vms=memory.vms,
                uss=0,
            )
        except (AttributeError, sr_module.psutil.Error):
            pass
    return _original_filter_sr_proc(self, process)


sr_module.sr_GlobalState._filter_sr_proc = _filter_sr_proc_case_insensitive_python
main = sr_module.main


if __name__ == "__main__":
    raise SystemExit(main())
