#!/usr/bin/env python3
"""A fake esptool: no board. Logs every invocation, one JSON line each, with every
write's (offset, length) -- which is what "flash mode never touches NVS" is
asserted against.

    FAKE_ESPTOOL_LOG   the JSON-lines log (required)
    FAKE_CHIP          what flash_id reports (default ESP32-S3)
    FAKE_MAC           what read_mac reports (default 00:00:00:00:00:01)
    FAKE_FAIL          a subcommand that exits 2 (e.g. write_flash)
"""
import json
import os
import sys


def main(argv):
    args, opts = list(argv), {}
    while args and args[0].startswith("--"):
        k = args.pop(0)
        opts[k] = args.pop(0)
    sub = (args.pop(0) if args else "").replace("-", "_")
    rec = {"sub": sub, "port": opts.get("--port"), "chip": opts.get("--chip")}
    if sub == "write_flash":
        rec["writes"] = [[int(o, 0), os.path.getsize(p), os.path.basename(p)]
                         for o, p in zip(args[0::2], args[1::2])]
    with open(os.environ["FAKE_ESPTOOL_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    if os.environ.get("FAKE_FAIL", "").replace("-", "_") == sub:
        print(f"A fatal error occurred: fake failure of {sub}")
        return 2
    if sub == "flash_id":
        print(f"Detecting chip type... {os.environ.get('FAKE_CHIP', 'ESP32-S3')}")
        return 0
    if sub == "read_mac":
        print(f"MAC: {os.environ.get('FAKE_MAC', '00:00:00:00:00:01')}")
        return 0
    if sub == "write_flash":
        print("Hash of data verified.")
        return 0
    print(f"fake esptool: unsupported {sub!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
