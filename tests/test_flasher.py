#!/usr/bin/env python3
"""valar-flasher tests: no board, no network. esptool is tests/fake_esptool.py,
the provisioner is tests/fake_provisioner.py.

    python -m unittest discover -s tests -v
"""
import csv
import hashlib
import json
import re
import os
import secrets
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import valar_flasher as vf  # noqa: E402
from plans import resolve_plans, serialize  # noqa: E402

NVS_OFF, NVS_SIZE = 0x9000, 0x15000
VNAME = "Kit S3 1.28in (s3-128)"


def sha(b):
    return hashlib.sha256(b).hexdigest()


def make_variant(d, slug="s3-128", app_extra=b""):
    """A small but self-consistent image set, laid out like the real s3-128:
    bootloader 0x0, partitions 0x8000, NVS 0x9000..0x1E000 (preserved),
    boot_app0 0x1E000, app 0x20000."""
    os.makedirs(d, exist_ok=True)
    regions = [("bootloader", 0x0, b"\xe9" + b"B" * 200),
               ("partitions", 0x8000, b"\xaa\x50" + b"P" * 100),
               ("boot_app0", 0x1E000, b"\x00" * 12 + b"\xff" * (0x2000 - 12)),
               ("app", 0x20000, b"\xe9" + b"\0[build] env=blipscope-s3-128\0" + b"A" * 300 + app_extra + b"\0")]
    names = {"bootloader": f"bootloader-{slug}.bin", "partitions": f"partitions-{slug}.bin",
             "boot_app0": f"boot_app0-{slug}.bin", "app": f"firmware-{slug}.bin"}
    end = max(o + len(b) for _, o, b in regions)
    factory = bytearray(b"\xff" * end)
    for n, o, b in regions:
        factory[o:o + len(b)] = b
        open(os.path.join(d, names[n]), "wb").write(b)
    factory = bytes(factory)
    open(os.path.join(d, f"firmware-{slug}.factory.bin"), "wb").write(factory)
    m = {"v": 1, "slug": slug, "env": "blipscope-s3-128", "chip": "esp32s3",
         "factory": {"file": f"firmware-{slug}.factory.bin", "offset": "0x0", "size": len(factory), "sha256": sha(factory)},
         "regions": [{"name": n, "offset": hex(o), "file": names[n], "size": len(b), "sha256": sha(b)} for n, o, b in regions],
         "preserve": [{"name": "nvs", "offset": hex(NVS_OFF), "size": hex(NVS_SIZE)}],
         "scan": {"file": f"factory-scan-{slug}.json"}}
    json.dump(m, open(os.path.join(d, f"flash-manifest-{slug}.json"), "w"))
    json.dump({"v": 1, "image_sha256": sha(factory), "anchor": "[build] env=", "public_runs": []},
              open(os.path.join(d, f"factory-scan-{slug}.json"), "w"))
    return m


class Rig(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.saved = (vf.FW_ROOT, vf.ESPTOOL, vf.KNOWN_PUBLIC_FILE, vf.PROTECTED_FILE,
                      vf._port_serials, vf.PORT_ALLOW, vf.PROVISION_TOKEN_FILE)
        # A bench loop that never reaches its count must FAIL, not hang: in production it
        # runs until Ctrl-C, so every test gets a deadline that stops it. (Found when a
        # sabotage that made every board fail hung the suite instead of turning it red.)
        self._orig_loop = vf.bench_loop

        def _deadline_loop(s, stop, *a, **k):
            t = threading.Timer(20, stop.set)
            t.start()
            try:
                return self._orig_loop(s, stop, *a, **k)
            finally:
                t.cancel()
        vf.bench_loop = _deadline_loop
        # Never this machine's real token file: a test one, present by default.
        vf.PROVISION_TOKEN_FILE = os.path.join(self.td, "provision-token")
        with open(vf.PROVISION_TOKEN_FILE, "w") as f:
            f.write("test-provision-token\n")
        # Never this machine's real protected list or real ports.
        vf.PROTECTED_FILE = os.path.join(self.td, "protected-macs.json")
        vf._port_serials = lambda: {}
        vf.PORT_ALLOW = None
        vf.FW_ROOT = os.path.join(self.td, "firmware")
        vf.ESPTOOL = [sys.executable, os.path.join(HERE, "fake_esptool.py")]
        vf.KNOWN_PUBLIC_FILE = os.path.join(ROOT, "known_public_runs.json")
        self.log = os.path.join(self.td, "esptool.jsonl")
        os.environ["FAKE_ESPTOOL_LOG"] = self.log
        for k in ("FAKE_CHIP", "FAKE_MAC", "FAKE_FAIL", "FAKE_PROV_EXIT", "FAKE_PROV_LIE"):
            os.environ.pop(k, None)
        self.vcfg = {"chip": "esp32s3", "repo": "x/y", "asset": "firmware-s3-128.factory.bin",
                     "manifest": "flash-manifest-s3-128.json", "anchor": "[build] env="}
        self.vdir = vf.variant_dir("Blipscope", VNAME)
        self.manifest = make_variant(self.vdir)
        # The cache stands in for one synced from a TAGGED release; the release
        # checks refuse a set with no record, so the fixture must carry one.
        with open(os.path.join(self.vdir, vf.RELEASE_RECORD), "w") as f:
            json.dump({"tag": "v99", "prerelease": False}, f)
        self.saved_allow = vf.ALLOW_PRERELEASE
        vf.ALLOW_PRERELEASE = False
        self.msgs = []

    def tearDown(self):
        (vf.FW_ROOT, vf.ESPTOOL, vf.KNOWN_PUBLIC_FILE, vf.PROTECTED_FILE,
         vf._port_serials, vf.PORT_ALLOW, vf.PROVISION_TOKEN_FILE) = self.saved
        vf.ALLOW_PRERELEASE = self.saved_allow
        vf.bench_loop = self._orig_loop
        shutil.rmtree(self.td, ignore_errors=True)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        return [json.loads(l) for l in open(self.log, encoding="utf-8")]

    def writes(self):
        return [w for c in self.calls() if c["sub"] == "write_flash" for w in c["writes"]]


def touches_nvs(writes):
    return [w for w in writes if w[0] < NVS_OFF + NVS_SIZE and NVS_OFF < w[0] + w[1]]


class LegacyProductsUnchanged(unittest.TestCase):
    """Ropener, Glasscalibur and Generic board resolve to BYTE-IDENTICAL plans:
    same downloads, same destination files, same esptool argv, as the flasher
    at 40595f3 (tests/golden_legacy_plans.json, made by make_golden_legacy.py)."""

    def test_flash_plans_are_byte_identical_to_40595f3(self):
        now = serialize(resolve_plans(vf, os.path.join(ROOT, "products.json")))
        with open(os.path.join(HERE, "golden_legacy_plans.json"), "rb") as f:
            # LF-normalised: git stores LF, and a Windows checkout hands us CRLF.
            golden = f.read().replace(b"\r\n", b"\n")
        self.assertEqual(now, golden)

    def test_their_products_json_entries_are_unchanged(self):
        now = json.load(open(os.path.join(ROOT, "products.json")))["products"]
        self.assertEqual(sorted(k for k in now if not vf.is_variant(now[k])),
                         ["Generic board", "Glasscalibur", "Ropener"])
        golden = json.loads(open(os.path.join(HERE, "golden_legacy_plans.json")).read())
        self.assertEqual(sorted(golden), ["Generic board", "Glasscalibur", "Ropener"])


class ManifestChecks(Rig):
    def test_a_consistent_set_is_accepted(self):
        m, why = vf.check_variant_dir(self.vdir, self.vcfg)
        self.assertIsNotNone(m, why)

    def test_a_changed_region_file_is_refused(self):
        p = os.path.join(self.vdir, "firmware-s3-128.bin")
        b = bytearray(open(p, "rb").read()); b[5] ^= 1; open(p, "wb").write(b)
        m, why = vf.check_variant_dir(self.vdir, self.vcfg)
        self.assertIsNone(m)
        self.assertIn("does not match the manifest", why)

    def test_a_region_inside_nvs_is_refused_even_if_hashes_match(self):
        mp = os.path.join(self.vdir, "flash-manifest-s3-128.json")
        m = json.load(open(mp))
        for r in m["regions"]:
            if r["name"] == "boot_app0":
                r["offset"] = "0xe000"   # where the platform's uploader puts it
        json.dump(m, open(mp, "w"))
        m2, why = vf.check_variant_dir(self.vdir, self.vcfg)
        self.assertIsNone(m2)
        self.assertIn("overlaps preserved nvs", why)


class FlashMode(Rig):
    def flash(self, factory_reset=False):
        return vf.flash_variant("COMX", "Blipscope", VNAME, self.vcfg, factory_reset, self.msgs.append)

    def test_flash_mode_never_touches_nvs(self):
        ok, msg = self.flash()
        self.assertTrue(ok, msg)
        w = self.writes()
        self.assertEqual(sorted(x[0] for x in w), [0x0, 0x8000, 0x1E000, 0x20000])
        self.assertEqual(touches_nvs(w), [], "flash mode wrote inside the NVS span")

    def test_control_factory_reset_does_write_over_nvs(self):
        """The assertion above must be able to fail: the factory image does cover NVS."""
        ok, msg = self.flash(factory_reset=True)
        self.assertTrue(ok, msg)
        self.assertEqual([x[0] for x in self.writes()], [0x0])
        self.assertNotEqual(touches_nvs(self.writes()), [])

    def test_wrong_chip_is_refused_before_any_write(self):
        os.environ["FAKE_CHIP"] = "ESP32-C6"
        ok, msg = self.flash()
        self.assertFalse(ok)
        self.assertIn("needs an ESP32-S3", msg)
        self.assertEqual(self.writes(), [])

    def test_a_baked_in_key_is_refused_and_nothing_is_written(self):
        """A self-consistent image set (manifest matches every file) that carries
        a key -- what a local build with -DCLOUD_FEED_KEY looks like."""
        shutil.rmtree(self.vdir)
        make_variant(self.vdir, app_extra=b"\0" + secrets.token_hex(32).encode())
        with open(os.path.join(self.vdir, vf.RELEASE_RECORD), "w") as f:   # a tagged release,
            json.dump({"tag": "v99", "prerelease": False}, f)                # so the SCAN refuses
        ok, msg = self.flash()
        self.assertFalse(ok)
        self.assertIn("REFUSED", msg)
        self.assertIn("64-hex", msg)
        self.assertEqual(self.writes(), [], "a refused scan must write nothing")

    def test_local_build_is_labelled(self):
        local = os.path.join(self.td, "local-build")
        make_variant(local)
        cfg = dict(self.vcfg, local_build=local)
        ok, msg = vf.flash_variant("COMX", "Blipscope", VNAME, cfg, False, self.msgs.append)
        self.assertTrue(ok, msg)
        self.assertTrue(any("LOCAL BUILD" in m and "CLOUD_FEED_KEY" in m for m in self.msgs))


class ChipProductScan(Rig):
    def test_known_framework_runs_pass_and_an_unknown_key_is_refused(self):
        known = json.load(open(vf.KNOWN_PUBLIC_FILE))["public_runs"]
        self.assertEqual(len(known), 19)
        img = os.path.join(self.td, "c6.factory.bin")
        open(img, "wb").write(b"\xe9\0esp_image\0" + b"\0".join(b"x" * 8 for _ in range(3)) + b"\0")
        ok, why, _ = vf.scan_gate(img, "esp_image", vf.public_lists("Ropener"))
        self.assertTrue(ok, why)
        open(img, "ab").write(secrets.token_hex(32).encode() + b"\0")
        ok, why, _ = vf.scan_gate(img, "esp_image", vf.public_lists("Ropener"))
        self.assertFalse(ok)
        self.assertIn("HIT", why)


class Bench(Rig):
    def setUp(self):
        super().setUp()
        self.csv = os.path.join(self.td, "provisioned.csv")
        self.pcfg = {"select": "variant", "variants": {VNAME: self.vcfg},
                     "provisioner": {
                                     "command": os.path.join(HERE, "fake_provisioner.py"),
                                     "log": self.csv}}

    def session(self, count=0):
        return vf.BenchSession("Blipscope", VNAME, self.vcfg, self.pcfg, count=count)

    def test_done_means_verified_and_recorded(self):
        s = self.session()
        self.assertEqual(vf.bench_board("COMX", s), "DONE")
        t = s.tiles["00:00:00:00:00:01"]
        self.assertEqual(t["state"], "DONE")
        self.assertIn("00:00:00:00:00:01", open(self.csv).read())
        self.assertEqual([x[0] for x in self.writes()], [0x0], "a factory board is written whole")

    def test_provisioner_exit_nonzero_makes_the_tile_red(self):
        os.environ["FAKE_PROV_EXIT"] = "1"
        s = self.session()
        self.assertEqual(vf.bench_board("COMX", s), "FAILED")
        t = s.tiles["00:00:00:00:00:01"]
        self.assertEqual(t["state"], "FAILED")
        self.assertEqual(vf.TILE_COLOR[t["state"]], "#e5534b")
        self.assertIn("PROVISION_TOKEN rejected (403)", t["detail"])
        self.assertEqual(s.done, 0)
        self.assertEqual(len(s.failures), 1)

    def test_exit_zero_without_result_ok_is_not_done(self):
        os.environ["FAKE_PROV_LIE"] = "1"
        s = self.session()
        self.assertEqual(vf.bench_board("COMX", s), "FAILED")

    def test_a_mac_already_in_the_csv_is_skipped_untouched(self):
        with open(self.csv, "w") as f:
            f.write("utc,env,mac,device_id,source\nx,test,00:00:00:00:00:01,fakeid,provisioned\n")
        s = self.session()
        self.assertEqual(vf.bench_board("COMX", s), "SKIPPED")
        self.assertEqual(self.writes(), [])

    def test_bench_refuses_in_one_sentence_without_the_token_file(self):
        self.assertIsNone(vf.bench_refusal(self.pcfg), "CONTROL: with the file, bench mode may start")
        os.remove(vf.PROVISION_TOKEN_FILE)
        why = vf.bench_refusal(self.pcfg)
        self.assertIsNotNone(why)
        self.assertEqual(why.count(". "), 0, "one sentence")
        self.assertIn("provisioning token", why)
        self.assertIn(vf.PROVISION_TOKEN_FILE, why)

    def test_an_empty_token_file_is_no_token(self):
        with open(vf.PROVISION_TOKEN_FILE, "w") as f:
            f.write("   \n")
        self.assertIsNotNone(vf.bench_refusal(self.pcfg))

    def test_the_provisioner_gets_the_token_file_path_never_its_contents(self):
        argv = vf.provisioner_argv(self.pcfg, "COMX", "00:00:00:00:00:01")
        i = argv.index("--token-file")
        self.assertEqual(argv[i + 1], vf.PROVISION_TOKEN_FILE)
        self.assertNotIn("test-provision-token", " ".join(argv))

    def test_the_flasher_never_reads_the_device_key_secret(self):
        src = open(os.path.join(ROOT, "valar_flasher.py"), encoding="utf-8").read()
        read = re.compile(r"(environ|getenv)[^\n]{0,40}DEVICE_KEY_SECRET")
        self.assertIsNone(read.search(src))
        self.assertIsNotNone(read.search('os.environ.get("DEVICE_KEY_SECRET")'), "CONTROL: the pattern finds a read")

    def test_bench_is_not_offered_without_a_local_command(self):
        self.assertFalse(vf.bench_available({"provisioner": {"auth": "provision-token"}}))
        shipped = json.load(open(os.path.join(ROOT, "products.json")))["products"]["Blipscope"]
        self.assertNotIn("command", shipped.get("provisioner", {}), "the shipped products.json names no command")

    def test_loop_stops_at_count(self):
        s = self.session(count=1)
        ports = iter([["COMX"], ["COMX"], ["COMX"]] + [["COMX"]] * 50)
        vf.bench_loop(s, threading.Event(), ports_fn=lambda: next(ports), poll=0.01)
        self.assertEqual(s.done, 1)
        self.assertEqual(s.progress(), "1 of 1 this session")


class PortAllowlist(Rig):
    """--ports: a bench run on a machine with other boards attached touches only
    the named port. Every Valar board is 303A, so VID cannot tell them apart."""

    def setUp(self):
        super().setUp()
        self.saved_ports = (vf._enumerate_ports, vf.PORT_ALLOW)
        vf._enumerate_ports = lambda: [("COM6", 0x303A), ("COM15", 0x303A), ("COM18", 0x303A)]

    def tearDown(self):
        vf._enumerate_ports, vf.PORT_ALLOW = self.saved_ports
        super().tearDown()

    def test_control_without_ports_every_esp_board_is_seen(self):
        vf.PORT_ALLOW = None
        self.assertEqual(vf.list_ports(), ["COM6", "COM15", "COM18"])

    def test_with_ports_only_the_named_board_is_seen(self):
        vf.PORT_ALLOW = {"COM18"}
        self.assertEqual(vf.list_ports(), ["COM18"])

    def test_a_bench_run_touches_only_the_named_port(self):
        vf.PORT_ALLOW = {"COM18"}
        csv_path = os.path.join(self.td, "provisioned.csv")
        pcfg = {"select": "variant", "variants": {VNAME: self.vcfg},
                "provisioner": {
                                "command": os.path.join(HERE, "fake_provisioner.py"), "log": csv_path}}
        s = vf.BenchSession("Blipscope", VNAME, self.vcfg, pcfg, count=1)
        vf.bench_loop(s, threading.Event(), ports_fn=vf.list_ports, poll=0.01)
        self.assertEqual(s.done, 1)
        self.assertEqual(sorted({c["port"] for c in self.calls()}), ["COM18"])


COM6_MAC, COM15_SERIAL, COM18_MAC = "90:70:69:32:6e:64", "90706931E9D8", "90:70:69:31:e2:08"


class Protected(Rig):
    """A protected board is refused before any write -- and, when the OS already
    knows its MAC, before esptool even resets it."""

    def setUp(self):
        super().setUp()
        with open(vf.PROTECTED_FILE, "w") as f:
            json.dump({"macs": {COM6_MAC: "COM6 bench unit", "90:70:69:31:e9:d8": "COM15 Missileer"}}, f)
        self.pcfg = {"select": "variant", "variants": {VNAME: self.vcfg},
                     "provisioner": {
                                     "command": os.path.join(HERE, "fake_provisioner.py"),
                                     "log": os.path.join(self.td, "provisioned.csv")}}

    def run_board(self, port):
        s = vf.BenchSession("Blipscope", VNAME, self.vcfg, self.pcfg)
        return vf.bench_board(port, s), s

    def test_protected_by_usb_serial_is_red_and_untouched(self):
        vf._port_serials = lambda: {"COM6": "90:70:69:32:6E:64"}
        result, s = self.run_board("COM6")
        self.assertEqual(result, "FAILED")
        t = s.tiles["COM6"]
        self.assertEqual(vf.TILE_COLOR[t["state"]], "#e5534b")
        self.assertIn("PROTECTED", t["detail"])
        self.assertEqual(self.calls(), [], "a protected board must not even be reset")

    def test_tinyusb_serial_form_matches(self):
        vf._port_serials = lambda: {"COM15": COM15_SERIAL}
        result, s = self.run_board("COM15")
        self.assertEqual(result, "FAILED")
        self.assertEqual(self.calls(), [])

    def test_protected_seen_only_by_esptool_is_red_before_any_write(self):
        os.environ["FAKE_MAC"] = COM6_MAC          # the OS reports no serial for it
        result, s = self.run_board("COMX")
        self.assertEqual(result, "FAILED")
        self.assertIn("PROTECTED", s.tiles[COM6_MAC]["detail"])
        self.assertEqual([c["sub"] for c in self.calls()], ["read_mac"], "nothing past the MAC read")

    def test_control_an_unprotected_board_is_done(self):
        vf._port_serials = lambda: {"COM18": "90:70:69:31:E2:08"}
        os.environ["FAKE_MAC"] = COM18_MAC
        result, s = self.run_board("COM18")
        self.assertEqual(result, "DONE")

    def test_flash_mode_refuses_a_protected_board_untouched(self):
        vf._port_serials = lambda: {"COM6": COM6_MAC}
        ok, msg = vf.flash_variant("COM6", "Blipscope", VNAME, self.vcfg, False, self.msgs.append)
        self.assertFalse(ok)
        self.assertIn("PROTECTED", msg)
        self.assertEqual(self.calls(), [])

    def test_an_unreadable_list_refuses_bench_mode(self):
        with open(vf.PROTECTED_FILE, "w") as f:
            f.write("{ not json")
        self.assertIn("protected-board list", vf.bench_refusal(self.pcfg))


class Confirmation(Protected):
    """No --ports: bench mode lists every board it will flash and needs 'yes'."""

    def setUp(self):
        super().setUp()
        self.saved_c = (vf._enumerate_ports, vf.sync_variant)
        vf._enumerate_ports = lambda: [("COM6", 0x303A), ("COM15", 0x303A), ("COM18", 0x303A)]
        vf._port_serials = lambda: {"COM6": COM6_MAC, "COM15": COM15_SERIAL, "COM18": COM18_MAC}
        vf.sync_variant = lambda *a, **k: None
        os.environ["FAKE_MAC"] = COM18_MAC
        self.cfg = {"products": {"Blipscope": self.pcfg}}

    def tearDown(self):
        vf._enumerate_ports, vf.sync_variant = self.saved_c
        super().tearDown()

    def console(self, answer, count=1):
        import builtins
        import contextlib
        import io
        saved = builtins.input
        builtins.input = lambda prompt="": answer
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = vf.run_console(self.cfg, "Blipscope", VNAME, bench=True, count=count)
        finally:
            builtins.input = saved
        return rc, out.getvalue()

    def test_the_list_names_every_board_and_excludes_the_protected(self):
        targets, lines = vf.bench_targets()
        self.assertEqual(targets, {"COM18": COM18_MAC})
        self.assertEqual(sum("PROTECTED" in l for l in lines), 2)
        self.assertEqual(sum("WILL BE FLASHED" in l for l in lines), 1)

    def test_anything_but_yes_flashes_nothing(self):
        rc, out = self.console("y")
        self.assertEqual(rc, 2)
        self.assertIn("Nothing done", out)
        self.assertEqual(self.calls(), [])

    def test_yes_flashes_only_the_listed_board(self):
        rc, out = self.console("yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted({c["port"] for c in self.calls()}), ["COM18"])
        self.assertNotIn("FAILED", out)

    def test_a_board_attached_after_confirmation_is_refused_untouched(self):
        s = vf.BenchSession("Blipscope", VNAME, self.vcfg, self.pcfg)
        s.allowed_macs = {COM18_MAC}
        vf._port_serials = lambda: {"COM20": "90:70:69:00:00:99"}
        self.assertEqual(vf.bench_board("COM20", s), "FAILED")
        self.assertIn("not in the list confirmed", s.tiles["COM20"]["detail"])
        self.assertEqual(self.calls(), [])


class ReleaseChecks(Rig):
    """Manifest resolution: the app region must BE the release's firmware-<slug>.bin,
    a prerelease is refused without --allow-prerelease, and bench mode writes only
    a tagged release -- checked at download AND at use."""

    def setUp(self):
        super().setUp()
        self.csv = os.path.join(self.td, "provisioned.csv")
        self.pcfg = {"select": "variant", "variants": {VNAME: self.vcfg},
                     "provisioner": {"command": os.path.join(HERE, "fake_provisioner.py"),
                                     "log": self.csv}}
        self.saved_get_release = vf.get_release
        self.rel = os.path.join(self.td, "release")   # the release's assets, served as file://
        make_variant(self.rel)
        self.prerelease = False

        def fake_get_release(repo, tag, log):
            assets = [{"name": n, "browser_download_url": Path(os.path.join(self.rel, n)).as_uri()}
                      for n in os.listdir(self.rel)]
            return "v99", assets, self.prerelease
        vf.get_release = fake_get_release
        shutil.rmtree(self.vdir)          # nothing cached: every test starts from a sync

    def tearDown(self):
        vf.get_release = self.saved_get_release
        super().tearDown()

    def session(self, count=0):
        return vf.BenchSession("Blipscope", VNAME, self.vcfg, self.pcfg, count=count)

    def sync(self):
        return vf.sync_variant("Blipscope", VNAME, self.vcfg, self.msgs.append, force=True)

    def rename_app(self, new):
        """Make the release's manifest name a different, self-consistent app file."""
        mp = os.path.join(self.rel, "flash-manifest-s3-128.json")
        m = json.load(open(mp))
        app = next(r for r in m["regions"] if r["name"] == "app")
        shutil.copy(os.path.join(self.rel, app["file"]), os.path.join(self.rel, new))
        app["file"] = new
        json.dump(m, open(mp, "w"))

    def test_CONTROL_a_tagged_release_syncs_and_the_bench_writes_it(self):
        self.assertEqual(self.sync(), "v99", self.msgs)
        rec = vf.read_release_record(self.vdir)
        self.assertEqual((rec["tag"], rec["prerelease"]), ("v99", False))
        self.assertEqual(vf.bench_board("COMX", self.session()), "DONE")

    def windows_locks(self, files_locked_for=0, locked_file=None):
        """What OneDrive (or a virus scanner) does on Windows: deleting or renaming the
        variant FOLDER is refused with Access is denied, and a freshly written file
        can be held for a moment -- `files_locked_for` refusals per file, or for good
        for `locked_file`. The 2026-10-20 bench report: '[WinError 5] Access is
        denied: ...firmware\\Blipscope\\Kit_S3_1.28in_s3-128'."""
        from unittest import mock
        vdir = os.path.normcase(os.path.abspath(self.vdir))
        denied = lambda p: os.path.normcase(os.path.abspath(str(p))) == vdir

        def refuse(real):
            def f(*a, **k):
                if any(denied(x) for x in a[:2]):
                    raise PermissionError(13, "Access is denied", self.vdir)
                return real(*a, **k)
            return f
        tries = {}
        real_copyfile = shutil.copyfile

        def copyfile(src, dst, *a, **k):
            name = os.path.basename(dst)
            tries[name] = tries.get(name, 0) + 1
            if name == locked_file or tries[name] <= files_locked_for:
                raise PermissionError(13, "Access is denied", dst)
            return real_copyfile(src, dst, *a, **k)
        vf.RETRY_WAIT = 0
        self.addCleanup(setattr, vf, "RETRY_WAIT", 0.5)
        return [mock.patch.object(vf.shutil, "rmtree", refuse(shutil.rmtree)),
                mock.patch.object(vf.shutil, "move", refuse(shutil.move)),
                mock.patch.object(vf.os, "rename", refuse(os.rename)),
                mock.patch.object(vf.shutil, "copyfile", copyfile)]

    def with_locks(self, patches, fn):
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return fn()

    def test_a_folder_windows_wont_delete_or_rename_still_updates(self):
        make_variant(self.vdir, app_extra=b"an older release")          # what's there now: v98
        with open(os.path.join(self.vdir, vf.RELEASE_RECORD), "w") as f:
            json.dump({"tag": "v98", "prerelease": False}, f)
        open(os.path.join(self.vdir, "factory-scan-old.json"), "w").write('{"public_runs": ["x"]}')
        self.assertEqual(self.with_locks(self.windows_locks(), self.sync), "v99", self.msgs)
        self.assertEqual(vf.read_release_record(self.vdir)["tag"], "v99")
        self.assertEqual(open(os.path.join(self.vdir, ".release_tag")).read(), "v99", "the window shows this")
        self.assertIsNotNone(vf.check_variant_dir(self.vdir, self.vcfg)[0])
        self.assertFalse(os.path.exists(os.path.join(self.vdir, "factory-scan-old.json")),
                         "nothing from an older release stays beside the new one")
        self.assertEqual(vf.bench_board("COMX", self.session()), "DONE")

    def test_a_first_download_into_a_watched_folder_installs(self):
        self.assertEqual(self.with_locks(self.windows_locks(files_locked_for=2), self.sync), "v99", self.msgs)
        self.assertIsNone(vf.release_refusal(self.vdir, False, bench=True))

    def test_a_file_locked_for_good_installs_nothing_a_board_can_get(self):
        patches = self.windows_locks(locked_file="firmware-s3-128.bin")
        self.assertIsNone(self.with_locks(patches, self.sync))
        self.assertTrue(any("download refused" in m for m in self.msgs), self.msgs)
        self.assertIsNotNone(vf.release_refusal(self.vdir, False, bench=False))
        self.assertEqual(vf.bench_board("COMX", self.session()), "FAILED")
        self.assertEqual(self.writes(), [])

    def test_nothing_downloaded_says_so_instead_of_blaming_an_old_cache(self):
        why = vf.release_refusal(self.vdir, False, bench=False)
        self.assertIn("no firmware downloaded", why)
        self.assertNotIn("synced before release checks", why)

    def test_app_region_not_named_firmware_slug_is_refused(self):
        self.rename_app("app-s3-128.bin")
        # the release's OTA binary is now something else; the manifest set stays self-consistent
        with open(os.path.join(self.rel, "firmware-s3-128.bin"), "ab") as f:
            f.write(b"a different build")
        self.assertIsNone(self.sync())
        self.assertFalse(os.path.isdir(self.vdir), "nothing may be cached")
        self.assertTrue(any("app region is not firmware-s3-128.bin" in m for m in self.msgs), self.msgs)

    def test_app_hash_differing_from_the_releases_firmware_bin_is_refused(self):
        with open(os.path.join(self.rel, "firmware-s3-128.bin"), "ab") as f:
            f.write(b"a different build")
        self.assertIsNone(self.sync())
        self.assertFalse(os.path.isdir(self.vdir))

    def test_a_prerelease_is_refused_without_the_flag(self):
        self.prerelease = True
        self.assertIsNone(self.sync())
        self.assertFalse(os.path.isdir(self.vdir))
        self.assertTrue(any("PRERELEASE" in m for m in self.msgs), self.msgs)

    def test_a_prerelease_is_accepted_with_the_flag_and_recorded_as_one(self):
        self.prerelease = True
        vf.ALLOW_PRERELEASE = True
        self.assertEqual(self.sync(), "v99", self.msgs)
        self.assertTrue(vf.read_release_record(self.vdir)["prerelease"])

    def _cache_prerelease(self):
        self.prerelease = True
        vf.ALLOW_PRERELEASE = True
        self.assertEqual(self.sync(), "v99")
        vf.ALLOW_PRERELEASE = False

    def test_bench_refuses_a_cached_prerelease_without_the_flag(self):
        self._cache_prerelease()
        s = self.session()
        self.assertEqual(vf.bench_board("COMX", s), "FAILED")
        self.assertIn("PRERELEASE", s.tiles["00:00:00:00:00:01"]["detail"])
        self.assertEqual(self.writes(), [], "nothing may be written")
        vf.ALLOW_PRERELEASE = True
        self.assertEqual(vf.bench_board("COMY", self.session()), "DONE", "CONTROL: the flag lets it through")

    def test_bench_refuses_a_cache_with_no_release_record(self):
        make_variant(self.vdir)            # an image set synced before the release checks existed
        s = self.session()
        self.assertEqual(vf.bench_board("COMX", s), "FAILED")
        self.assertIn("no release record", s.tiles["00:00:00:00:00:01"]["detail"])
        self.assertEqual(self.writes(), [])

    def test_bench_refuses_a_local_build_even_with_the_flag(self):
        make_variant(os.path.join(self.td, "local"))
        vf.ALLOW_PRERELEASE = True
        self.vcfg = dict(self.vcfg, local_build=os.path.join(self.td, "local"))
        s = vf.BenchSession("Blipscope", VNAME, self.vcfg, self.pcfg)
        self.assertEqual(vf.bench_board("COMX", s), "FAILED")
        self.assertIn("never a local build", s.tiles["00:00:00:00:00:01"]["detail"])
        self.assertEqual(self.writes(), [])


def esp_image(project, app_off=0x10000):
    """A minimal ESP-IDF factory image: partition table at 0x8000 (nvs, app0, app1)
    and an app whose descriptor names `project`. Carries the scan's anchor."""
    def entry(kind, sub, off, size, name):
        return (b"\xaa\x50" + bytes([kind, sub]) + off.to_bytes(4, "little") + size.to_bytes(4, "little")
                + name.ljust(16, b"\0") + b"\0" * 4)
    img = bytearray(b"\xff" * (app_off + 0x200))
    img[0:12] = b"\xe9\0esp_image\0"
    table = (entry(1, 2, 0x9000, 0x5000, b"nvs") + entry(0, 0x10, app_off, 0x1C0000, b"app0")
             + entry(0, 0x11, app_off + 0x1C0000, 0x1C0000, b"app1"))
    img[0x8000:0x8000 + len(table)] = table
    head = bytearray(0x100)
    head[0] = 0xE9
    head[32:36] = vf.APP_DESC_MAGIC.to_bytes(4, "little")
    head[48:56] = b"2026.6.5"
    head[80:80 + len(project)] = project
    img[app_off:app_off + 0x100] = head
    return bytes(img)


class FeatureRig(Rig):
    """Rig, plus this machine's settings and flash record kept out of the way."""

    def setUp(self):
        super().setUp()
        self.fsaved = (vf.SETTINGS_FILE, vf.FLASH_LOG, vf.play_sound, vf.list_ports, vf.get_latest)
        vf.SETTINGS_FILE = os.path.join(self.td, "settings.json")
        vf.FLASH_LOG = os.path.join(self.td, "flash-log.csv")
        self.sounds = []
        vf.play_sound = lambda ok, fallback=None: self.sounds.append(ok)
        vf.get_latest = lambda repo, log: None            # offline: flash what is cached
        os.environ.pop("FAKE_FLASH", None)
        self.products = {
            "Ropener": {"repo": "x/ropener", "assets": {"esp32c3": "val3000", "esp32c6": "val3100"}},
            "Glasscalibur": {"repo": "x/glass", "assets": {"esp32c6": "glasscalibur"}},
        }
        for product, chip, name in (("Ropener", "esp32c6", b"ropener"), ("Ropener", "esp32c3", b"ropener"),
                                    ("Glasscalibur", "esp32c6", b"glasscalibur")):
            os.makedirs(vf.fw_dir(product), exist_ok=True)
            with open(vf.bin_path(product, chip), "wb") as f:
                f.write(esp_image(name))

    def tearDown(self):
        (vf.SETTINGS_FILE, vf.FLASH_LOG, vf.play_sound, vf.list_ports, vf.get_latest) = self.fsaved
        os.environ.pop("FAKE_FLASH", None)
        super().tearDown()

    def board(self, project=None, app_off=0x10000):
        """The next board's flash: `project`'s firmware, or blank."""
        if project is None:
            os.environ.pop("FAKE_FLASH", None)
            return
        path = os.path.join(self.td, "board.bin")
        with open(path, "wb") as f:
            f.write(esp_image(project, app_off))
        os.environ["FAKE_FLASH"] = path

    def rows(self):
        if not os.path.exists(vf.FLASH_LOG):
            return []
        with open(vf.FLASH_LOG, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))


class BoardFirmware(FeatureRig):
    def test_the_project_name_is_read_from_a_factory_image(self):
        self.assertEqual(vf.image_project(vf.bin_path("Glasscalibur", "esp32c6")), "glasscalibur")
        other = os.path.join(self.td, "far.bin")
        open(other, "wb").write(esp_image(b"val3100", app_off=0x20000))
        self.assertEqual(vf.image_project(other), "val3100")
        open(other, "wb").write(b"\xff" * 0x10000)
        self.assertIsNone(vf.image_project(other), "a blank image names nothing")

    def test_one_esptool_session_reads_chip_name_and_mac_and_writes_nothing(self):
        os.environ.update(FAKE_CHIP="ESP32-C6", FAKE_MAC="40:4c:ca:57:2a:08")
        self.board(b"glasscalibur")
        chip, found, mac, _ = vf.inspect_board("COM9")
        self.assertEqual((chip, found, mac), ("esp32c6", "glasscalibur", "40:4c:ca:57:2a:08"))
        self.assertEqual([c["sub"] for c in self.calls()], ["read_flash"])
        self.assertEqual(self.writes(), [])

    def test_an_app_past_the_first_read_costs_one_more_read(self):
        os.environ["FAKE_CHIP"] = "ESP32-C6"
        self.board(b"val3100", app_off=0x20000)
        self.assertEqual(vf.inspect_board("COM9")[1], "val3100")
        self.assertEqual([c["sub"] for c in self.calls()], ["read_flash", "read_flash"])

    def test_a_blank_board_has_a_chip_but_no_name(self):
        os.environ["FAKE_CHIP"] = "ESP32-C6"
        chip, found, _, _ = vf.inspect_board("COM9")
        self.assertEqual((chip, found), ("esp32c6", None))

    def test_another_products_firmware_is_a_mismatch_that_names_it(self):
        owners, line = vf.firmware_mismatch(self.products, "Ropener", "esp32c6", "glasscalibur")
        self.assertEqual(owners, ["Glasscalibur"])
        self.assertIn("Glasscalibur", line)
        self.assertIn("Ropener is selected", line)

    def test_own_unknown_or_no_firmware_is_not_a_mismatch(self):
        for found in ("ropener", "arduino-lib-builder", None):
            self.assertIsNone(vf.firmware_mismatch(self.products, "Ropener", "esp32c6", found), found)

    def test_the_base_mac_is_taken_over_a_longer_eui64(self):
        out = "MAC: 40:4c:ca:ff:fe:57:2a:08\nBASE MAC: 40:4c:ca:57:2a:08\nMAC_EXT: ff:fe\n"
        self.assertEqual(vf._mac_in(out), "40:4c:ca:57:2a:08")
        self.assertEqual(vf._mac_in("MAC: 90:70:69:32:6E:64"), "90:70:69:32:6e:64")


class ConsoleChipFlash(FeatureRig):
    """The console flash loop end to end: one board on COM9, then Ctrl-C."""

    def run_once(self, product, answer=None):
        seen = []

        def ports():
            if seen:
                raise KeyboardInterrupt
            seen.append(1)
            return ["COM9"]
        vf.list_ports = ports
        os.environ.setdefault("FAKE_CHIP", "ESP32-C6")
        asked = []

        def fake_input(prompt=""):
            asked.append(prompt)
            if answer is None:
                raise AssertionError(f"asked when it should not have: {prompt}")
            return answer
        from unittest import mock
        with mock.patch("builtins.input", fake_input):
            vf.run_console({"products": self.products}, product)
        return asked

    def test_a_board_running_another_products_firmware_is_not_flashed_unless_confirmed(self):
        self.board(b"glasscalibur")
        asked = self.run_once("Ropener", answer="no")
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.writes(), [])
        row, = self.rows()
        self.assertEqual((row["result"], row["was"], row["product"]), ("SKIPPED", "glasscalibur", "Ropener"))
        self.assertEqual(self.sounds, [], "an operator's 'no' is not a failure")

    def test_CONTROL_yes_flashes_it(self):
        self.board(b"glasscalibur")
        self.run_once("Ropener", answer="yes")
        self.assertEqual([w[2] for w in self.writes()], ["esp32c6.factory.bin"])
        self.assertEqual(self.rows()[0]["result"], "DONE")
        self.assertEqual(self.sounds, [True])

    def test_a_blank_board_and_a_same_product_board_flash_without_asking(self):
        for project in (None, b"ropener"):
            self.board(project)
            self.run_once("Ropener")
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual([r["result"] for r in self.rows()], ["DONE", "DONE"])
        self.assertEqual([r["was"] for r in self.rows()], ["", "ropener"])

    def test_the_record_says_what_was_flashed_onto_which_board(self):
        os.environ["FAKE_MAC"] = "40:4c:ca:57:2a:08"
        with open(os.path.join(vf.fw_dir("Glasscalibur"), ".release_tag"), "w") as f:
            f.write("v2.1.0")
        self.run_once("Glasscalibur")
        row, = self.rows()
        self.assertEqual({k: row[k] for k in ("product", "version", "chip", "port", "mac", "result")},
                         {"product": "Glasscalibur", "version": "v2.1.0", "chip": "ESP32-C6",
                          "port": "COM9", "mac": "40:4c:ca:57:2a:08", "result": "DONE"})
        self.assertRegex(row["time"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_a_failed_flash_is_recorded_and_sounds_different(self):
        os.environ["FAKE_FAIL"] = "write_flash"
        self.run_once("Ropener")
        self.assertEqual(self.rows()[0]["result"], "FAILED")
        self.assertEqual(self.sounds, [False])


class FlashRecord(FeatureRig):
    def test_rows_append_under_one_header(self):
        vf.record_flash(product="Ropener", result="DONE", detail="flashed\nin 12s")
        vf.record_flash(product="Glasscalibur", result="FAILED", detail="reseat — retry")
        raw = open(vf.FLASH_LOG, "rb").read()
        self.assertEqual(raw.count(b"\xef\xbb\xbf"), 1, "one BOM, at the start, for Excel")
        rows = self.rows()
        self.assertEqual([(r["product"], r["result"]) for r in rows], [("Ropener", "DONE"), ("Glasscalibur", "FAILED")])
        self.assertEqual(rows[0]["detail"], "flashed in 12s", "one line per board")
        self.assertEqual(rows[1]["detail"], "reseat — retry")

    def test_a_record_that_cannot_be_written_never_stops_the_flashing(self):
        vf.FLASH_LOG = self.td                    # a directory: opening it to append fails
        note = vf.record_flash(product="Ropener", result="DONE")
        self.assertIn("open in Excel", note)


class Remembered(FeatureRig):
    def test_the_last_firmware_and_variant_open_next_time(self):
        cfg = {"default": "Ropener", "products": dict(self.products, Blipscope={"variants": {"A": {}, "B": {}}})}
        self.assertEqual(vf.starting_product(cfg), "Ropener")
        vf.save_settings(product="Glasscalibur")
        vf.save_settings(variants={"Blipscope": "B"})
        self.assertEqual(vf.starting_product(cfg), "Glasscalibur")
        self.assertEqual(vf.starting_variant("Blipscope", cfg["products"]["Blipscope"]), "B")
        self.assertEqual(vf.load_settings()["product"], "Glasscalibur", "saving one key keeps the others")

    def test_a_product_that_is_gone_or_an_unreadable_file_means_the_default(self):
        cfg = {"default": "Ropener", "products": self.products}
        vf.save_settings(product="Discontinued")
        self.assertEqual(vf.starting_product(cfg), "Ropener")
        open(vf.SETTINGS_FILE, "w").write("{not json")
        self.assertEqual(vf.load_settings(), {})
        self.assertEqual(vf.starting_product(cfg), "Ropener")


class AppUpdate(unittest.TestCase):
    """The flasher updating itself from a fake GitHub: a tree, and the blobs it names."""

    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.app = os.path.join(self.td, "app")
        os.makedirs(self.app)
        self.saved = (vf.HERE, vf.UPDATE_BACKUP, vf._gh_json)
        vf.HERE, vf.UPDATE_BACKUP = self.app, os.path.join(self.app, ".update-backup")
        self.remote = {}                          # path -> (bytes, mode)
        self.corrupt = set()

        def gh_json(url, timeout=30):
            if "/git/trees/" in url:
                return {"truncated": False, "tree": [
                    {"path": p, "type": "blob", "mode": m, "sha": vf._blob_sha(d)} for p, (d, m) in self.remote.items()]}
            sha = url.rsplit("/", 1)[-1]
            data = next(d for d, m in self.remote.values() if vf._blob_sha(d) == sha)
            if sha in self.corrupt:
                data += b"tampered"
            return {"content": __import__("base64").b64encode(data).decode()}
        vf._gh_json = gh_json

    def tearDown(self):
        vf.HERE, vf.UPDATE_BACKUP, vf._gh_json = self.saved
        shutil.rmtree(self.td, ignore_errors=True)

    def local(self, path, data):
        full = os.path.join(self.app, *path.split("/"))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "wb").write(data)

    def read(self, path):
        return open(os.path.join(self.app, *path.split("/")), "rb").read()

    def test_an_up_to_date_folder_has_nothing_to_update_even_with_windows_line_endings(self):
        self.remote = {"valar_flasher.py": (b"print('v2')\n", "100644"),
                       "Valar Flasher (Windows).bat": (b"@echo off\nstart\n", "100644")}
        self.local("valar_flasher.py", b"print('v2')\n")
        self.local("Valar Flasher (Windows).bat", b"@echo off\r\nstart\r\n")
        self.assertEqual(vf.app_update_check(), [])

    def test_changed_and_new_files_are_found_and_development_files_ignored(self):
        self.remote = {"valar_flasher.py": (b"print('v2')\n", "100644"),
                       "products.json": (b"{}\n", "100644"),
                       "tools/new.py": (b"x = 1\n", "100644"),
                       "tests/test_flasher.py": (b"changed\n", "100644"),
                       ".github/workflows/test.yml": (b"changed\n", "100644")}
        self.local("valar_flasher.py", b"print('v1')\n")
        self.local("products.json", b"{}\n")
        self.assertEqual(sorted(e["path"] for e in vf.app_update_check()), ["tools/new.py", "valar_flasher.py"])

    def test_an_update_replaces_the_files_and_keeps_the_old_ones(self):
        self.remote = {"valar_flasher.py": (b"print('v2')\n", "100644"),
                       "Valar Flasher (Mac).command": (b"#!/bin/bash\necho v2\n", "100755"),
                       "Valar Flasher (Windows).bat": (b"@echo off\nv2\n", "100644")}
        self.local("valar_flasher.py", b"print('v1')\n")
        self.local("products.local.json", b"{\"mine\": 1}\n")
        ok, msg = vf.app_update_apply(vf.app_update_check())
        self.assertTrue(ok, msg)
        self.assertEqual(self.read("valar_flasher.py"), b"print('v2')\n")
        self.assertEqual(self.read("Valar Flasher (Windows).bat"), b"@echo off\r\nv2\r\n")
        self.assertEqual(self.read("products.local.json"), b"{\"mine\": 1}\n", "local files are not the repo's")
        if os.name == "posix":
            self.assertTrue(os.access(os.path.join(self.app, "Valar Flasher (Mac).command"), os.X_OK))
        backup, = os.listdir(vf.UPDATE_BACKUP)
        self.assertEqual(open(os.path.join(vf.UPDATE_BACKUP, backup, "valar_flasher.py"), "rb").read(),
                         b"print('v1')\n")
        self.assertEqual(vf.app_update_check(), [], "and afterwards it is up to date")

    def test_a_download_that_fails_its_hash_changes_nothing(self):
        self.remote = {"valar_flasher.py": (b"print('v2')\n", "100644"), "README.md": (b"new\n", "100644")}
        self.local("valar_flasher.py", b"print('v1')\n")
        self.corrupt = {vf._blob_sha(b"new\n")}
        ok, msg = vf.app_update_apply(vf.app_update_check())
        self.assertFalse(ok)
        self.assertIn("Nothing was changed", msg)
        self.assertEqual(self.read("valar_flasher.py"), b"print('v1')\n")
        self.assertFalse(os.path.exists(os.path.join(self.app, "README.md")))

    def test_a_write_that_fails_halfway_puts_the_old_files_back(self):
        self.remote = {"a.py": (b"a2\n", "100644"), "b.py": (b"b2\n", "100644")}
        self.local("a.py", b"a1\n")
        self.local("b.py", b"b1\n")
        real_replace, calls = os.replace, []

        def flaky_replace(src, dst):
            calls.append(dst)
            if len(calls) == 2:
                raise PermissionError(13, "Permission denied", dst)
            real_replace(src, dst)
        from unittest import mock
        with mock.patch.object(vf.os, "replace", flaky_replace):
            ok, msg = vf.app_update_apply(vf.app_update_check())
        self.assertFalse(ok)
        self.assertIn("put back", msg)
        self.assertEqual((self.read("a.py"), self.read("b.py")), (b"a1\n", b"b1\n"))
        self.assertEqual(sorted(n for n in os.listdir(self.app) if n.endswith(".new")), [])

    def test_a_git_checkout_is_left_to_git(self):
        os.makedirs(os.path.join(self.app, ".git"))
        self.remote = {"valar_flasher.py": (b"print('v2')\n", "100644")}
        self.assertIsNone(vf.app_update_check())


class Sounds(unittest.TestCase):
    def test_done_and_failed_play_different_mac_system_sounds(self):
        from unittest import mock
        started = []
        with mock.patch.object(vf.sys, "platform", "darwin"), \
                mock.patch.object(vf.shutil, "which", lambda c: "/usr/bin/" + c), \
                mock.patch.object(vf.os.path, "exists", lambda p: True), \
                mock.patch.object(vf.subprocess, "Popen", lambda cmd, **k: started.append(cmd)):
            vf.play_sound(True)
            vf.play_sound(False)
        self.assertEqual([c[0] for c in started], ["afplay", "afplay"])
        self.assertNotEqual(started[0][1], started[1][1])

    def test_no_player_falls_back_and_never_raises(self):
        from unittest import mock
        fell_back = []
        with mock.patch.object(vf.sys, "platform", "linux"), mock.patch.object(vf.shutil, "which", lambda c: None):
            vf.play_sound(True, fallback=lambda: fell_back.append(1))
        self.assertEqual(fell_back, [1])


class ConnectedBoards(Rig):
    """BoardList: which connected boards the window may touch."""
    BLIP1, BLIP2, GLASS = "COM3", "COM4", "COM7"
    DETAILS = {"COM3": ("Blipscope", "90:70:69:32:6E:64"), "COM4": ("Blipscope", "90:70:69:31:E2:08"),
               "COM7": ("USB JTAG/serial debug unit", "40:4C:CA:57:2A:08"), "COM9": ("CH340", None)}

    def setUp(self):
        super().setUp()
        self.b = vf.BoardList()

    def test_boards_already_connected_are_ignored_and_new_ones_flash(self):
        self.b.update([self.BLIP1, self.BLIP2], details=self.DETAILS)            # window opens
        self.assertEqual(self.b.flashable([self.BLIP1, self.BLIP2]), [])
        self.b.update([self.BLIP1, self.BLIP2, self.GLASS], details=self.DETAILS)
        self.assertEqual(self.b.flashable([self.BLIP1, self.BLIP2, self.GLASS]), [self.GLASS])

    def test_a_board_plugged_in_after_opening_is_flashable_even_if_it_reconnects(self):
        """The bench report, 2026-10-14: an ESP32 plugged in before auto-flash went on,
        which dropped off USB and came back, sat IGNORED while auto-flash was on."""
        d = {"COM18": ("USB Serial Device", "90:70:69:31:E2:08"), "COM6": ("USB Serial Device", None),
             "COM42": ("USB Serial Device", "A0:F2:62:9E:5C:A4")}
        self.b.update(["COM18", "COM6"], details=d)              # flasher opens
        self.b.update(["COM18", "COM6", "COM42"], details=d)     # ESP32 plugged in, auto-flash still off
        self.b.update(["COM18", "COM6"], details=d)              # it drops off USB...
        self.b.update(["COM18", "COM6", "COM42"], details=d)     # ...and comes back
        self.assertEqual(self.b.flashable(["COM18", "COM6", "COM42"]), ["COM42"])

    def test_an_ignored_board_stays_ignored_when_replugged_known_by_its_serial(self):
        self.b.update([self.BLIP1], details=self.DETAILS)
        self.b.update([], details=self.DETAILS)                                 # unplugged...
        self.b.update(["COM12"], details={"COM12": ("Blipscope", "90:70:69:32:6E:64")})   # ...back, another port
        self.assertEqual(self.b.flashable(["COM12"]), [])

    def test_a_board_switched_to_ignored_by_hand_stays_ignored_when_replugged(self):
        self.b.update([], details=self.DETAILS)
        self.b.update([self.GLASS], details=self.DETAILS)
        self.assertTrue(self.b.toggle(self.GLASS))
        self.b.update([], details=self.DETAILS)
        self.b.update([self.GLASS], details=self.DETAILS)
        self.assertEqual(self.b.flashable([self.GLASS]), [])
        self.assertFalse(self.b.toggle(self.GLASS), "CONTROL: flipping it back lets it be flashed")
        self.assertEqual(self.b.flashable([self.GLASS]), [self.GLASS])

    def test_a_board_with_no_serial_is_known_only_by_its_port(self):
        self.b.update(["COM9"], details=self.DETAILS)
        self.b.update([], details=self.DETAILS)
        self.b.update(["COM9"], details=self.DETAILS)
        self.assertEqual(self.b.flashable(["COM9"]), ["COM9"], "replugged = a new board")

    def test_a_protected_board_is_ignored_and_cannot_be_switched_on(self):
        json.dump({"macs": {"40:4c:ca:57:2a:08": "bench unit"}}, open(vf.PROTECTED_FILE, "w"))
        self.b.update([], details=self.DETAILS)
        self.b.update([self.GLASS], details=self.DETAILS)
        self.assertEqual(self.b.flashable([self.GLASS]), [])
        self.assertIsNone(self.b.toggle(self.GLASS))
        self.assertTrue(self.b.snapshot()[0][1]["protected"])

    def test_an_unlisted_port_is_not_flashable(self):
        self.assertEqual(self.b.flashable([self.GLASS]), [], "nothing is touched before the list has seen it")

    def test_bench_mode_excludes_boards_ignored_in_the_window(self):
        vf._port_serials = lambda: {p: s for p, (d, s) in self.DETAILS.items()}
        saved = vf.list_ports
        vf.list_ports = lambda: [self.BLIP1, self.BLIP2]
        try:
            targets, lines = vf.bench_targets(ignored={self.BLIP2})
        finally:
            vf.list_ports = saved
        self.assertEqual(list(targets), [self.BLIP1])
        self.assertTrue(any(self.BLIP2 in l and "ignored" in l for l in lines))


class Vendored(unittest.TestCase):
    def test_scan_image_matches_its_recorded_source(self):
        src = json.load(open(os.path.join(ROOT, "scan_image.SOURCE.json")))
        with open(os.path.join(ROOT, "scan_image.py"), "rb") as f:
            # LF-normalised: git stores LF, and a Windows checkout may hand us CRLF.
            got = hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
        self.assertEqual(got, src["sha256"], "scan_image.py was edited here -- change it upstream and re-vendor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
