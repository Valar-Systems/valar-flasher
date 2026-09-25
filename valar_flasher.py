#!/usr/bin/env python3
"""
Valar Flasher — plug in a Valar board, pick which firmware to install, and it
flashes the right image. No typing.

The product list lives in products.json next to this script — the SINGLE SOURCE
OF TRUTH. A product selects its image one of two ways:

  "select": "chip" (the default — Ropener, Glasscalibur, Generic board)
      {"repo": "owner/repo", "assets": {"<chip>": "<asset-name-substring>"}}
      detected chip -> the asset whose name matches assets[chip] -> write_flash 0x0
      e.g. assets {"esp32c6": "val3100"} matches Ropener-VAL3100.factory.bin.

  "select": "variant" (Blipscope)
      {"variants": {"<name>": {"chip", "repo", "asset", "manifest", "anchor"}},
       "provisioner": {"auth": "provision-token"}}
      Every Blipscope SKU is the same ESP32-S3, so chip detection CANNOT tell them
      apart and a wrong pick flashes the wrong board's image. The operator picks
      the variant, and the chosen variant is shown in large type throughout.
      The release's flash-manifest-<slug>.json lists every region (bootloader,
      partition table, boot_app0, app) with its offset; flash mode writes those
      and NEVER the NVS span the manifest marks "preserve", so a customer's Wi-Fi,
      location and device key survive. "Factory reset (erase settings)" writes the
      whole factory image instead.

BEFORE EVERY WRITE the image about to be flashed is scanned for secrets
(scan_image.py, vendored byte-for-byte from the Blipscope repo). A hit refuses
the write and says why. Why it matters: a factory image built on someone's own
machine can carry a -DCLOUD_FEED_KEY build flag into every board flashed from it;
CI never sets one, which is why the default image is the one CI published.

Bench mode (provisioning) appears only for a product whose provisioner is
configured in products.local.json (never in the shipped products.json), and it
refuses unless the bench's provisioning token is in its file
(~/.config/valar-flasher/provision-token). Keys are MINTED BY THE WORKER; nobody on
the bench holds DEVICE_KEY_SECRET and nothing here reads it. The token is never
typed into the window and never printed; the flasher checks only that the file
exists, and hands its PATH (not its contents) to the provisioner.

For each product it checks the repo's latest GitHub release on launch and caches
the download (offline? it uses whatever is cached). Runs a small Tk window; falls
back to a console loop if Tk is unavailable. Requires esptool (the launcher
installs it for you).
"""
import os, sys, json, subprocess, threading, queue, time, urllib.request
import csv, hashlib, re, shutil, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FW_ROOT = os.path.join(HERE, "firmware")
PRODUCTS_FILE = os.path.join(HERE, "products.json")
LOCAL_FILE = os.path.join(HERE, "products.local.json")
KNOWN_PUBLIC_FILE = os.path.join(HERE, "known_public_runs.json")
PY = sys.executable
FLASH_BAUD = "921600"
# The esptool command. Tests point this at tests/fake_esptool.py.
ESPTOOL = [PY, "-m", "esptool"]
DEFAULT_ANCHOR = "esp_image"   # present in every ESP-IDF image; the scan's first control
BENCH_MAX_PARALLEL = 8

sys.path.insert(0, HERE)
import scan_image  # noqa: E402  (vendored; see scan_image.SOURCE)

CHIP_LABEL = {"esp32c3": "ESP32-C3", "esp32c6": "ESP32-C6",
              "esp32c2": "ESP32-C2", "esp32s3": "ESP32-S3"}

# products.json (next to this script) is the SINGLE SOURCE OF TRUTH for the
# product list — add products THERE. The table below is only a minimal fallback
# so the tool still flashes Ropener boards if products.json is ever missing or
# corrupt; it is deliberately NOT a mirror of the full list.
FALLBACK_PRODUCTS = {
    "default": "Ropener",
    "products": {
        "Ropener": {"repo": "Valar-Systems/Ropener", "assets": {"esp32c3": "val3000", "esp32c6": "val3100"}},
    },
}


def _merge(base, over):
    """Deep-merge `over` into a copy of `base` (dicts only; anything else replaces)."""
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_products():
    """Load the product list from products.json (the source of truth), then merge
    products.local.json over it if present -- the machine-local settings that must
    never ship (a provisioner's command path, a release-tag override, a local build).
    If products.json is missing or invalid, fall back to a minimal built-in default
    and warn — without overwriting anything."""
    try:
        with open(PRODUCTS_FILE) as f:
            cfg = json.load(f)
        if not cfg.get("products"):
            raise ValueError("no 'products' key")
    except FileNotFoundError:
        print("[valar-flasher] products.json not found next to the script — using a "
              "minimal Ropener-only fallback. Restore products.json for the full list.")
        return FALLBACK_PRODUCTS
    except Exception as e:
        print(f"[valar-flasher] products.json couldn't be read ({e}) — using a "
              "minimal Ropener-only fallback.")
        return FALLBACK_PRODUCTS
    if os.path.exists(LOCAL_FILE):
        try:
            with open(LOCAL_FILE) as f:
                cfg = _merge(cfg, json.load(f))
        except Exception as e:
            print(f"[valar-flasher] products.local.json couldn't be read ({e}) — ignoring it.")
    return cfg


def is_variant(pcfg):
    return pcfg.get("select", "chip") == "variant"


# ---- GitHub firmware sync (per product) ------------------------------------
def _gh_headers():
    """Auth only needed for a PRIVATE repo. Set GITHUB_TOKEN, or drop a token in
    'github_token.txt' next to this script."""
    h = {"User-Agent": "valar-flasher"}
    tok = os.environ.get("GITHUB_TOKEN")
    tf = os.path.join(HERE, "github_token.txt")
    if not tok and os.path.exists(tf):
        try:
            tok = open(tf).read().strip()
        except Exception:
            tok = None
    if tok:
        h["Authorization"] = f"token {tok}"
    return h


def fw_dir(product):
    return os.path.join(FW_ROOT, product)


def bin_path(product, chip):
    return os.path.join(fw_dir(product), f"{chip}.factory.bin")


def cached_tag(product):
    try:
        return open(os.path.join(fw_dir(product), ".release_tag")).read().strip()
    except Exception:
        return None


def expected_chips(pcfg):
    return list((pcfg.get("assets") or {}).keys())


def have_bins(product, pcfg):
    chips = expected_chips(pcfg)
    return bool(chips) and all(os.path.exists(bin_path(product, c)) for c in chips)


def get_latest(repo, log):
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        req = urllib.request.Request(url, headers=_gh_headers())
        data = json.load(urllib.request.urlopen(req, timeout=30))
        return (data.get("tag_name") or ""), data.get("assets", [])
    except Exception as e:
        log(f"Couldn't reach GitHub ({repo}): {e}")
        return None


def sync_firmware(product, pcfg, log, force=False):
    """Download the selected product's factory bins if newer/missing. Never
    fails hard — offline keeps whatever is cached. Returns the loaded tag."""
    d = fw_dir(product)
    os.makedirs(d, exist_ok=True)
    repo = pcfg.get("repo", "")
    assets_map = pcfg.get("assets") or {}
    latest = get_latest(repo, log)
    if latest is None:
        log(f"Offline — using cached {product} firmware ({cached_tag(product) or 'none'})."
            if have_bins(product, pcfg) else
            f"Offline and no cached {product} firmware. Connect once, or drop bins in {d}.")
        return cached_tag(product)
    tag, assets = latest
    if not force and have_bins(product, pcfg) and tag and tag == cached_tag(product):
        log(f"{product} firmware up to date ({tag}).")
        return tag
    got = 0
    for chip, needle in assets_map.items():
        needle = needle.lower()
        match = next((a for a in assets
                      if a.get("name", "").lower().endswith(".bin")
                      and "factory" in a.get("name", "").lower()
                      and needle in a.get("name", "").lower()), None)
        if not match:
            log(f"  no {product} asset matching '{needle}' + factory + .bin")
            continue
        try:
            log(f"Downloading {match['name']} → {product}/{chip}.factory.bin …")
            req = urllib.request.Request(match["browser_download_url"], headers=_gh_headers())
            with urllib.request.urlopen(req, timeout=180) as r:
                open(bin_path(product, chip), "wb").write(r.read())
            got += 1
        except Exception as e:
            log(f"  failed: {e}")
    if got and tag:
        try:
            open(os.path.join(d, ".release_tag"), "w").write(tag)
        except Exception:
            pass
    log(f"{product} updated to {tag} ({got} file(s))." if got
        else f"No matching factory bins found in {repo} latest release.")
    return (tag if got else cached_tag(product))


# ---- variant products: manifest-driven -------------------------------------
def variant_dir(product, vname):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", vname).strip("_")
    return os.path.join(fw_dir(product), safe)


def get_release(repo, tag, log):
    """releases/latest, or one named tag when products.local.json pins one (a
    prerelease under test). Returns (tag, assets) or None."""
    if not tag:
        return get_latest(repo, log)
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    try:
        req = urllib.request.Request(url, headers=_gh_headers())
        data = json.load(urllib.request.urlopen(req, timeout=30))
        return (data.get("tag_name") or ""), data.get("assets", [])
    except Exception as e:
        log(f"Couldn't read release {tag} of {repo}: {e}")
        return None


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_variant_dir(vdir, vcfg=None):
    """(manifest, None) if the directory holds a complete, self-consistent image
    set, else (None, reason). Re-derives the manifest's proof: every file matches
    its recorded size and sha256, and the regions laid on 0xFF ARE the factory
    image with none touching a preserved span. Run at download AND before every
    write, so a file changed on disk in between is caught."""
    try:
        mpath = next(os.path.join(vdir, n) for n in sorted(os.listdir(vdir))
                     if n.startswith("flash-manifest-") and n.endswith(".json"))
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
    except (StopIteration, OSError, ValueError):
        return None, "no flash manifest here (Update firmware, or build one with flash_manifest.py)"
    if vcfg is not None:
        if vcfg.get("asset") and m.get("factory", {}).get("file") != vcfg["asset"]:
            return None, f"manifest names {m.get('factory', {}).get('file')}, products.json expects {vcfg['asset']}"
        if vcfg.get("chip") and m.get("chip") != vcfg["chip"]:
            return None, f"manifest is for {m.get('chip')}, this variant is {vcfg['chip']}"
    try:
        fac = m["factory"]
        entries = [fac] + list(m["regions"])
        for e in entries:
            p = os.path.join(vdir, e["file"])
            if os.path.getsize(p) != e["size"] or _sha256(p) != e["sha256"]:
                return None, f"{e['file']} does not match the manifest (size or sha256)"
        with open(os.path.join(vdir, fac["file"]), "rb") as f:
            factory = f.read()
        rebuilt = bytearray(b"\xff" * len(factory))
        end = 0
        for r in m["regions"]:
            off = int(r["offset"], 16)
            with open(os.path.join(vdir, r["file"]), "rb") as f:
                data = f.read()
            for p in m.get("preserve", []):
                po, ps = int(p["offset"], 16), int(p["size"], 16)
                if off < po + ps and po < off + len(data):
                    return None, f"region {r['name']} overlaps preserved {p['name']}"
            rebuilt[off:off + len(data)] = data
            end = max(end, off + len(data))
        if end != len(factory) or bytes(rebuilt) != factory:
            return None, "the manifest's regions do not reproduce the factory image"
    except (KeyError, ValueError, OSError) as e:
        return None, f"manifest incomplete ({e})"
    return m, None


def sync_variant(product, vname, vcfg, log, force=False):
    """Download a variant's manifest, every file it names and the scan report,
    check them all, and only then replace the cache. Never fails hard: offline or
    on any mismatch the previous cache stays. Returns the loaded tag."""
    vdir = variant_dir(product, vname)
    tag_file = os.path.join(vdir, ".release_tag")
    cached = open(tag_file).read().strip() if os.path.exists(tag_file) else None
    rel = get_release(vcfg["repo"], vcfg.get("release_tag"), log)
    if rel is None:
        ok = check_variant_dir(vdir, vcfg)[0] is not None if os.path.isdir(vdir) else False
        log(f"Offline — using cached {vname} ({cached})." if ok else
            f"Offline and no usable cached {vname}. Connect once.")
        return cached
    tag, assets = rel
    if not force and cached == tag and os.path.isdir(vdir) and check_variant_dir(vdir, vcfg)[0]:
        log(f"{vname} up to date ({tag}).")
        return tag
    by_name = {a.get("name"): a for a in assets}
    if vcfg["manifest"] not in by_name:
        log(f"  {vcfg['repo']} {tag} has no {vcfg['manifest']} — this release predates "
            f"flash manifests. Keeping the cache ({cached or 'none'}).")
        return cached
    tmp = tempfile.mkdtemp(prefix="vf-", dir=fw_dir(product) if os.path.isdir(fw_dir(product)) else None)
    try:
        def fetch(name):
            if name not in by_name:
                raise RuntimeError(f"{tag} has no asset {name}")
            req = urllib.request.Request(by_name[name]["browser_download_url"], headers=_gh_headers())
            with urllib.request.urlopen(req, timeout=180) as r, open(os.path.join(tmp, name), "wb") as f:
                f.write(r.read())
        log(f"Downloading {vname} {tag} …")
        fetch(vcfg["manifest"])
        with open(os.path.join(tmp, vcfg["manifest"]), encoding="utf-8") as f:
            m = json.load(f)
        for name in [m["factory"]["file"]] + [r["file"] for r in m["regions"]] + [m["scan"]["file"]]:
            fetch(name)
        man, why = check_variant_dir(tmp, vcfg)
        if man is None:
            raise RuntimeError(why)
        with open(os.path.join(tmp, m["scan"]["file"]), encoding="utf-8") as f:
            if json.load(f).get("image_sha256") != m["factory"]["sha256"]:
                raise RuntimeError("the scan report is for a different image")
        with open(os.path.join(tmp, ".release_tag"), "w") as f:
            f.write(tag)
        if os.path.isdir(vdir):
            shutil.rmtree(vdir)
        os.makedirs(os.path.dirname(vdir), exist_ok=True)
        shutil.move(tmp, vdir)
        tmp = None
        log(f"{vname} updated to {tag}.")
        return tag
    except Exception as e:
        log(f"  {vname} download refused: {e}. Keeping the cache ({cached or 'none'}).")
        return cached
    finally:
        if tmp and os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def variant_source(product, vname, vcfg):
    """(directory, is_local_build). A local build is what flash_manifest.py --out
    wrote from a `pio run` on THIS machine -- labelled everywhere it is used."""
    local = vcfg.get("local_build")
    return (local, True) if local else (variant_dir(product, vname), False)


LOCAL_BUILD_WARNING = ("LOCAL BUILD — not the image CI published. If it was built with a "
                       "-DCLOUD_FEED_KEY flag, that key goes into EVERY board you flash. "
                       "Customer boards are flashed from the release.")


# ---- the scan: before EVERY write ------------------------------------------
def public_lists(product, vname=None, vcfg=None):
    """The public-run lists a scan may trust: the checked-in list (framework
    constants traced for the chip-select products) and, for a variant, the scan
    report CI published beside the RELEASE image. A local build is judged
    against the release's report -- its framework strings are the same; a key
    baked in by a flag is in neither."""
    paths = [KNOWN_PUBLIC_FILE] if os.path.exists(KNOWN_PUBLIC_FILE) else []
    if vname:
        rdir = variant_dir(product, vname)
        if os.path.isdir(rdir):
            paths += [os.path.join(rdir, n) for n in os.listdir(rdir)
                      if n.startswith("factory-scan-") and n.endswith(".json")]
    return paths


def scan_gate(image_path, anchor, list_paths):
    """(ok, one_line_reason, full_lines). ok only on a CLEAN verdict: a hit refuses,
    and so does a failed control -- an unreadable scan is not a clean one."""
    public = set()
    for p in list_paths:
        try:
            with open(p, encoding="utf-8") as f:
                public |= set(json.load(f).get("public_runs", []))
        except (OSError, ValueError):
            pass
    with open(image_path, "rb") as f:
        data = f.read()
    v = scan_image.scan(data, anchor, public_runs=public, name=os.path.basename(image_path))
    code = scan_image.exit_code(v)
    if code == 0:
        return True, "scan clean", v.lines
    hits = [l.strip() for l in v.lines if "HIT" in l or "UNTRACED" in l or "found: False" in l
            or "flagged: False" in l or "untraced: False" in l]
    why = ("secret scan could not be trusted (a control failed)" if code == 3
           else "secret scan HIT: " + "; ".join(hits[:2]))
    return False, why, v.lines


# ---- serial + esptool ------------------------------------------------------
# --ports: when set, the ONLY ports this run may see, and therefore touch. Every
# Valar board presents the same Espressif USB ID, so on a machine with a board
# that must not be flashed (a configured unit, another project's device) the
# default "every ESP port" is exactly the wrong set. Applied inside list_ports,
# the one function every flash and bench path gets its ports from.
PORT_ALLOW = None


def _enumerate_ports():
    """[(device, vid)] for every serial port; [] if pyserial is missing."""
    try:
        from serial.tools import list_ports as lp
    except Exception:
        return []
    return [(p.device, getattr(p, "vid", None)) for p in lp.comports()]


def list_ports():
    ports = _enumerate_ports()
    if PORT_ALLOW is not None:
        return [d for d, _ in ports if d and d.upper() in PORT_ALLOW]
    esp, other = [], []
    for device, vid in ports:
        if vid in (0x303A, 0x10C4, 0x1A86, 0x0403):
            esp.append(device)
        elif device:
            other.append(device)
    return esp if esp else other


# ---- protected boards --------------------------------------------------------
# Boards on THIS machine that must never be written: a configured unit, another
# product's test board, another project's device. Kept in a local file, never in
# the repo (it names this bench's hardware), at ~/.config/valar-flasher/
# protected-macs.json or $VALAR_FLASHER_PROTECTED:
#     {"macs": {"90:70:69:32:6e:64": "COM6 -- bench unit", ...}}
# Checked FIRST by the USB serial number the OS already holds (an S3's native USB
# reports its MAC there), so a protected board is refused without even being
# reset; then again by the MAC esptool reads, before any write.
PROTECTED_FILE = os.environ.get("VALAR_FLASHER_PROTECTED") or os.path.join(
    os.path.expanduser("~"), ".config", "valar-flasher", "protected-macs.json")


def norm_mac(s):
    """'90:70:69:31:E2:08' / '90706931E208' / '90-70-..' -> '90:70:69:31:e2:08'; else None."""
    h = re.sub(r"[^0-9a-fA-F]", "", s or "").lower()
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)) if len(h) == 12 else None


class ProtectedListError(Exception):
    pass


def load_protected():
    """{mac: label}. No file = nothing protected. A file that exists but cannot be
    read is an ERROR, not an empty list -- a guard that fails open is not a guard."""
    if not os.path.exists(PROTECTED_FILE):
        return {}
    try:
        with open(PROTECTED_FILE, encoding="utf-8") as f:
            macs = json.load(f).get("macs", {})
        out = {norm_mac(k): v for k, v in macs.items()}
        if None in out:
            raise ValueError("an entry is not a MAC address")
        return out
    except (OSError, ValueError, AttributeError) as e:
        raise ProtectedListError(f"{PROTECTED_FILE} cannot be read ({e})")


def _port_serials():
    """{device: USB serial number} from the OS -- no port is opened."""
    try:
        from serial.tools import list_ports as lp
    except Exception:
        return {}
    return {p.device: getattr(p, "serial_number", None) for p in lp.comports()}


def usb_mac(port):
    return norm_mac(_port_serials().get(port))


def protected_reason(port, mac=None):
    """One line if the board on `port` (or with `mac`) is protected, else None."""
    prot = load_protected()
    for m in (usb_mac(port), norm_mac(mac) if mac else None):
        if m and m in prot:
            return f"PROTECTED board {m} ({prot[m]}) -- refused, nothing written"
    return None


def run_esptool(args, timeout=180):
    try:
        r = subprocess.run(ESPTOOL + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "TIMEOUT talking to the board (bad cable or port?)"
    except FileNotFoundError:
        return 1, "esptool is not installed — run the Setup launcher first."


def detect_chip(port):
    rc, out = run_esptool(["--port", port, "flash_id"], timeout=40)
    low = out.lower()
    for key in ("esp32-c3", "esp32-c6", "esp32-c2", "esp32-s3"):
        if key in low:
            return key.replace("-", ""), out
    return None, out


def read_mac(port):
    rc, out = run_esptool(["--port", port, "read_mac"], timeout=60)
    m = re.findall(r"MAC:\s*((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})", out)
    return (m[0].replace("-", ":").lower() if rc == 0 and m else None), out


def flash(port, chip, binpath):
    return run_esptool([
        "--chip", chip, "--port", port, "--baud", FLASH_BAUD,
        "--before", "default_reset", "--after", "hard_reset",
        "write_flash", "0x0", binpath,
    ], timeout=240)


def variant_write_pairs(vdir, manifest, factory_reset):
    """[(offset, path)] to write. Flash mode: every region, never a preserved span.
    Factory reset: the whole factory image at 0x0, which blanks NVS."""
    if factory_reset:
        return [(manifest["factory"]["offset"], os.path.join(vdir, manifest["factory"]["file"]))]
    return [(r["offset"], os.path.join(vdir, r["file"])) for r in manifest["regions"]]


def flash_pairs(port, chip, pairs):
    args = ["--chip", chip, "--port", port, "--baud", FLASH_BAUD,
            "--before", "default_reset", "--after", "hard_reset", "write_flash"]
    for off, path in pairs:
        args += [off, path]
    return run_esptool(args, timeout=300)


def flash_variant(port, product, vname, vcfg, factory_reset, log):
    """Flash one board with a variant's image. (ok, message)."""
    try:
        prot = protected_reason(port)
    except ProtectedListError as e:
        return False, f"REFUSED -- {e}"
    if prot:
        return False, prot
    vdir, local = variant_source(product, vname, vcfg)
    if local:
        log(f"[{port}] {LOCAL_BUILD_WARNING}")
    manifest, why = check_variant_dir(vdir, vcfg)
    if manifest is None:
        return False, f"{vname}: {why}"
    chip, raw = detect_chip(port)
    if not chip:
        return False, "couldn't identify the chip"
    if chip != manifest["chip"]:
        return False, (f"this board is an {CHIP_LABEL.get(chip, chip)}; {vname} needs an "
                       f"{CHIP_LABEL.get(manifest['chip'], manifest['chip'])}")
    factory = os.path.join(vdir, manifest["factory"]["file"])
    ok, why, _ = scan_gate(factory, vcfg.get("anchor", DEFAULT_ANCHOR), public_lists(product, vname, vcfg))
    if not ok:
        return False, f"REFUSED — {why}. Nothing was written."
    rc, out = flash_pairs(port, chip, variant_write_pairs(vdir, manifest, factory_reset))
    if rc != 0:
        return False, "flash FAILED — reseat & retry\n" + out.strip()[-600:]
    return True, ("flashed; settings ERASED (factory reset)" if factory_reset
                  else "flashed; settings kept (NVS untouched)")


# ---- bench mode (provisioning) ---------------------------------------------
# The bench's PROVISION_TOKEN for the Worker's mint route (Blipscope
# docs/provisioning-mint.md). One path, read by the flasher (presence only) and
# passed to the provisioner with --token-file, so the two can never disagree.
PROVISION_TOKEN_FILE = os.environ.get("VALAR_FLASHER_PROVISION_TOKEN") or os.path.join(
    os.path.expanduser("~"), ".config", "valar-flasher", "provision-token")


def provision_token_present():
    try:
        with open(PROVISION_TOKEN_FILE, encoding="utf-8") as f:
            return bool(f.read().strip())
    except OSError:
        return False


def bench_available(pcfg):
    """Shown at all only when this machine has configured a provisioner command."""
    return bool((pcfg.get("provisioner") or {}).get("command"))


def bench_refusal(pcfg):
    """None if bench mode may start, else the one sentence that says why not."""
    prov = pcfg.get("provisioner") or {}
    if not prov.get("command"):
        return "This product has no provisioner configured on this machine (products.local.json)."
    if not provision_token_present():
        return (f"Bench mode needs the provisioning token in {PROVISION_TOKEN_FILE} -- "
                f"put it there once from the password manager.")
    if prov["command"].endswith(".py") and not os.path.exists(prov["command"]):
        return f"The provisioner configured in products.local.json does not exist: {prov['command']}"
    try:
        load_protected()
    except ProtectedListError as e:
        return f"Bench mode refused: the protected-board list {e}."
    return None


def provisioned_macs(path):
    macs = set()
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("mac"):
                    macs.add(row["mac"].strip().lower())
    except OSError:
        pass
    return macs


class BenchSession:
    """One bench run: the tiles, the count, the failures. Thread-safe; the GUI
    and the console both render it."""

    def __init__(self, product, vname, vcfg, pcfg, count=0, on_change=None):
        self.product, self.vname, self.vcfg, self.pcfg = product, vname, vcfg, pcfg
        self.count = count
        self.lock = threading.Lock()
        self.tiles = {}          # key -> {"state", "detail", "mac", "port"}
        self.inflight = set()
        self.done = 0
        self.failures = []
        self.on_change = on_change or (lambda: None)
        # When bench mode ran without --ports, the operator confirmed exactly these
        # MACs; any other board that appears is refused rather than flashed unlisted.
        self.allowed_macs = None
        self.excluded_ports = set()   # listed as EXCLUDED at confirmation; never dispatched
        prov = pcfg.get("provisioner") or {}
        self.log_path = prov.get("log") or os.path.join(HERE, "provisioned.csv")

    def set(self, key, state, detail="", **kw):
        with self.lock:
            t = self.tiles.setdefault(key, {})
            t.update(state=state, detail=detail, **kw)
            if state == "FAILED":
                self.failures.append(f"{t.get('mac') or key}: {detail}")
        self.on_change()

    def rekey(self, old, new):
        """A tile starts under its PORT (nothing else is known yet) and moves to
        its MAC once read: one tile per board, and a port reused by the next board
        on the hub does not overwrite the last one's result."""
        with self.lock:
            if old in self.tiles:
                self.tiles[new] = self.tiles.pop(old)
        self.on_change()

    def reached(self):
        return bool(self.count) and self.done >= self.count

    def progress(self):
        return f"{self.done} of {self.count} this session" if self.count else f"{self.done} this session"


def provisioner_argv(pcfg, port, mac):
    prov = pcfg.get("provisioner") or {}
    cmd = prov["command"]
    head = [PY, cmd] if cmd.endswith(".py") else [cmd]
    return (head + [port, mac] + list(prov.get("args") or [])
            + ["--token-file", PROVISION_TOKEN_FILE]
            + (["--log", prov["log"]] if prov.get("log") else []))


def bench_board(port, s):
    """The whole bench pipeline for one attached board. DONE means verified:
    reached only on the provisioner's exit 0 with a RESULT OK line."""
    s.set(port, "waiting", "reading MAC", port=port)

    def refuse(mac_seen):
        try:
            prot = protected_reason(port, mac_seen)
        except ProtectedListError as e:
            return f"REFUSED -- {e}"
        if prot:
            return prot
        m = norm_mac(mac_seen) if mac_seen else usb_mac(port)
        if s.allowed_macs is not None and m not in s.allowed_macs:
            return f"board {m or 'with unknown MAC'} was not in the list confirmed at start -- re-run to include it"
        return None

    # BEFORE any contact: esptool resets the board just to read its MAC.
    why = refuse(None)
    if why:
        s.set(port, "FAILED", why, port=port)
        return "FAILED"
    mac, raw = read_mac(port)
    if not mac:
        s.set(port, "FAILED", "could not read the MAC (bad cable, or hold BOOT + tap RESET)", port=port)
        return "FAILED"
    # AGAIN on the MAC esptool read, before any write.
    why = refuse(mac)
    if why:
        s.rekey(port, mac)
        s.set(mac, "FAILED", why, mac=mac)
        return "FAILED"
    key = mac
    s.rekey(port, mac)
    with s.lock:
        if mac in s.inflight:
            skip = "already in flight on another port"
        elif mac in provisioned_macs(s.log_path):
            skip = "already in provisioned.csv"
        else:
            skip = None
            s.inflight.add(mac)
    if skip:
        s.set(key, "SKIPPED", skip, mac=mac)
        return "SKIPPED"
    try:
        s.set(key, "flashing", "", mac=mac)
        vdir, local = variant_source(s.product, s.vname, s.vcfg)
        manifest, why = check_variant_dir(vdir, s.vcfg)
        if manifest is None:
            s.set(key, "FAILED", why, mac=mac)
            return "FAILED"
        chip, _ = detect_chip(port)
        if chip != manifest["chip"]:
            s.set(key, "FAILED", f"board is {CHIP_LABEL.get(chip, chip)}, not {CHIP_LABEL.get(manifest['chip'])}", mac=mac)
            return "FAILED"
        factory = os.path.join(vdir, manifest["factory"]["file"])
        ok, why, _ = scan_gate(factory, s.vcfg.get("anchor", DEFAULT_ANCHOR), public_lists(s.product, s.vname, s.vcfg))
        if not ok:
            s.set(key, "FAILED", f"REFUSED — {why}", mac=mac)
            return "FAILED"
        # A factory board is written whole: the image blanks NVS, and the
        # provisioner writes the key into it next.
        rc, out = flash_pairs(port, chip, variant_write_pairs(vdir, manifest, factory_reset=True))
        if rc != 0:
            s.set(key, "FAILED", "flash failed: " + (out.strip().splitlines() or ["?"])[-1][-120:], mac=mac)
            return "FAILED"
        s.set(key, "provisioning", "", mac=mac)
        proc = subprocess.Popen(provisioner_argv(s.pcfg, port, mac), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        last = ""
        for line in proc.stdout:
            line = line.rstrip()
            if line == "STEP verify":
                s.set(key, "verifying", "", mac=mac)
            if line.startswith("RESULT "):
                last = line
        rc = proc.wait()
        if rc == 0 and last.startswith("RESULT OK"):
            with s.lock:
                s.done += 1
            s.set(key, "DONE", last[len("RESULT OK"):].strip(), mac=mac)
            return "DONE"
        reason = last[len("RESULT FAIL"):].strip() if last.startswith("RESULT FAIL") else f"provisioner exited {rc}"
        s.set(key, "FAILED", reason or f"provisioner exited {rc}", mac=mac)
        return "FAILED"
    except Exception as e:
        s.set(key, "FAILED", f"{type(e).__name__}: {e}", mac=mac)
        return "FAILED"
    finally:
        with s.lock:
            s.inflight.discard(mac)


def bench_loop(s, stop, ports_fn=list_ports, poll=1.0):
    """Hub-parallel: one worker per newly attached port, up to BENCH_MAX_PARALLEL,
    until the count is reached or `stop` is set. A port is not re-run until it is
    unplugged -- a finished board sitting on the hub must not be re-flashed."""
    from concurrent.futures import ThreadPoolExecutor
    busy = set()
    with ThreadPoolExecutor(max_workers=BENCH_MAX_PARALLEL) as pool:
        futures = {}
        while not stop.is_set() and not s.reached():
            present = set(ports_fn())
            busy &= present | set(futures.values())
            for port in sorted(present - busy - s.excluded_ports):
                if s.reached():
                    break
                busy.add(port)
                futures[pool.submit(bench_board, port, s)] = port
            for f in [f for f in futures if f.done()]:
                futures.pop(f)
            time.sleep(poll)
        for f in list(futures):
            f.result()


# ===========================================================================
# GUI
# ===========================================================================
TILE_COLOR = {"waiting": "#6e7681", "flashing": "#e8c15a", "provisioning": "#e8c15a",
              "verifying": "#58a6ff", "DONE": "#3fb950", "FAILED": "#e5534b", "SKIPPED": "#9aa0a6"}


def run_gui(cfg):
    import tkinter as tk
    from tkinter import scrolledtext

    products = cfg["products"]
    default_product = cfg.get("default") or next(iter(products))

    root = tk.Tk()
    root.title("Valar Flasher")
    root.geometry("640x640")
    root.configure(bg="#111418")

    q = queue.Queue()
    state = {"busy": False, "auto": True, "force": "Auto",
             "product": default_product, "handled": {}, "variant": None,
             "bench": None, "bench_stop": None}

    def log(m): q.put(("log", m))
    def set_status(m, c): q.put(("status", (m, c)))
    def set_version(t): q.put(("version", t))

    # The chosen variant, in LARGE type, for as long as it is chosen: every
    # Blipscope SKU is the same chip, so this label is the only thing standing
    # between the operator and a board flashed with the wrong SKU's image.
    variant_banner = tk.Label(root, text="", font=("Helvetica", 22, "bold"),
                              bg="#111418", fg="#58a6ff", pady=4)
    local_banner = tk.Label(root, text="", font=("Helvetica", 11, "bold"), wraplength=600,
                            bg="#5a1d1d", fg="#ffffff", pady=4)
    status = tk.Label(root, text="Plug in a board…", font=("Helvetica", 18, "bold"),
                      bg="#111418", fg="#e6e6e6", pady=10)
    status.pack(fill="x")
    counter = tk.Label(root, text="Flashed this session: 0",
                       font=("Helvetica", 11), bg="#111418", fg="#9aa0a6")
    counter.pack()
    version = tk.Label(root, text="Firmware: checking…",
                       font=("Helvetica", 10), bg="#111418", fg="#6e7681")
    version.pack()

    top = tk.Frame(root, bg="#111418"); top.pack(fill="x", padx=12, pady=(8, 0))
    tk.Label(top, text="Firmware:", bg="#111418", fg="#c9d1d9",
             font=("Helvetica", 12, "bold")).pack(side="left")
    product_var = tk.StringVar(value=default_product)
    tk.OptionMenu(top, product_var, *products.keys()).pack(side="left", padx=6)
    variant_var = tk.StringVar(value="")
    variant_menu_holder = tk.Frame(top, bg="#111418"); variant_menu_holder.pack(side="left")

    opts = tk.Frame(root, bg="#111418"); opts.pack(fill="x", padx=12, pady=(4, 0))
    reset_var = tk.BooleanVar(value=False)
    reset_box = tk.Checkbutton(opts, text="Factory reset (erase settings)", variable=reset_var,
                               bg="#111418", fg="#e5534b", selectcolor="#111418",
                               activebackground="#111418", activeforeground="#e5534b")
    bench_var = tk.BooleanVar(value=False)
    bench_box = tk.Checkbutton(opts, text="Bench mode (provision keys)", variable=bench_var,
                               bg="#111418", fg="#c9d1d9", selectcolor="#111418",
                               activebackground="#111418", activeforeground="#c9d1d9")
    tk.Label(opts, text="  count:", bg="#111418", fg="#9aa0a6")
    count_var = tk.StringVar(value="0")
    count_entry = tk.Entry(opts, textvariable=count_var, width=5)

    tiles_frame = tk.Frame(root, bg="#111418")
    logbox = scrolledtext.ScrolledText(root, height=10, bg="#0b0d10", fg="#c9d1d9",
                                       insertbackground="#c9d1d9", font=("Menlo", 10),
                                       relief="flat")
    logbox.pack(fill="both", expand=True, padx=12, pady=10)

    controls = tk.Frame(root, bg="#111418")
    controls.pack(fill="x", padx=12, pady=(0, 12))
    auto_var = tk.BooleanVar(value=True)
    tk.Checkbutton(controls, text="Auto-flash on plug-in", variable=auto_var,
                   bg="#111418", fg="#c9d1d9", selectcolor="#111418",
                   activebackground="#111418", activeforeground="#c9d1d9",
                   command=lambda: state.update(auto=auto_var.get())).pack(side="left")
    force_var = tk.StringVar(value="Auto")
    force_lbl = tk.Label(controls, text="  Force:", bg="#111418", fg="#9aa0a6")
    force_menu = tk.OptionMenu(controls, force_var, "Auto", "VAL3000", "VAL3100")
    force_lbl.pack(side="left"); force_menu.pack(side="left")

    counts = {"n": 0}

    def pcfg():
        return products[state["product"]]

    def vcfg():
        return (pcfg().get("variants") or {}).get(state["variant"] or "")

    def refresh_version():
        p = state["product"]
        if is_variant(pcfg()):
            v = state["variant"]
            vdir, local = variant_source(p, v, vcfg())
            ok = check_variant_dir(vdir, vcfg())[0] is not None if os.path.isdir(vdir) else False
            tag = None
            try:
                tag = open(os.path.join(vdir, ".release_tag")).read().strip()
            except Exception:
                pass
            set_version(f"{p} · {v}: {'local build' if local else (tag or 'none yet')}"
                        f"{'' if ok else '  (incomplete — Update)'}")
        else:
            set_version(f"{p}: {cached_tag(p) or 'none yet'}"
                        f"{'' if have_bins(p, pcfg()) else '  (incomplete — Update)'}")

    def refresh_layout():
        """Show the controls the selected product needs, and nothing else."""
        variant = is_variant(pcfg())
        for w in variant_menu_holder.winfo_children():
            w.destroy()
        for w in (variant_banner, local_banner, reset_box, bench_box, count_entry, tiles_frame):
            w.pack_forget()
        if variant:
            names = list((pcfg().get("variants") or {}).keys())
            if state["variant"] not in names:
                state["variant"] = pcfg().get("default_variant") or names[0]
            variant_var.set(state["variant"])
            tk.OptionMenu(variant_menu_holder, variant_var, *names).pack(side="left", padx=6)
            variant_banner.config(text=f"{state['product'].upper()} · {state['variant']}")
            variant_banner.pack(fill="x", before=status)
            if vcfg().get("local_build"):
                local_banner.config(text=LOCAL_BUILD_WARNING)
                local_banner.pack(fill="x", before=status)
            reset_box.pack(side="left")
            if bench_available(pcfg()):
                bench_box.pack(side="left", padx=(12, 0))
                count_entry.pack(side="left")
            force_lbl.pack_forget(); force_menu.pack_forget()
        else:
            state["variant"] = None
            force_lbl.pack(side="left"); force_menu.pack(side="left")
            bench_var.set(False)

    def do_one_flash(port):
        state["busy"] = True
        p = state["product"]
        if is_variant(pcfg()):
            v = state["variant"]
            set_status(f"Flashing {v}…  (don't unplug)", "#e8c15a")
            log(f"[{port}] → {p} · {v} ({'FACTORY RESET' if reset_var.get() else 'settings kept'})")
            t0 = time.time()
            ok, msg = flash_variant(port, p, v, vcfg(), reset_var.get(), log)
            if ok:
                counts["n"] += 1; q.put(("count", counts["n"]))
                set_status(f"✓ {v} DONE — unplug & next", "#3fb950")
                log(f"[{port}] {msg} in {time.time() - t0:.0f}s.\n")
                state["handled"][port] = "ok"
            else:
                set_status("✗ " + msg.splitlines()[0][:60], "#e5534b")
                log(f"[{port}] {msg}\n")
                state["handled"][port] = "err"
            state["busy"] = False
            return
        try:
            prot = protected_reason(port)
        except ProtectedListError as e:
            prot = f"REFUSED -- {e}"
        if prot:
            set_status("✗ PROTECTED board — refused", "#e5534b")
            log(f"[{port}] {prot}")
            state["busy"] = False; state["handled"][port] = "err"; return
        set_status(f"Detecting {os.path.basename(port)}…", "#e8c15a")
        chip, raw = detect_chip(port)
        if not chip:
            set_status("Couldn't identify the chip", "#e5534b")
            log(f"[{port}] chip not recognized:\n{raw.strip()[-400:]}")
            state["busy"] = False; state["handled"][port] = "err"; return
        forced = state["force"]
        chip_eff = ("esp32c3" if forced == "VAL3000" else
                    "esp32c6" if forced == "VAL3100" else chip)
        binp = bin_path(p, chip_eff)
        clabel = CHIP_LABEL.get(chip_eff, chip_eff)
        if chip_eff not in expected_chips(pcfg()):
            set_status(f"{p} has no {clabel} firmware", "#e5534b")
            log(f"[{port}] {p} defines no firmware for {clabel}. Pick another firmware or board.")
            state["busy"] = False; state["handled"][port] = "err"; return
        if not os.path.exists(binp):
            set_status(f"Missing {p} {clabel} bin", "#e5534b")
            log(f"[{port}] need {p}/{chip_eff}.factory.bin — click 'Update firmware'.")
            state["busy"] = False; state["handled"][port] = "err"; return
        ok, why, _ = scan_gate(binp, pcfg().get("anchor", DEFAULT_ANCHOR), public_lists(p))
        if not ok:
            set_status("✗ REFUSED — secret scan", "#e5534b")
            log(f"[{port}] REFUSED {p}/{chip_eff}.factory.bin — {why}. Nothing was written.")
            state["busy"] = False; state["handled"][port] = "err"; return
        log(f"[{port}] {clabel} → {p} firmware, flashing …")
        set_status(f"Flashing {p} · {clabel}…  (don't unplug)", "#e8c15a")
        t0 = time.time()
        rc, out = flash(port, chip_eff, binp)
        dt = time.time() - t0
        if rc == 0:
            counts["n"] += 1; q.put(("count", counts["n"]))
            set_status(f"✓ {p} · {clabel} DONE — unplug & next", "#3fb950")
            log(f"[{port}] flashed in {dt:.0f}s.\n")
            state["handled"][port] = "ok"
        else:
            set_status(f"✗ FAILED — reseat & retry", "#e5534b")
            log(f"[{port}] FAILED:\n{out.strip()[-600:]}\n")
            state["handled"][port] = "err"
        state["busy"] = False

    def worker():
        while True:
            if state["auto"] and not state["busy"] and not state["bench"]:
                present = set(list_ports())
                for p in list(state["handled"]):
                    if p not in present:
                        del state["handled"][p]
                for p in present:
                    if p not in state["handled"]:
                        do_one_flash(p); break
            time.sleep(0.8)

    def manual_flash():
        if state["busy"] or state["bench"]:
            return
        ports = list_ports()
        if not ports:
            log("No board detected. Plug one in."); return
        threading.Thread(target=do_one_flash, args=(ports[0],), daemon=True).start()

    def sync_selected(force=False):
        def go():
            if is_variant(pcfg()):
                sync_variant(state["product"], state["variant"], vcfg(), log, force=force)
            else:
                sync_firmware(state["product"], pcfg(), log, force=force)
            refresh_version()
        threading.Thread(target=go, daemon=True).start()

    tk.Button(controls, text="Flash now", command=manual_flash).pack(side="right")
    tk.Button(controls, text="Update firmware", command=lambda: sync_selected(True)).pack(side="right", padx=6)

    def render_tiles():
        s = state["bench"]
        for w in tiles_frame.winfo_children():
            w.destroy()
        if not s:
            return
        with s.lock:
            tiles = list(s.tiles.items())
            head = s.progress()
            fails = list(s.failures)
        tk.Label(tiles_frame, text=head, font=("Helvetica", 12, "bold"),
                 bg="#111418", fg="#c9d1d9").grid(row=0, column=0, columnspan=4, sticky="w")
        for i, (k, t) in enumerate(tiles):
            txt = f"{t.get('mac') or k}\n{t['state']}" + (f"\n{t['detail'][:40]}" if t.get("detail") else "")
            tk.Label(tiles_frame, text=txt, width=22, height=3, bg=TILE_COLOR.get(t["state"], "#6e7681"),
                     fg="#0b0d10", font=("Helvetica", 9, "bold")).grid(row=1 + i // 4, column=i % 4, padx=2, pady=2)
        if fails and (s.reached() or (state["bench_stop"] and state["bench_stop"].is_set())):
            tk.Label(tiles_frame, text="FAILED this session:\n" + "\n".join(fails), justify="left",
                     bg="#111418", fg="#e5534b").grid(row=99, column=0, columnspan=4, sticky="w")

    def on_bench(*_):
        if bench_var.get():
            why = bench_refusal(pcfg())
            if why:
                bench_var.set(False)
                set_status("Bench mode refused", "#e5534b")
                log(why)
                return
            if vcfg().get("local_build"):
                log(LOCAL_BUILD_WARNING)
            try:
                n = int(count_var.get() or 0)
            except ValueError:
                n = 0
            s = BenchSession(state["product"], state["variant"], vcfg(), pcfg(), count=n,
                             on_change=lambda: q.put(("tiles", None)))
            if PORT_ALLOW is None:
                from tkinter import messagebox
                targets, lines = bench_targets()
                if not targets or not messagebox.askyesno(
                        "Bench mode — confirm boards",
                        "\n".join(lines) + f"\n\nFlash AND provision these {len(targets)} board(s)? "
                        "Everything on them is erased. Boards attached later are NOT included."):
                    bench_var.set(False)
                    log("Bench mode not confirmed. Nothing done.")
                    return
                s.allowed_macs = set(targets.values())
                s.excluded_ports = set(list_ports()) - set(targets)
            stop = threading.Event()
            state["bench"], state["bench_stop"] = s, stop
            tiles_frame.pack(fill="x", padx=12, before=logbox)
            set_status(f"BENCH · {state['variant']} — load the hub", "#58a6ff")

            def run():
                bench_loop(s, stop)
                q.put(("tiles", None))
                set_status(f"Bench session over — {s.progress()}", "#3fb950" if not s.failures else "#e5534b")
                for f in s.failures:
                    log(f"FAILED  {f}")
            threading.Thread(target=run, daemon=True).start()
        elif state["bench_stop"]:
            state["bench_stop"].set()
            state["bench"] = None
    bench_var.trace_add("write", on_bench)

    def on_product(*_):
        if state["bench_stop"]:
            state["bench_stop"].set(); state["bench"] = None; bench_var.set(False)
        state["product"] = product_var.get()
        state["handled"].clear()
        refresh_layout()
        log(f"— firmware set to {state['product']}"
            + (f" · {state['variant']}" if state["variant"] else "") + " —")
        sync_selected(False)
    product_var.trace_add("write", on_product)

    def on_variant(*_):
        if variant_var.get() and variant_var.get() != state["variant"]:
            state["variant"] = variant_var.get()
            state["handled"].clear()
            refresh_layout()
            log(f"— variant set to {state['variant']} —")
            sync_selected(False)
    variant_var.trace_add("write", on_variant)

    def on_force(*_):
        state["force"] = force_var.get()
    force_var.trace_add("write", on_force)

    def pump():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "log":
                    logbox.insert("end", payload + "\n"); logbox.see("end")
                elif kind == "status":
                    m, c = payload; status.config(text=m, fg=c)
                elif kind == "count":
                    counter.config(text=f"Flashed this session: {payload}")
                elif kind == "version":
                    version.config(text=payload)
                elif kind == "tiles":
                    render_tiles()
        except queue.Empty:
            pass
        root.after(120, pump)

    def startup():
        log(f"Firmware source: {state['product']}. Checking GitHub for the latest release…")
        if is_variant(pcfg()):
            sync_variant(state["product"], state["variant"], vcfg(), log)
        else:
            sync_firmware(state["product"], pcfg(), log)
        refresh_version()
        log("Ready. Plug in a board.")
    refresh_layout()
    threading.Thread(target=startup, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    root.after(120, pump)
    if os.environ.get("VALAR_FLASHER_GUI_SMOKE"):
        root.after(int(os.environ["VALAR_FLASHER_GUI_SMOKE"]), root.destroy)
    root.mainloop()


# ===========================================================================
# Console fallback
# ===========================================================================
def bench_targets():
    """({port: mac} bench mode would flash, [lines to show]). Reads USB serial
    numbers only -- no board is opened or reset to build this list."""
    prot = load_protected()
    targets, lines = {}, ["Bench mode sees these boards:"]
    for port in list_ports():
        m = usb_mac(port)
        if not m:
            lines.append(f"  {port:8} MAC unknown        -- EXCLUDED (cannot identify it without touching it)")
        elif m in prot:
            lines.append(f"  {port:8} {m}  -- EXCLUDED: PROTECTED ({prot[m]})")
        else:
            targets[port] = m
            lines.append(f"  {port:8} {m}  -- WILL BE FLASHED AND PROVISIONED")
    return targets, lines


def run_console(cfg, product=None, variant=None, bench=False, count=0, factory_reset=False):
    products = cfg["products"]
    product = product or cfg.get("default") or next(iter(products))
    pcfg = products[product]
    if is_variant(pcfg):
        names = list((pcfg.get("variants") or {}).keys())
        variant = variant or pcfg.get("default_variant") or names[0]
        vcfg = pcfg["variants"][variant]
        print(f"Valar Flasher (console mode).\n\n    >>> {product.upper()} · {variant} <<<\n")
        if vcfg.get("local_build"):
            print(f"!! {LOCAL_BUILD_WARNING}\n")
        sync_variant(product, variant, vcfg, print)
        if bench:
            why = bench_refusal(pcfg)
            if why:
                print(why)
                return 2
            s = BenchSession(product, variant, vcfg, pcfg, count=count)
            if PORT_ALLOW is None:
                targets, lines = bench_targets()
                print("\n".join(lines))
                if not targets:
                    print("No board to flash. Nothing done.")
                    return 2
                ans = input(f"\nFlash AND provision these {len(targets)} board(s)? Everything on them is erased. "
                            f"Type yes to continue: ").strip().lower()
                if ans != "yes":
                    print("Not confirmed. Nothing done.")
                    return 2
                s.allowed_macs = set(targets.values())
                s.excluded_ports = set(list_ports()) - set(targets)
                print("Boards attached after this point are NOT included -- re-run to add them.\n")
            last = {}

            def show():
                with s.lock:
                    for k, t in s.tiles.items():
                        line = f"  [{t.get('mac') or k}] {t['state']} {t.get('detail', '')}".rstrip()
                        if last.get(k) != line:
                            last[k] = line
                            print(line, flush=True)
            s.on_change = show
            stop = threading.Event()
            try:
                bench_loop(s, stop)
            except KeyboardInterrupt:
                stop.set()
            print(f"\nBench session over — {s.progress()}")
            for f in s.failures:
                print(f"  FAILED  {f}")
            return 1 if s.failures else 0
        print("Plug in boards one at a time. Ctrl-C to quit.\n")
        handled, n = {}, 0
        try:
            while True:
                present = set(list_ports())
                for p in list(handled):
                    if p not in present:
                        del handled[p]
                for p in present:
                    if p in handled:
                        continue
                    ok, msg = flash_variant(p, product, variant, vcfg, factory_reset, print)
                    print(f"[{p}] {variant}: {'DONE (#%d) — ' % (n + 1) if ok else 'FAILED — '}{msg}\n")
                    n += ok
                    handled[p] = 1
                time.sleep(0.8)
        except KeyboardInterrupt:
            print(f"\nSession total: {n} flashed.")
        return 0
    print(f"Valar Flasher (console mode). Firmware source: {product}")
    sync_firmware(product, pcfg, print)
    print("Plug in boards one at a time. Ctrl-C to quit.\n")
    handled, n = {}, 0
    try:
        while True:
            present = set(list_ports())
            for p in list(handled):
                if p not in present:
                    del handled[p]
            for p in present:
                if p in handled:
                    continue
                try:
                    prot = protected_reason(p)
                except ProtectedListError as e:
                    prot = f"REFUSED -- {e}"
                if prot:
                    print(f"[{p}] {prot}"); handled[p] = 1; continue
                chip, raw = detect_chip(p)
                if not chip:
                    print(f"[{p}] chip not recognized"); handled[p] = 1; continue
                binp = bin_path(product, chip)
                if not os.path.exists(binp):
                    print(f"[{p}] {product}: missing {chip}.factory.bin"); handled[p] = 1; continue
                ok, why, _ = scan_gate(binp, pcfg.get("anchor", DEFAULT_ANCHOR), public_lists(product))
                if not ok:
                    print(f"[{p}] REFUSED {chip}.factory.bin — {why}. Nothing was written."); handled[p] = 1; continue
                print(f"[{p}] {CHIP_LABEL.get(chip, chip)} -> {product}: flashing …")
                rc, out = flash(p, chip, binp)
                print(f"[{p}] DONE (#{n+1}) — unplug & next\n" if rc == 0
                      else f"[{p}] FAILED:\n{out.strip()[-500:]}\n")
                if rc == 0:
                    n += 1
                handled[p] = 1
            time.sleep(0.8)
    except KeyboardInterrupt:
        print(f"\nSession total: {n} flashed.")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Valar Flasher")
    ap.add_argument("--console", action="store_true", help="no window")
    ap.add_argument("--product")
    ap.add_argument("--variant")
    ap.add_argument("--bench", action="store_true", help="bench mode (console)")
    ap.add_argument("--count", type=int, default=0, help="bench: stop after this many new boards")
    ap.add_argument("--factory-reset", action="store_true", help="erase settings (console flash mode)")
    ap.add_argument("--ports", help="comma-separated: the ONLY ports this run may touch, e.g. COM18")
    a = ap.parse_args()
    if a.ports:
        PORT_ALLOW = {p.strip().upper() for p in a.ports.split(",") if p.strip()}
        print(f"[valar-flasher] only these ports will be touched: {', '.join(sorted(PORT_ALLOW))}")
    cfg = load_products()
    if a.console or a.bench or a.product or a.variant:
        sys.exit(run_console(cfg, a.product, a.variant, a.bench, a.count, a.factory_reset))
    try:
        import tkinter  # noqa
        run_gui(cfg)
    except Exception:
        run_console(cfg)
