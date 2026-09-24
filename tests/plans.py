"""Resolve every chip-select product's flash plan from a flasher module WITHOUT a
board or the network: what it downloads, where it puts it, and the exact esptool
argv it issues.

The same function is run against the LEGACY flasher (40595f3, to produce
golden_legacy_plans.json) and against the current one (in the regression test),
so "byte-identical before and after" compares one harness's output on two
programs rather than two hand-written expectations.
"""
import io
import json
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def resolve_plans(mod, products_json_path):
    with open(products_json_path, encoding="utf-8") as f:
        products = json.load(f)["products"]
    with open(os.path.join(HERE, "fixture_release_assets.json"), encoding="utf-8") as f:
        releases = {r["repo"]: r for r in json.load(f)}
    out = {}
    saved = (mod.FW_ROOT, mod.get_latest, mod.urllib.request.urlopen, mod.run_esptool)
    with tempfile.TemporaryDirectory() as td:
        downloaded = []

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=None):
            downloaded.append(req.full_url.rsplit("/", 1)[-1])
            return Resp(b"")

        def get_latest(repo, log):
            r = releases[repo]
            return r["tag"], [{"name": n, "browser_download_url": "https://example.invalid/" + n}
                              for n in r["assets"]]

        calls = []
        mod.FW_ROOT = td
        mod.get_latest = get_latest
        mod.urllib.request.urlopen = urlopen
        mod.run_esptool = lambda args, timeout=180: (calls.append(list(args)), (0, ""))[1]
        try:
            for name, pcfg in sorted(products.items()):
                if pcfg.get("select", "chip") != "chip":
                    continue
                downloaded.clear()
                mod.sync_firmware(name, pcfg, lambda m: None, force=True)
                rel = lambda p: os.path.relpath(p, td).replace(os.sep, "/")
                plan = {"downloads": list(downloaded), "chips": {}}
                for chip in sorted((pcfg.get("assets") or {}).keys()):
                    calls.clear()
                    mod.flash("PORT", chip, mod.bin_path(name, chip))
                    plan["chips"][chip] = {"file": rel(mod.bin_path(name, chip)),
                                           "esptool": [rel(a) if a.startswith(td) else a for a in calls[0]]}
                out[name] = plan
        finally:
            mod.FW_ROOT, mod.get_latest, mod.urllib.request.urlopen, mod.run_esptool = saved
    return out


def serialize(plans) -> bytes:
    return (json.dumps(plans, indent=1, sort_keys=True) + "\n").encode()
