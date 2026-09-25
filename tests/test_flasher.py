#!/usr/bin/env python3
"""valar-flasher tests: no board, no network. esptool is tests/fake_esptool.py,
the provisioner is tests/fake_provisioner.py.

    python -m unittest discover -s tests -v
"""
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
        self.msgs = []

    def tearDown(self):
        (vf.FW_ROOT, vf.ESPTOOL, vf.KNOWN_PUBLIC_FILE, vf.PROTECTED_FILE,
         vf._port_serials, vf.PORT_ALLOW, vf.PROVISION_TOKEN_FILE) = self.saved
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


class Vendored(unittest.TestCase):
    def test_scan_image_matches_its_recorded_source(self):
        src = json.load(open(os.path.join(ROOT, "scan_image.SOURCE.json")))
        with open(os.path.join(ROOT, "scan_image.py"), "rb") as f:
            # LF-normalised: git stores LF, and a Windows checkout may hand us CRLF.
            got = hashlib.sha256(f.read().replace(b"\r\n", b"\n")).hexdigest()
        self.assertEqual(got, src["sha256"], "scan_image.py was edited here -- change it upstream and re-vendor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
