"""Patch upstream OpenTAKServer 1.7.11 bugs in the installed package.

Both fixes are idempotent and abort the build if their anchor is missing, so a
version bump can never silently drop them. Upstream sources ship with CRLF line
endings, so anchors are matched with \r?\n tolerance.

Run as: python3 fix_eud_handler.py [site-packages/opentakserver]
"""

import re
import sys
from pathlib import Path

PKG = Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else "/app/venv/lib/python3.13/site-packages/opentakserver"
)


def patch(path: Path, pattern: str, replacement: str, applied: str, label: str) -> None:
    # read_text() applies universal newlines, so the upstream CRLF is normalised
    # to LF here and written back as LF. Harmless for Python sources.
    text = path.read_text()
    if re.search(applied, text, flags=re.M):
        print(f"[skip] {label}: already applied")
        return
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    if count != 1:
        sys.exit(f"[FAIL] {label}: anchor not found in {path}")
    path.write_text(new_text)
    print(f"[ok]   {label}")


# 1. eud_handler.py imports the EudHandler *module* where the class is required,
#    so it gets passed as RequestHandlerClass and every plain-TCP (8088)
#    connection dies with "TypeError: 'module' object is not callable".
#    EudHandlerSSL.py already uses the correct form, which is exactly why only
#    the TCP listener was affected.
patch(
    PKG / "eud_handler" / "eud_handler.py",
    r"^from opentakserver\.eud_handler import EudHandler[ \t]*$",
    "from opentakserver.eud_handler.EudHandler import EudHandler",
    r"^from opentakserver\.eud_handler\.EudHandler import EudHandler[ \t]*$",
    "eud_handler.py: import the class, not the module",
)

# 2. setup() opens the RabbitMQ channel asynchronously via pika.SelectConnection,
#    but handle_cot() registers the client only on the FIRST CoT
#    ("if event and not self.uid"). parse_device_info() then guards its
#    queue_bind/basic_consume block with "if self.rabbit_channel and ...", so a
#    client whose self-report arrives before the channel opens is silently
#    skipped and stays unbound for the entire connection -- no crash, no retry.
#    ATAK sends its self-report immediately on connect, so it loses this race
#    routinely. Block in setup() (which runs before handle()) until the channel
#    is ready, capped at 5s so a dead broker can't wedge the connection.
patch(
    PKG / "eud_handler" / "EudHandler.py",
    r"^([ \t]*)self\.iothread\.start\(\)([ \t]*)$",
    (
        r"\1self.iothread.start()\2\n"
        r"\1# Wait for the async channel before handle() reads the first CoT.\n"
        r"\1# Registration runs once, so losing the race leaves us unbound.\n"
        r"\1import time as _ots_time\n"
        r"\1for _ in range(100):\n"
        r"\1    if self.rabbit_channel and self.rabbit_channel.is_open:\n"
        r"\1        break\n"
        r"\1    _ots_time.sleep(0.05)"
    ),
    r"_ots_time",
    "EudHandler.py: await RabbitMQ channel in setup()",
)

print("patches complete")
