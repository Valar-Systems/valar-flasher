#!/usr/bin/env python3
"""Add a chip-select product's image to known_public_runs.json -- ONLY what traces.

When a new Ropener / Glasscalibur / Generic release is refused with an UNTRACED
run, run this on a machine with PlatformIO (it traces against the framework
packages the image was built from):

    python tools/trace_public_runs.py firmware/Ropener/esp32c6.factory.bin [...]

Each image is scanned with --trace ~/.platformio/packages. A CLEAN image's traced
runs are merged into the list; an image with ANY untraced run or on-sight hit is
reported and NOTHING is added for it -- the list only ever holds runs found
verbatim in a public input, never "whatever the refused image contained".
"""
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import scan_image  # noqa: E402

LIST = os.path.join(ROOT, "known_public_runs.json")
PACKAGES = os.environ.get("PLATFORMIO_PACKAGES", os.path.join(os.path.expanduser("~"), ".platformio", "packages"))


def main(images):
    if not images or not os.path.isdir(PACKAGES):
        print(__doc__ if not images else f"no PlatformIO packages at {PACKAGES}")
        return 2
    with open(LIST, encoding="utf-8") as f:
        known = json.load(f)
    runs, bad = set(known["public_runs"]), 0
    for img in images:
        with open(img, "rb") as f:
            data = f.read()
        v = scan_image.scan(data, "esp_image", trace_roots=[PACKAGES], name=os.path.basename(img))
        print("\n".join(v.lines))
        if scan_image.exit_code(v) != 0:
            print(f"NOT ADDED: {img} did not scan clean against the framework packages\n")
            bad += 1
            continue
        runs |= set(v.report["public_runs"])
        known["images"].append({"image": os.path.basename(img), "sha256": v.report["image_sha256"]})
    if bad < len(images):   # write only when something was actually added
        known["public_runs"] = sorted(runs)
        known["traced"] = f"{datetime.date.today().isoformat()}, scan_image.py --trace {PACKAGES}"
        with open(LIST, "w", encoding="utf-8", newline="\n") as f:
            json.dump(known, f, indent=1)
            f.write("\n")
    print(f"{len(runs)} public runs in {os.path.basename(LIST)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
