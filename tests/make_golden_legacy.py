#!/usr/bin/env python3
"""Regenerate golden_legacy_plans.json from the flasher as it was BEFORE the
variant/bench work (commit 40595f3), run unmodified against today's products.json.

    python tests/make_golden_legacy.py            # needs git history for 40595f3

The golden is the "before" half of the regression test: Ropener, Glasscalibur and
Generic board must resolve to byte-identical plans in the current flasher.
"""
import importlib.util
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LEGACY = "40595f3"
sys.path.insert(0, HERE)
from plans import resolve_plans, serialize  # noqa: E402

src = subprocess.run(["git", "show", f"{LEGACY}:valar_flasher.py"], cwd=ROOT,
                     capture_output=True, check=True).stdout
with tempfile.TemporaryDirectory() as td:
    p = os.path.join(td, "valar_flasher_legacy.py")
    with open(p, "wb") as f:
        f.write(src)
    spec = importlib.util.spec_from_file_location("valar_flasher_legacy", p)
    legacy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(legacy)
    # The legacy products.json at the same commit -- the "before" input.
    pj = os.path.join(td, "products.json")
    with open(pj, "wb") as f:
        f.write(subprocess.run(["git", "show", f"{LEGACY}:products.json"], cwd=ROOT,
                               capture_output=True, check=True).stdout)
    out = serialize(resolve_plans(legacy, pj))
with open(os.path.join(HERE, "golden_legacy_plans.json"), "wb") as f:
    f.write(out)
print(f"wrote golden_legacy_plans.json from {LEGACY} ({len(out)} bytes)")
