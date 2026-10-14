#!/usr/bin/env python3
"""A fake esptool: no board. Logs every invocation, one JSON line each, with every
write's (offset, length) -- which is what "flash mode never touches NVS" is
asserted against.

    FAKE_ESPTOOL_LOG   the JSON-lines log (required)
    FAKE_CHIP          what flash_id reports (default ESP32-S3)
    FAKE_CHIPS         JSON {port: chip} overriding FAKE_CHIP per port
    FAKE_MAC           what read_mac reports (default 00:00:00:00:00:01)
    FAKE_FAIL          a subcommand that exits 2 (e.g. write_flash)
    FAKE_FLASH         a file standing in for the board's flash, served by read_flash
                       (absent = a blank board: all 0xFF)
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
    chip = json.loads(os.environ.get("FAKE_CHIPS") or "{}").get(rec["port"]) or os.environ.get("FAKE_CHIP", "ESP32-S3")
    if sub == "write_flash":
        rec["writes"] = [[int(o, 0), os.path.getsize(p), os.path.basename(p)]
                         for o, p in zip(args[0::2], args[1::2])]
    if sub == "read_flash":
        rec["reads"] = [int(args[0], 0), int(args[1], 0)]
    with open(os.environ["FAKE_ESPTOOL_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    if os.environ.get("FAKE_FAIL", "").replace("-", "_") == sub:
        print(f"A fatal error occurred: fake failure of {sub}")
        return 2
    if sub == "flash_id":
        print(f"Detecting chip type... {chip}")
        return 0
    if sub == "read_mac":
        print(f"MAC: {os.environ.get('FAKE_MAC', '00:00:00:00:00:01')}")
        return 0
    if sub == "write_flash":
        print("Hash of data verified.")
        return 0
    if sub == "read_flash":
        off, size = rec["reads"]
        flash = b""
        if os.environ.get("FAKE_FLASH"):
            with open(os.environ["FAKE_FLASH"], "rb") as f:
                flash = f.read()
        data = flash[off:off + size]
        with open(args[2], "wb") as f:
            f.write(data + b"\xff" * (size - len(data)))
        print(f"Detecting chip type... {chip}")
        print(f"MAC: {os.environ.get('FAKE_MAC', '00:00:00:00:00:01')}")
        print(f"Read {size} bytes at {off:#010x}")
        return 0
    print(f"fake esptool: unsupported {sub!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
