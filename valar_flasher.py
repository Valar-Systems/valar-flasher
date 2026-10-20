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

IN THE WINDOW, the boards already connected when it opens are IGNORED (BoardList):
never touched, not even reset, until the operator flips its switch in the Connected
boards list. Boards plugged in after that are flashed.

BEFORE A CHIP-SELECT WRITE the name of the firmware already on the board is read
from its app descriptor. Ropener (VAL3100), Generic board and Glasscalibur share
the ESP32-C6, so if that name is ANOTHER product's, the operator is asked first.

For each product it checks the repo's latest GitHub release on launch and caches
the download (offline? it uses whatever is cached), and it checks this repo's main
branch for a newer flasher, which the window offers to install. Every board it
tries is added to flash-log.csv. Runs a small Tk window; falls back to a console
loop if Tk is unavailable. Requires esptool (the launcher installs it for you).
"""
import os, sys, json, subprocess, threading, queue, time, urllib.request
import base64, csv, hashlib, re, shutil, stat, tempfile

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


# ---- remembered choices ------------------------------------------------------
# The firmware (and variant) last picked on this machine, and whether sounds are
# on. A convenience only: a missing or unreadable file just means the defaults.
SETTINGS_FILE = os.environ.get("VALAR_FLASHER_SETTINGS") or os.path.join(
    os.path.expanduser("~"), ".config", "valar-flasher", "settings.json")


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    s = _merge(load_settings(), changes)
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=1)
    except OSError:
        pass


def starting_product(cfg):
    """The firmware last picked here if it still exists, else products.json's default."""
    products = cfg["products"]
    last = load_settings().get("product")
    return last if last in products else (cfg.get("default") or next(iter(products)))


def starting_variant(product, pcfg):
    names = list((pcfg.get("variants") or {}).keys())
    last = (load_settings().get("variants") or {}).get(product)
    return last if last in names else (pcfg.get("default_variant") or names[0])


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


# ---- updating the flasher itself ---------------------------------------------
# The firmware is kept current from GitHub; this keeps the FLASHER current the
# same way, so nobody has to download a new ZIP. Each file is compared with main
# by git's own blob hash, so there is no version number to remember to bump. A git
# checkout is left to git.
APP_REPO = "Valar-Systems/valar-flasher"
APP_BRANCH = "main"
UPDATE_SKIP = ("tests/", ".github/")     # development only; not needed to flash
UPDATE_BACKUP = os.path.join(HERE, ".update-backup")


def _blob_sha(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _local_blob_sha(data):
    """What git would store these bytes as: text LF-normalised (.gitattributes is
    text=auto), so a copy with Windows line endings still matches."""
    if b"\0" not in data[:8000]:
        data = data.replace(b"\r\n", b"\n")
    return _blob_sha(data)


def _gh_json(url, timeout=30):
    req = urllib.request.Request(url, headers=_gh_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def app_update_check(log=lambda m: None):
    """[{path, sha, mode}] for every file on main that differs from, or is missing
    in, this folder. [] when up to date; None when it cannot tell (offline, or
    this folder is a git checkout)."""
    if os.path.isdir(os.path.join(HERE, ".git")):
        return None
    try:
        tree = _gh_json(f"https://api.github.com/repos/{APP_REPO}/git/trees/{APP_BRANCH}?recursive=1")
    except Exception as e:
        log(f"Couldn't check for a newer flasher: {e}")
        return None
    if tree.get("truncated"):
        return None
    changed = []
    for e in tree.get("tree", []):
        path = e.get("path", "")
        parts = path.split("/")
        if (e.get("type") != "blob" or e.get("mode") == "120000" or path.startswith(UPDATE_SKIP)
                or ".." in parts or path.startswith("/")):
            continue
        try:
            with open(os.path.join(HERE, *parts), "rb") as f:
                same = _local_blob_sha(f.read()) == e["sha"]
        except OSError:
            same = False
        if not same:
            changed.append({"path": path, "sha": e["sha"], "mode": e.get("mode", "100644")})
    return changed


def app_update_apply(changed, log=lambda m: None):
    """Download every changed file and check it against its hash, and only then
    replace them, keeping each old file in .update-backup/<time>/. (ok, message).
    A failed download changes nothing; a failed write puts back what it replaced."""
    staged = []
    try:
        for e in changed:
            blob = _gh_json(f"https://api.github.com/repos/{APP_REPO}/git/blobs/{e['sha']}", timeout=60)
            data = base64.b64decode(blob.get("content", ""))
            if _blob_sha(data) != e["sha"]:
                raise RuntimeError(f"{e['path']} did not match its hash")
            if e["path"].endswith(".bat"):          # .gitattributes: *.bat text eol=crlf
                data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            staged.append((e["path"], data, e["mode"]))
    except Exception as ex:
        return False, f"Update not installed: {ex}. Nothing was changed."
    backup = os.path.join(UPDATE_BACKUP, time.strftime("%Y%m%d-%H%M%S"))
    done = []                                       # (dest, backup copy or None)
    tmp = None
    try:
        for path, data, mode in staged:
            dest = os.path.join(HERE, *path.split("/"))
            saved = None
            if os.path.exists(dest):
                saved = os.path.join(backup, *path.split("/"))
                os.makedirs(os.path.dirname(saved), exist_ok=True)
                shutil.copy2(dest, saved)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".new"
            with open(tmp, "wb") as f:
                f.write(data)
            if mode == "100755" or (saved and os.access(saved, os.X_OK)):
                os.chmod(tmp, 0o755)
            os.replace(tmp, dest)
            done.append((dest, saved))
            log(f"  updated {path}")
    except OSError as ex:
        for dest, saved in reversed(done):
            try:
                if saved:
                    shutil.copy2(saved, dest)
                else:
                    os.remove(dest)
            except OSError:
                pass
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
        return False, (f"Update not installed: couldn't write {ex.filename or 'a file'} "
                       f"({ex.strerror or ex}). Your old files were put back.")
    return True, (f"Updated {len(done)} file(s). Close and reopen Valar Flasher to use the new version. "
                  f"The old files are in {backup}.")


# ---- variant products: manifest-driven -------------------------------------
def variant_dir(product, vname):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", vname).strip("_")
    return os.path.join(fw_dir(product), safe)


# A prerelease is a release UNDER TEST, not a release: the scratch build that
# COM18 was provisioned from (2026-09-25) was a prerelease carrying an unreleased
# build labelled 14. Customer boards are written from a tagged release;
# --allow-prerelease is the explicit, visible way round that, for the bench only.
ALLOW_PRERELEASE = False
RELEASE_RECORD = ".release.json"   # {tag, prerelease}, written beside a synced image set


def get_release(repo, tag, log):
    """releases/latest, or one named tag when products.local.json pins one.
    Returns (tag, assets, prerelease) or None. A response without the prerelease
    field counts AS a prerelease: unknown is not tagged."""
    url = (f"https://api.github.com/repos/{repo}/releases/tags/{tag}" if tag
           else f"https://api.github.com/repos/{repo}/releases/latest")
    try:
        req = urllib.request.Request(url, headers=_gh_headers())
        data = json.load(urllib.request.urlopen(req, timeout=30))
        return (data.get("tag_name") or ""), data.get("assets", []), bool(data.get("prerelease", True))
    except Exception as e:
        log(f"Couldn't read release {tag or 'latest'} of {repo}: {e}")
        return None


def read_release_record(vdir):
    try:
        with open(os.path.join(vdir, RELEASE_RECORD), encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) and rec.get("tag") else None
    except (OSError, ValueError):
        return None


def release_refusal(vdir, local, bench):
    """None if this image set may be written, else why not -- checked at USE time,
    not only at download, so a cache synced from a prerelease (or before this check
    existed) cannot reach a board by being already on disk."""
    if local:
        return ("bench mode writes a tagged release, never a local build" if bench else None)
    rec = read_release_record(vdir)
    if rec is None:
        if not os.path.isdir(vdir) or not any(n.startswith("flash-manifest-") for n in os.listdir(vdir)):
            return ("no firmware downloaded for this variant yet -- see the log for why the download "
                    "failed, then click Update firmware")
        return "no release record for this image set (synced before release checks) -- Update firmware"
    if rec.get("prerelease") and not ALLOW_PRERELEASE:
        return f"{rec['tag']} is a PRERELEASE -- not written without --allow-prerelease"
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


# Windows: OneDrive and virus scanners hold a file (and its folder) for a moment
# after it is written, and refuse to let it be replaced, deleted or renamed --
# "[WinError 5] Access is denied". A short retry gets past a moment's hold.
RETRY_WAIT = 0.5


def _retry(fn, *args, tries=10):
    for i in range(tries):
        try:
            return fn(*args)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(RETRY_WAIT)


def _remove(path):
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)       # a read-only file can't be removed on Windows
        os.remove(path)


def _put(src, dst):
    if os.path.exists(dst):
        _remove(dst)
    shutil.copyfile(src, dst)


def install_image_set(src, vdir, vcfg, tag, record):
    """Copy a checked image set from `src` into `vdir`, file by file, then write its
    release record. The variant FOLDER is never deleted or renamed: on a folder that
    OneDrive or a virus scanner is watching, Windows refuses that with Access is
    denied for a while after its files were written (the 2026-10-20 bench report).
    The record is removed first and written last, after the installed copy passes
    its manifest check again, so an install that stops halfway leaves a set that
    every write refuses -- never a half-new one."""
    os.makedirs(vdir, exist_ok=True)
    meta = (RELEASE_RECORD, ".release_tag")
    for name in meta:
        if os.path.exists(os.path.join(vdir, name)):
            _retry(_remove, os.path.join(vdir, name))
    names = set(os.listdir(src)) - set(meta)
    for name in os.listdir(vdir):
        # nothing from an older release stays beside the new one (an old scan
        # report would widen what the secret scan accepts)
        if name not in names and os.path.isfile(os.path.join(vdir, name)):
            _retry(_remove, os.path.join(vdir, name))
    for name in sorted(names):
        _retry(_put, os.path.join(src, name), os.path.join(vdir, name))
    man, why = check_variant_dir(vdir, vcfg)
    if man is None:
        raise RuntimeError(f"the installed copy failed its check ({why})")

    def write(name, text):
        with open(os.path.join(vdir, name), "w", encoding="utf-8") as f:
            f.write(text)
    _retry(write, ".release_tag", tag)
    _retry(write, RELEASE_RECORD, json.dumps(record))


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
    tag, assets, prerelease = rel
    if prerelease and not ALLOW_PRERELEASE:
        log(f"  {vcfg['repo']} {tag} is a PRERELEASE — refused (pass --allow-prerelease to use it). "
            f"Keeping the cache ({cached or 'none'}).")
        return cached
    if (not force and cached == tag and os.path.isdir(vdir) and read_release_record(vdir)
            and check_variant_dir(vdir, vcfg)[0]):
        log(f"{vname} up to date ({tag}).")
        return tag
    by_name = {a.get("name"): a for a in assets}
    if vcfg["manifest"] not in by_name:
        log(f"  {vcfg['repo']} {tag} has no {vcfg['manifest']} — this release predates "
            f"flash manifests. Keeping the cache ({cached or 'none'}).")
        return cached
    # Staged in the system's temp folder, not beside the cache, where a folder
    # OneDrive syncs starts uploading every file the moment it is written. Clear out
    # the staging folders older versions left beside the cache when a swap failed.
    if os.path.isdir(fw_dir(product)):
        for n in os.listdir(fw_dir(product)):
            if n.startswith("vf-"):
                shutil.rmtree(os.path.join(fw_dir(product), n), ignore_errors=True)
    tmp = tempfile.mkdtemp(prefix="valar-flasher-")
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
        # THE APP REGION IS THIS RELEASE'S firmware-<slug>.bin -- the binary OTA ships.
        # check_variant_dir already hashed every region file against the manifest,
        # and every file came from THIS release; what it cannot establish is WHICH
        # file the app region names. A self-consistent set naming "app-<slug>.bin"
        # passes it while the release's OTA binary is something else entirely.
        slug = m.get("slug")
        ota = f"firmware-{slug}.bin"
        if vcfg["manifest"] != f"flash-manifest-{slug}.json":
            raise RuntimeError(f"the manifest is for slug {slug!r}, not {vcfg['manifest']}")
        app = [r for r in m["regions"] if r.get("name") == "app"]
        if len(app) != 1 or app[0].get("file") != ota:
            raise RuntimeError(f"the manifest's app region is not {ota}, the binary {tag} ships to devices")
        install_image_set(tmp, vdir, vcfg, tag,
                          {"tag": tag, "prerelease": prerelease, "app_sha256": app[0]["sha256"]})
        log(f"{vname} updated to {tag}.")
        return tag
    except Exception as e:
        log(f"  {vname} download refused: {e}. Keeping the cache ({cached or 'none'}).")
        if isinstance(e, PermissionError):
            log(f"  Windows wouldn't let the flasher write in {vdir}. Close any window showing that "
                f"folder, or delete it, then click Update firmware.")
        return cached
    finally:
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


def port_details():
    """{device: (description, USB serial number)} from the OS -- no port is opened,
    so listing a board never resets it."""
    try:
        from serial.tools import list_ports as lp
    except Exception:
        return {}
    out = {}
    for p in lp.comports():
        desc = (getattr(p, "description", "") or "").replace(f"({p.device})", "").strip()
        out[p.device] = ("" if desc in ("n/a", p.device) else desc, getattr(p, "serial_number", None))
    return out


class BoardList:
    """The boards the window can see, and which of them it must leave alone.

    The boards ALREADY CONNECTED when the window opens (the first update) are
    IGNORED automatically: never touched -- not even reset to read its chip --
    until the operator flips its switch. Every board plugged in after that is
    flashed, whether auto-flash was on or off when it arrived: a board plugged in
    with the flasher open is a board to flash. (Ignoring boards that arrived while
    auto-flash was off was tried, and it ignored the very board the operator had
    just plugged in.) An IGNORED board stays ignored if it is unplugged and plugged
    back in, recognised by its USB serial number (on these chips, its MAC); a board
    that reports none is known only by its port. A protected board
    (protected-macs.json) is always ignored, and its switch cannot be flipped."""

    def __init__(self):
        self.lock = threading.Lock()
        self.rows = {}               # port -> {"key", "ignore", "protected", "desc", "mac", "info", "status"}
        self.ignored_keys = set()    # serial numbers of ignored boards unplugged this session
        self.opened = False          # set by the first update: what it sees was already connected

    def update(self, ports, details=None):
        """Bring the list in line with the ports present now. True if it changed."""
        details = details or {}
        prot = None                  # read only when a new board appears: this runs every 0.8 s
        already_connected = not self.opened
        self.opened = True
        changed = False
        with self.lock:
            for port in [p for p in self.rows if p not in ports]:
                row = self.rows.pop(port)
                if row["key"] and row["ignore"]:
                    self.ignored_keys.add(row["key"])
                changed = True
            for port in ports:
                if port in self.rows:
                    continue
                desc, serial = details.get(port, ("", None))
                mac = norm_mac(serial)
                if prot is None:
                    try:
                        prot = load_protected()
                    except ProtectedListError:
                        prot = {}
                protected = bool(mac and mac in prot)
                self.rows[port] = {"key": serial or None, "protected": protected, "desc": desc, "mac": mac,
                                   "ignore": protected or already_connected or (serial in self.ignored_keys),
                                   "info": "", "status": ""}
                changed = True
        return changed

    def toggle(self, port):
        """Flip a board's switch. Returns its new state, or None if it can't be flipped."""
        with self.lock:
            row = self.rows.get(port)
            if not row or row["protected"]:
                return None
            row["ignore"] = not row["ignore"]
            if not row["ignore"]:
                self.ignored_keys.discard(row["key"])
            return row["ignore"]

    def flashable(self, ports):
        """The listed, not-ignored ports among `ports` -- the only ones flashing may touch."""
        with self.lock:
            return [p for p in ports if p in self.rows and not self.rows[p]["ignore"]]

    def ignored_ports(self):
        with self.lock:
            return {p for p, r in self.rows.items() if r["ignore"]}

    def note(self, port, status=None, info=None):
        with self.lock:
            row = self.rows.get(port)
            if row:
                if status is not None:
                    row["status"] = status
                if info is not None:
                    row["info"] = info

    def snapshot(self):
        with self.lock:
            return [(p, dict(r)) for p, r in sorted(self.rows.items())]


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


def _chip_in(out):
    low = out.lower()
    for key in ("esp32-c3", "esp32-c6", "esp32-c2", "esp32-s3"):
        if key in low:
            return key.replace("-", "")
    return None


def _mac_in(out):
    """The board's MAC from esptool's output: its BASE MAC where a chip also prints
    a longer EUI-64 (the lookahead skips that line rather than cutting it short)."""
    m = re.findall(r"MAC:\s*((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})(?![:-][0-9a-fA-F])", out)
    return m[0].replace("-", ":").lower() if m else None


def detect_chip(port):
    rc, out = run_esptool(["--port", port, "flash_id"], timeout=40)
    return _chip_in(out), out


def read_mac(port):
    rc, out = run_esptool(["--port", port, "read_mac"], timeout=60)
    m = re.findall(r"MAC:\s*((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})", out)
    return (m[0].replace("-", ":").lower() if rc == 0 and m else None), out


# ---- what is already on the board --------------------------------------------
# Ropener (VAL3100), Generic board (VAL3100) and Glasscalibur are all ESP32-C6, so
# chip detection cannot tell their boards apart. Every ESP-IDF app carries its
# project name in its app descriptor ("ropener", "glasscalibur", "val3100", ...),
# so the name read off the board, set against the names in the images cached
# here, says when a board is about to get another product's firmware.
PART_TABLE_OFFSET = 0x8000
APP_DESC_MAGIC = 0xABCD5432
BOARD_READ_SPAN = 0x10000 + 0x100 - PART_TABLE_OFFSET   # partition table .. a 0x10000 app's descriptor


def _app_offsets(table):
    """Offsets of the app partitions in a partition table, lowest first."""
    out = []
    for i in range(0, len(table) - 31, 32):
        e = table[i:i + 32]
        if e[:2] != b"\xaa\x50":
            break
        if e[2] == 0:
            out.append(int.from_bytes(e[4:8], "little"))
    return sorted(out)


def _project_name(head):
    """The project name in the app descriptor at the start of an app image
    (24-byte image header + 8-byte segment header, then the descriptor), or None."""
    if len(head) < 112 or head[0] != 0xE9 or int.from_bytes(head[32:36], "little") != APP_DESC_MAGIC:
        return None
    return head[80:112].split(b"\0")[0].decode("ascii", "replace").strip() or None


def image_project(path):
    """The project name inside a factory image on disk, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(PART_TABLE_OFFSET)
            apps = _app_offsets(f.read(0xC00))
            if not apps:
                return None
            f.seek(apps[0])
            return _project_name(f.read(0x100))
    except OSError:
        return None


def inspect_board(port):
    """(chip, project, mac, raw) for the board on `port`, from one esptool session
    that reads the partition table and the start of the first app. project is
    None for a blank board, or one whose firmware carries no app descriptor."""
    with tempfile.TemporaryDirectory() as td:
        head_path = os.path.join(td, "head.bin")

        def read(off, size):
            rc, out = run_esptool(["--port", port, "--baud", FLASH_BAUD, "read_flash",
                                   hex(off), hex(size), head_path], timeout=60)
            data = None
            if rc == 0 and os.path.exists(head_path):
                with open(head_path, "rb") as f:
                    data = f.read()
            return data, out

        head, raw = read(PART_TABLE_OFFSET, BOARD_READ_SPAN)
        chip, mac, project = _chip_in(raw), _mac_in(raw), None
        apps = _app_offsets(head[:0xC00]) if head else []
        if apps:
            rel = apps[0] - PART_TABLE_OFFSET
            if 0 <= rel and rel + 0x100 <= len(head):
                project = _project_name(head[rel:rel + 0x100])
            else:
                app_head, more = read(apps[0], 0x100)
                raw += more
                project = _project_name(app_head or b"")
    if not chip:
        chip, more = detect_chip(port)
        raw += more
        mac = mac or _mac_in(more)
    return chip, project, mac, raw


def known_firmware(products, chip):
    """{project name: [products]} from every chip-select product's cached image for `chip`."""
    out = {}
    for name, pcfg in products.items():
        if is_variant(pcfg) or chip not in expected_chips(pcfg):
            continue
        proj = image_project(bin_path(name, chip))
        if proj:
            out.setdefault(proj, []).append(name)
    return out


def firmware_mismatch(products, product, chip, found):
    """(other products, one line) when the board's firmware `found` is another
    product's, else None. A name no cached image carries (blank, an older build,
    something else entirely) is not a mismatch: there is nothing to compare it with."""
    owners = known_firmware(products, chip).get(found, []) if found else []
    if not owners or product in owners:
        return None
    other = " / ".join(owners)
    return owners, f"this board is running {other} firmware, but {product} is selected"


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
    refused = release_refusal(vdir, local, bench=False)
    if refused:
        return False, f"REFUSED — {refused}. Nothing was written."
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


def variant_version(product, vname, vcfg):
    vdir, local = variant_source(product, vname, vcfg)
    if local:
        return "local build"
    try:
        with open(os.path.join(vdir, ".release_tag")) as f:
            return f.read().strip()
    except OSError:
        return ""


# ---- the flash record --------------------------------------------------------
# One row for every board the flasher tried: when, which firmware and version, the
# board's MAC, what it was running before, and how it went. Kept next to the
# script, where it is easy to find and opens in any spreadsheet. (Bench mode keeps
# its own record, provisioned.csv.)
FLASH_LOG = os.environ.get("VALAR_FLASHER_FLASH_LOG") or os.path.join(HERE, "flash-log.csv")
FLASH_LOG_FIELDS = ["time", "product", "variant", "version", "chip", "port", "mac", "was", "result", "detail"]
_flash_log_lock = threading.Lock()


def record_flash(**row):
    """Append one row. Returns None, or one line saying why it could not: a record
    that cannot be written never stops the flashing (Excel locks a CSV it has open)."""
    row = {k: " ".join(str(row.get(k) or "").split())[:200] for k in FLASH_LOG_FIELDS}
    row["time"] = row["time"] or time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _flash_log_lock:
            new = not os.path.exists(FLASH_LOG) or os.path.getsize(FLASH_LOG) == 0
            # utf-8-sig: Excel reads the dashes in the details as text, not mojibake.
            with open(FLASH_LOG, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=FLASH_LOG_FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
        return None
    except OSError as e:
        return (f"Couldn't add this board to {os.path.basename(FLASH_LOG)} ({e.strerror or e}) "
                f"-- is it open in Excel?")


def open_file(path):
    if sys.platform.startswith("win"):
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


# ---- sounds ------------------------------------------------------------------
# DONE and FAILED sound different, so a pile can be worked without watching the
# screen. Built-in OS sounds only; nothing to install.
MAC_SOUNDS = {True: "/System/Library/Sounds/Glass.aiff", False: "/System/Library/Sounds/Basso.aiff"}
LINUX_SOUNDS = {True: "/usr/share/sounds/freedesktop/stereo/complete.oga",
                False: "/usr/share/sounds/freedesktop/stereo/dialog-error.oga"}
WIN_TONES = {True: [(880, 120), (1320, 180)], False: [(330, 300), (220, 450)]}   # (Hz, ms)


def play_sound(ok, fallback=None):
    """Never blocks and never raises; `fallback` runs when this OS has no way to play one."""
    try:
        if sys.platform.startswith("win"):
            import winsound

            def beep():
                try:
                    for hz, ms in WIN_TONES[ok]:
                        winsound.Beep(hz, ms)
                except RuntimeError:
                    pass
            threading.Thread(target=beep, daemon=True).start()
            return
        if sys.platform == "darwin":
            cmd = ["afplay", MAC_SOUNDS[ok]]
        else:
            player = shutil.which("paplay") or shutil.which("pw-play")
            cmd = [player, LINUX_SOUNDS[ok]] if player else None
        if cmd and shutil.which(cmd[0]) and os.path.exists(cmd[1]):
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    except Exception:
        pass
    if fallback:
        fallback()


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
        refused = release_refusal(vdir, local, bench=True)
        if refused:
            s.set(key, "FAILED", f"REFUSED — {refused}", mac=mac)
            return "FAILED"
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
    default_product = starting_product(cfg)

    root = tk.Tk()
    root.title("Valar Flasher")
    root.geometry("640x760")
    root.configure(bg="#111418")

    q = queue.Queue()
    # Auto-flash starts OFF on every launch, and every board already plugged in
    # when the window opens is IGNORED (BoardList): never touched unless the
    # operator flips its switch.
    state = {"busy": False, "auto": False, "force": "Auto",
             "product": default_product, "handled": {}, "variant": None,
             "bench": None, "bench_stop": None,
             "sound": load_settings().get("sound", True), "update": None}
    board_list = BoardList()

    def log(m): q.put(("log", m))
    def set_status(m, c): q.put(("status", (m, c)))
    def set_version(t): q.put(("version", t))

    # Shown only once a newer flasher is found on GitHub (see app_update_check).
    update_bar = tk.Frame(root, bg="#3d2f00")
    update_lbl = tk.Label(update_bar, text="A newer version of Valar Flasher is available.",
                          font=("Helvetica", 11, "bold"), bg="#3d2f00", fg="#ffd866",
                          wraplength=470, justify="left")
    update_lbl.pack(side="left", padx=10, pady=6)
    update_btn = tk.Button(update_bar, text="Update app")
    update_btn.pack(side="right", padx=10, pady=6)

    # The chosen firmware (and variant), in LARGE type, for as long as it is
    # chosen: Ropener, Generic board and Glasscalibur boards share a chip, as do
    # all the Blipscope SKUs, so this label is what stands between the operator
    # and a board flashed with the wrong product's image.
    product_banner = tk.Label(root, text="", font=("Helvetica", 22, "bold"),
                              bg="#111418", fg="#58a6ff", pady=4)
    local_banner = tk.Label(root, text="", font=("Helvetica", 11, "bold"), wraplength=600,
                            bg="#5a1d1d", fg="#ffffff", pady=4)
    status = tk.Label(root, text="Auto-flash is OFF", font=("Helvetica", 18, "bold"),
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

    # The auto-flash switch, drawn rather than a Checkbutton: a stock button
    # ignores its colours on macOS, and this has to read ON/OFF at a glance.
    auto_holder = tk.Frame(top, bg="#111418"); auto_holder.pack(side="right")
    auto_lbl = tk.Label(auto_holder, text="", font=("Helvetica", 12, "bold"),
                        bg="#111418", cursor="hand2")
    auto_lbl.pack(side="left", padx=(0, 6))
    auto_sw = tk.Canvas(auto_holder, width=44, height=24, bg="#111418",
                        highlightthickness=0, cursor="hand2")
    auto_sw.pack(side="left")

    def draw_switch(canvas, on, on_color):
        track = on_color if on else "#6e7681"
        canvas.delete("all")
        canvas.create_oval(2, 2, 22, 22, fill=track, outline=track)
        canvas.create_oval(22, 2, 42, 22, fill=track, outline=track)
        canvas.create_rectangle(12, 2, 32, 22, fill=track, outline=track)
        knob = 24 if on else 4
        canvas.create_oval(knob, 4, knob + 16, 20, fill="#ffffff", outline="")

    def draw_auto():
        on = state["auto"]
        draw_switch(auto_sw, on, "#3fb950")
        auto_lbl.config(text=f"Auto-flash: {'ON' if on else 'OFF'}",
                        fg="#3fb950" if on else "#9aa0a6")

    def toggle_auto(_=None):
        state["auto"] = not state["auto"]
        draw_auto()
        if state["auto"]:
            n = sum(1 for _p, r in board_list.snapshot() if r["ignore"] and not r["protected"])
            log("— auto-flash ON: every board that isn't ignored is flashed"
                + (f"; {n} ignored board(s) are left alone — flip a switch to flash one" if n else "") + " —")
        else:
            log("— auto-flash OFF: nothing is flashed until you turn it on or click Flash now —")
        if not state["busy"] and not state["bench"]:
            ignored = sum(1 for _p, r in board_list.snapshot() if r["ignore"] and not r["protected"])
            set_status("Auto-flash is OFF" if not state["auto"] else
                       f"Plug in a board…  ({ignored} ignored — see the list)" if ignored else
                       "Plug in a board…", "#e6e6e6")
        q.put(("boards", None))

    auto_lbl.bind("<Button-1>", toggle_auto)
    auto_sw.bind("<Button-1>", toggle_auto)
    draw_auto()

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

    boards_frame = tk.Frame(root, bg="#161b22", padx=8, pady=6)
    boards_frame.pack(fill="x", padx=12, pady=(8, 0))
    tiles_frame = tk.Frame(root, bg="#111418")
    logbox = scrolledtext.ScrolledText(root, height=10, bg="#0b0d10", fg="#c9d1d9",
                                       insertbackground="#c9d1d9", font=("Menlo", 10),
                                       relief="flat")
    logbox.pack(fill="both", expand=True, padx=12, pady=10)

    controls = tk.Frame(root, bg="#111418")
    controls.pack(fill="x", padx=12, pady=(0, 12))
    force_var = tk.StringVar(value="Auto")
    force_lbl = tk.Label(controls, text="  Force:", bg="#111418", fg="#9aa0a6")
    force_menu = tk.OptionMenu(controls, force_var, "Auto", "VAL3000", "VAL3100")
    sound_var = tk.BooleanVar(value=state["sound"])

    def on_sound():
        state["sound"] = sound_var.get()
        save_settings(sound=state["sound"])
    sound_box = tk.Checkbutton(controls, text="Sound", variable=sound_var, command=on_sound,
                               bg="#111418", fg="#c9d1d9", selectcolor="#111418",
                               activebackground="#111418", activeforeground="#c9d1d9")
    sound_box.pack(side="left", padx=(12, 0))
    force_lbl.pack(side="left", before=sound_box); force_menu.pack(side="left", before=sound_box)

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
        for w in (product_banner, local_banner, reset_box, bench_box, count_entry, tiles_frame):
            w.pack_forget()
        product_banner.pack(fill="x", before=status)
        if variant:
            names = list((pcfg().get("variants") or {}).keys())
            if state["variant"] not in names:
                state["variant"] = starting_variant(state["product"], pcfg())
            variant_var.set(state["variant"])
            tk.OptionMenu(variant_menu_holder, variant_var, *names).pack(side="left", padx=6)
            product_banner.config(text=f"{state['product'].upper()} · {state['variant']}")
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
            product_banner.config(text=state["product"].upper())
            force_lbl.pack(side="left", before=sound_box); force_menu.pack(side="left", before=sound_box)
            bench_var.set(False)

    def ask(title, text):
        """A yes/no question asked on the Tk thread; flashing runs on a worker, which waits."""
        box = {"answered": threading.Event(), "yes": False}
        q.put(("ask", (title, text, box)))
        box["answered"].wait()
        return box["yes"]

    def do_one_flash(port):
        state["busy"] = True
        if port in board_list.ignored_ports():      # the last word, whoever asked
            log(f"[{port}] is ignored in the board list — not touched.")
            state["busy"] = False
            return
        p, v = state["product"], state["variant"]
        rec = {"product": p, "variant": v, "port": port}
        board_list.note(port, status="flashing…")
        q.put(("boards", None))

        def finish(result, status_text, log_text, detail):
            """Every outcome ends here: the status line, the log, the board list,
            the flash record, the sound."""
            ok = result == "DONE"
            if ok:
                counts["n"] += 1; q.put(("count", counts["n"]))
            set_status(status_text, "#3fb950" if ok else "#e8c15a" if result == "SKIPPED" else "#e5534b")
            log(log_text)
            board_list.note(port, status=result,
                            info=" · ".join(x for x in (rec.get("chip"), f"had {rec['was']}" if rec.get("was")
                                                        else None) if x))
            q.put(("boards", None))
            note = record_flash(result=result, detail=detail, **rec)
            if note:
                log(note)
            if state["sound"] and result != "SKIPPED":
                play_sound(ok, fallback=lambda: q.put(("bell", 1 if ok else 2)))
            state["handled"][port] = "ok" if ok else "err"
            state["busy"] = False

        if is_variant(pcfg()):
            set_status(f"Flashing {v}…  (don't unplug)", "#e8c15a")
            log(f"[{port}] → {p} · {v} ({'FACTORY RESET' if reset_var.get() else 'settings kept'})")
            rec.update(version=variant_version(p, v, vcfg()), mac=usb_mac(port))
            t0 = time.time()
            ok, msg = flash_variant(port, p, v, vcfg(), reset_var.get(), log)
            if ok:
                return finish("DONE", f"✓ {v} DONE — unplug & next",
                              f"[{port}] {msg} in {time.time() - t0:.0f}s.\n", msg)
            first = msg.splitlines()[0]
            return finish("REFUSED" if first.startswith(("REFUSED", "PROTECTED")) else "FAILED",
                          "✗ " + first[:60], f"[{port}] {msg}\n", first)
        try:
            prot = protected_reason(port)
        except ProtectedListError as e:
            prot = f"REFUSED -- {e}"
        if prot:
            return finish("REFUSED", "✗ PROTECTED board — refused", f"[{port}] {prot}", prot)
        rec["version"] = cached_tag(p)
        set_status(f"Detecting {os.path.basename(port)}…", "#e8c15a")
        chip, found, mac, raw = inspect_board(port)
        rec.update(chip=CHIP_LABEL.get(chip, chip), mac=mac, was=found)
        if not chip:
            return finish("FAILED", "Couldn't identify the chip",
                          f"[{port}] chip not recognized:\n{raw.strip()[-400:]}", "chip not recognized")
        forced = state["force"]
        chip_eff = ("esp32c3" if forced == "VAL3000" else
                    "esp32c6" if forced == "VAL3100" else chip)
        binp = bin_path(p, chip_eff)
        clabel = CHIP_LABEL.get(chip_eff, chip_eff)
        if chip_eff not in expected_chips(pcfg()):
            return finish("FAILED", f"{p} has no {clabel} firmware",
                          f"[{port}] {p} defines no firmware for {clabel}. Pick another firmware or board.",
                          f"{p} has no {clabel} firmware")
        if not os.path.exists(binp):
            return finish("FAILED", f"Missing {p} {clabel} bin",
                          f"[{port}] need {p}/{chip_eff}.factory.bin — click 'Update firmware'.",
                          f"missing {p}/{chip_eff}.factory.bin")
        if found:
            log(f"[{port}] this board is currently running '{found}' firmware")
        clash = firmware_mismatch(products, p, chip, found)
        if clash:
            owners, line = clash
            other = " / ".join(owners)
            log(f"[{port}] WARNING: {line}.")
            set_status(f"This board has {other} firmware — waiting for you", "#e8c15a")
            if not ask("Different firmware on this board",
                       f"This board is running {other.upper()} firmware.\n\n"
                       f"{p.upper()} is selected, so flashing would replace it with {p}.\n\n"
                       f"Flash it with {p} anyway?"):
                return finish("SKIPPED", f"Skipped — this board has {other} firmware",
                              f"[{port}] not flashed: {line}. Unplug it, or pick {other} and click Flash now.\n",
                              line)
            log(f"[{port}] replacing its {other} firmware with {p}, as confirmed.")
        ok, why, _ = scan_gate(binp, pcfg().get("anchor", DEFAULT_ANCHOR), public_lists(p))
        if not ok:
            return finish("REFUSED", "✗ REFUSED — secret scan",
                          f"[{port}] REFUSED {p}/{chip_eff}.factory.bin — {why}. Nothing was written.", why)
        log(f"[{port}] {clabel} → {p} firmware, flashing …")
        set_status(f"Flashing {p} · {clabel}…  (don't unplug)", "#e8c15a")
        t0 = time.time()
        rc, out = flash(port, chip_eff, binp)
        dt = time.time() - t0
        rec["mac"] = rec["mac"] or _mac_in(out)
        if rc == 0:
            return finish("DONE", f"✓ {p} · {clabel} DONE — unplug & next",
                          f"[{port}] flashed in {dt:.0f}s.\n", f"flashed in {dt:.0f}s")
        return finish("FAILED", "✗ FAILED — reseat & retry", f"[{port}] FAILED:\n{out.strip()[-600:]}\n",
                      (out.strip().splitlines() or ["esptool failed"])[-1])

    def worker():
        while True:
            if not state["busy"] and not state["bench"]:
                present = list_ports()
                # The list first, so nothing can touch a board the list hasn't seen
                # (and, on the first pass, marked ignored as already connected).
                if board_list.update(present, port_details()):
                    q.put(("boards", None))
                # Forget unplugged ports even while auto-flash is off, so a board
                # swapped in on the same port is new when the switch goes on.
                for p in list(state["handled"]):
                    if p not in present:
                        del state["handled"][p]
                if state["auto"]:
                    for p in board_list.flashable(present):
                        if p not in state["handled"]:
                            do_one_flash(p); break
            time.sleep(0.8)

    def flash_board(port):
        if state["busy"] or state["bench"]:
            log("Busy — wait for the current board to finish.")
            return
        threading.Thread(target=do_one_flash, args=(port,), daemon=True).start()

    def manual_flash():
        if state["busy"] or state["bench"]:
            return
        ports = board_list.flashable(list_ports())
        if not ports:
            log("No board to flash — plug one in, or switch off Ignore on the board you want."); return
        if len(ports) > 1:
            log("More than one board can be flashed — click Flash next to the one you want."); return
        flash_board(ports[0])

    def toggle_ignore(port):
        now = board_list.toggle(port)
        if now is None:
            return
        log(f"— {port} ignored: it won't be touched —" if now else
            f"— {port} no longer ignored: it will be flashed"
            + (" now (auto-flash is on)" if state["auto"] else " when auto-flash is on or you click Flash")
            + " —")
        render_boards()

    def render_boards():
        """One row per connected board: its Ignore switch, what is known of it, its last result."""
        for w in boards_frame.winfo_children():
            w.destroy()
        rows = board_list.snapshot()
        head = tk.Frame(boards_frame, bg="#161b22"); head.pack(fill="x")
        tk.Label(head, text="Connected boards", font=("Helvetica", 11, "bold"),
                 bg="#161b22", fg="#c9d1d9").pack(side="left")
        tk.Label(head, text=("none" if not rows else
                             "already connected when the flasher opened = ignored"),
                 font=("Helvetica", 9), bg="#161b22", fg="#6e7681").pack(side="left", padx=8)
        result_color = {"DONE": "#3fb950", "SKIPPED": "#e8c15a", "flashing…": "#e8c15a"}
        for port, r in rows:
            line = tk.Frame(boards_frame, bg="#161b22"); line.pack(fill="x", pady=1)
            if r["protected"]:
                tk.Label(line, text="PROTECTED", width=11, font=("Helvetica", 9, "bold"),
                         bg="#161b22", fg="#e5534b").pack(side="left", padx=(0, 4))
            else:
                sw = tk.Canvas(line, width=44, height=24, bg="#161b22", highlightthickness=0, cursor="hand2")
                draw_switch(sw, r["ignore"], "#e8c15a")
                sw.bind("<Button-1>", lambda _e, p=port: toggle_ignore(p))
                sw.pack(side="left")
                tk.Label(line, text="Ignore", width=6, anchor="w", font=("Helvetica", 9),
                         bg="#161b22", fg="#e8c15a" if r["ignore"] else "#6e7681").pack(side="left")
            tk.Label(line, text=os.path.basename(port), font=("Helvetica", 10, "bold"),
                     bg="#161b22", fg="#c9d1d9").pack(side="left", padx=(4, 6))
            about = " · ".join(x for x in (r["info"] or r["desc"], r["mac"]) if x)
            tk.Label(line, text=about[:44], font=("Helvetica", 9), bg="#161b22",
                     fg="#9aa0a6").pack(side="left")
            tk.Button(line, text="Flash", state="disabled" if r["ignore"] else "normal",
                      command=lambda p=port: flash_board(p)).pack(side="right")
            if r["ignore"]:
                text, color = "ignored", "#6e7681"
            elif r["status"]:
                text, color = r["status"], result_color.get(r["status"], "#e5534b")
            else:
                text, color = ("will flash" if state["auto"] else "ready to flash"), "#58a6ff"
            tk.Label(line, text=text, font=("Helvetica", 9, "bold"), bg="#161b22",
                     fg=color).pack(side="right", padx=6)

    def sync_selected(force=False):
        def go():
            if is_variant(pcfg()):
                sync_variant(state["product"], state["variant"], vcfg(), log, force=force)
            else:
                sync_firmware(state["product"], pcfg(), log, force=force)
            refresh_version()
        threading.Thread(target=go, daemon=True).start()

    def open_flash_log():
        if not os.path.exists(FLASH_LOG):
            log(f"No boards recorded yet — {os.path.basename(FLASH_LOG)} is created with the first one.")
            return
        try:
            open_file(FLASH_LOG)
        except Exception as e:
            log(f"Couldn't open {FLASH_LOG}: {e}")

    def update_app():
        if state["busy"] or state["bench"]:
            log("Finish flashing (and stop bench mode) before updating the app.")
            return
        update_btn.config(state="disabled", text="Updating…")
        log("Updating Valar Flasher…")

        def go():
            q.put(("updated", app_update_apply(state["update"], log)))
        threading.Thread(target=go, daemon=True).start()
    update_btn.config(command=update_app)

    tk.Button(controls, text="Flash now", command=manual_flash).pack(side="right")
    tk.Button(controls, text="Update firmware", command=lambda: sync_selected(True)).pack(side="right", padx=6)
    tk.Button(controls, text="Flash log", command=open_flash_log).pack(side="right")

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
                targets, lines = bench_targets(board_list.ignored_ports())
                if not targets or not messagebox.askyesno(
                        "Bench mode — confirm boards",
                        "\n".join(lines) + f"\n\nFlash AND provision these {len(targets)} board(s)? "
                        "Everything on them is erased. Boards attached later are NOT included."):
                    bench_var.set(False)
                    log("Bench mode not confirmed. Nothing done.")
                    return
                s.allowed_macs = set(targets.values())
                s.excluded_ports = set(list_ports()) - set(targets)
            s.excluded_ports |= board_list.ignored_ports()
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
        save_settings(product=state["product"])
        log(f"— firmware set to {state['product']}"
            + (f" · {state['variant']}" if state["variant"] else "") + " —")
        sync_selected(False)
    product_var.trace_add("write", on_product)

    def on_variant(*_):
        if variant_var.get() and variant_var.get() != state["variant"]:
            state["variant"] = variant_var.get()
            state["handled"].clear()
            refresh_layout()
            save_settings(variants={state["product"]: state["variant"]})
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
                elif kind == "boards":
                    render_boards()
                elif kind == "ask":
                    from tkinter import messagebox
                    title, text, box = payload
                    box["yes"] = messagebox.askyesno(title, text, icon="warning", default="no", parent=root)
                    box["answered"].set()
                elif kind == "bell":
                    for i in range(payload):
                        root.after(300 * i, root.bell)
                elif kind == "update":
                    state["update"] = payload
                    update_bar.pack(fill="x", before=product_banner)
                elif kind == "updated":
                    ok, msg = payload
                    log(msg)
                    if ok:
                        update_lbl.config(text="Updated. Close and reopen Valar Flasher to use the new version.")
                        update_btn.config(state="normal", text="Close now", command=root.destroy)
                    else:
                        update_lbl.config(text="The update didn't install — see the log. Nothing was changed.")
                        update_btn.config(state="normal", text="Try again")
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
        log("Ready. Plug in a board." if state["auto"] else
            "Ready. Auto-flash is OFF — switch it on (top right) to flash the boards you plug in. "
            "Boards that were already connected when the flasher opened are ignored (see Connected "
            "boards) until you flip their switch.")
        changed = app_update_check(log)
        if changed:
            log(f"A newer version of Valar Flasher is available ({len(changed)} file(s) changed) "
                f"— click Update app at the top.")
            q.put(("update", changed))
    refresh_layout()
    render_boards()
    threading.Thread(target=startup, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    root.after(120, pump)
    if os.environ.get("VALAR_FLASHER_GUI_SMOKE"):
        root.after(int(os.environ["VALAR_FLASHER_GUI_SMOKE"]), root.destroy)
    root.mainloop()


# ===========================================================================
# Console fallback
# ===========================================================================
def bench_targets(ignored=()):
    """({port: mac} bench mode would flash, [lines to show]). Reads USB serial
    numbers only -- no board is opened or reset to build this list. `ignored`:
    ports the window's board list says to leave alone."""
    prot = load_protected()
    targets, lines = {}, ["Bench mode sees these boards:"]
    for port in list_ports():
        m = usb_mac(port)
        if port in ignored:
            lines.append(f"  {port:8} {m or 'MAC unknown':17}  -- EXCLUDED: ignored in the board list")
        elif not m:
            lines.append(f"  {port:8} MAC unknown        -- EXCLUDED (cannot identify it without touching it)")
        elif m in prot:
            lines.append(f"  {port:8} {m}  -- EXCLUDED: PROTECTED ({prot[m]})")
        else:
            targets[port] = m
            lines.append(f"  {port:8} {m}  -- WILL BE FLASHED AND PROVISIONED")
    return targets, lines


def run_console(cfg, product=None, variant=None, bench=False, count=0, factory_reset=False):
    products = cfg["products"]
    # The last firmware picked in the window applies only when the command names
    # none: a command that names a product flashes exactly what it says.
    implicit = product is None
    product = product or starting_product(cfg)
    pcfg = products[product]
    sound = load_settings().get("sound", True)

    def finish(result, line, **rec):
        """Every board ends here: the line, the flash record, the sound."""
        print(line)
        note = record_flash(result=result, **rec)
        if note:
            print(note)
        if sound and result != "SKIPPED":
            play_sound(result == "DONE", fallback=lambda: print("\a", end="", flush=True))

    if is_variant(pcfg):
        names = list((pcfg.get("variants") or {}).keys())
        variant = variant or (starting_variant(product, pcfg) if implicit
                              else pcfg.get("default_variant") or names[0])
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
                    first = msg.splitlines()[0]
                    finish("DONE" if ok else "REFUSED" if first.startswith(("REFUSED", "PROTECTED")) else "FAILED",
                           f"[{p}] {variant}: {'DONE (#%d) — ' % (n + 1) if ok else 'FAILED — '}{msg}\n",
                           product=product, variant=variant, version=variant_version(product, variant, vcfg),
                           port=p, mac=usb_mac(p), detail=first)
                    n += ok
                    handled[p] = 1
                time.sleep(0.8)
        except KeyboardInterrupt:
            print(f"\nSession total: {n} flashed.")
        return 0
    print(f"Valar Flasher (console mode). Firmware source: {product}")
    sync_firmware(product, pcfg, print)
    print("Plug in boards one at a time. Ctrl-C to quit.\n")

    def flash_one(p, rec):
        """(result, line, detail) for one board, filling `rec` as it learns."""
        try:
            prot = protected_reason(p)
        except ProtectedListError as e:
            prot = f"REFUSED -- {e}"
        if prot:
            return "REFUSED", f"[{p}] {prot}", prot
        chip, found, mac, raw = inspect_board(p)
        rec.update(chip=CHIP_LABEL.get(chip, chip), mac=mac, was=found)
        if not chip:
            return "FAILED", f"[{p}] chip not recognized", "chip not recognized"
        binp = bin_path(product, chip)
        if not os.path.exists(binp):
            return "FAILED", f"[{p}] {product}: missing {chip}.factory.bin", f"missing {chip}.factory.bin"
        clash = firmware_mismatch(products, product, chip, found)
        if clash:
            owners, line = clash
            print(f"[{p}] WARNING: {line}.")
            if input(f"Replace it with {product}? Type yes to flash it: ").strip().lower() != "yes":
                return "SKIPPED", f"[{p}] not flashed: {line}.\n", line
        ok, why, _ = scan_gate(binp, pcfg.get("anchor", DEFAULT_ANCHOR), public_lists(product))
        if not ok:
            return "REFUSED", f"[{p}] REFUSED {chip}.factory.bin — {why}. Nothing was written.", why
        print(f"[{p}] {CHIP_LABEL.get(chip, chip)} -> {product}: flashing …")
        rc, out = flash(p, chip, binp)
        rec["mac"] = rec["mac"] or _mac_in(out)
        if rc == 0:
            return "DONE", None, "flashed"
        return "FAILED", f"[{p}] FAILED:\n{out.strip()[-500:]}\n", (out.strip().splitlines() or ["esptool failed"])[-1]

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
                rec = {"product": product, "version": cached_tag(product), "port": p}
                result, line, detail = flash_one(p, rec)
                if result == "DONE":
                    n += 1
                    line = f"[{p}] DONE (#{n}) — unplug & next\n"
                finish(result, line, detail=detail, **rec)
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
    ap.add_argument("--allow-prerelease", action="store_true",
                    help="accept a PRERELEASE image set (a release under test); refused otherwise")
    a = ap.parse_args()
    if a.allow_prerelease:
        ALLOW_PRERELEASE = True
        print("[valar-flasher] --allow-prerelease: a PRERELEASE image set will be accepted and written")
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
