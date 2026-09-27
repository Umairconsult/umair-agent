"""Runs at the start of every GitHub run (self-hosted or not). Writes settings.env from the
SETTINGS_ENV secret, and masks every value in it so nothing leaks into the public run log.
Kept as its own file (instead of a bash heredoc in the workflow) so it works the same on
Windows, Linux, or anything else - no dependency on bash being installed."""
import os
import re
import sys

s = os.environ.get("SETTINGS_ENV", "")
if not s.strip():
    print("PROBLEM: the secret SETTINGS_ENV is empty or missing. Add it in Settings > Secrets and variables > Actions.")
    sys.exit(1)

open("settings.env", "w", encoding="utf-8").write(s)

for line in s.splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    val = re.split(r"\s*<--", line.split("=", 1)[1], maxsplit=1)[0].strip()
    if len(val) >= 6:
        print("::add-mask::" + val)
