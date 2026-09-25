#!/usr/bin/env python3
"""Stands in for Blipscope's scripts/provision_one.py: same argv, same output
contract (STEP write, STEP verify, then RESULT OK <id> | RESULT FAIL <reason>).

    fake_provisioner.py <port> <mac> [--log CSV] ...
    FAKE_PROV_EXIT     exit code to use (default 0 = verified)
    FAKE_PROV_LIE      "1" = exit 0 but print no RESULT OK (a provisioner that
                       did not actually verify must not produce a DONE tile)
"""
import csv
import os
import sys

port, mac, rest = sys.argv[1], sys.argv[2], sys.argv[3:]
log = rest[rest.index("--log") + 1] if "--log" in rest else None
# The real provisioner needs the token FILE's path from the flasher; refuse without it,
# so every DONE in the flasher's tests also proves the flasher passed it.
if "--token-file" not in rest or not os.path.exists(rest[rest.index("--token-file") + 1]):
    print("RESULT FAIL no --token-file passed (or the file is missing)")
    sys.exit(3)
print("STEP write", flush=True)
print("STEP verify", flush=True)
code = int(os.environ.get("FAKE_PROV_EXIT", "0"))
if code != 0:
    print("RESULT FAIL 02:00:00:00:00:01: PROVISION_TOKEN rejected (403) -- the token file does not match the Worker's")
    sys.exit(code)
if os.environ.get("FAKE_PROV_LIE") == "1":
    sys.exit(0)
dev = "fake" + mac.replace(":", "")[-12:]
if log:
    new = not os.path.exists(log)
    with open(log, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["utc", "env", "mac", "device_id", "source"])
        w.writerow(["2026-01-01T00:00:00+00:00", "test", mac, dev, "provisioned"])
print(f"RESULT OK {dev}")
