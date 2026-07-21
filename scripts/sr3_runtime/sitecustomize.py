"""Keep sr3 child instances from blocking on this Mac's `.local` reverse DNS."""

from __future__ import annotations

import os
import socket
import sys


_original_getfqdn = socket.getfqdn
_local_name = (
    os.environ.get("RADARSAT_SR3_HOSTNAME", "").strip()
    or socket.gethostname().split(".", 1)[0]
    or "radar-sat"
)


def _stable_getfqdn(name: str = "") -> str:
    return _original_getfqdn(name) if name else _local_name


socket.getfqdn = _stable_getfqdn


def _remove_forwarded_sanity_action(argv: list[str]) -> None:
    """Repair the child command emitted by Sarracenia 3.2 ``sanity``.

    Sarracenia's process manager forwards the manager action into a new flow
    instance, producing ``instance.py --no N sanity start subscribe/config``.
    The instance parser only accepts ``start`` or ``foreground``.  Keep this
    workaround deliberately narrow so ordinary sr3 arguments are untouched.
    """

    if not argv or os.path.basename(argv[0]) != "instance.py":
        return
    try:
        start_index = argv.index("start", 1)
    except ValueError:
        return
    if "sanity" in argv[1:start_index]:
        argv.remove("sanity")


_remove_forwarded_sanity_action(sys.argv)
