#!/usr/bin/env python3
"""
Valar Flasher — plug in a VAL board, pick which firmware to install, and it
flashes the right binary for that board's chip. No typing.

Firmware source is SELECTABLE (dropdown) and defined in products.json next to
this script — add a new product by editing that file, no code change:

    {
      "default": "Ropener",
      "products": {
        "Ropener":       {"repo": "Valar-Systems/Ropener",     "assets": {"esp32c3": "val3000", "esp32c6": "val3100"}},
        "Glasscalibur":  {"repo": "Valar-Systems/Glasscalibur","assets": {"esp32c6": "glasscalibur"}},
        "Generic board": {"repo": "Valar-Systems/valar-motion", "assets": {"esp32c3": "val3000", "esp32c6": "val3100"}}
      }
    }

For the selected product it checks that repo's latest GitHub release on launch and
downloads the matching factory bins (offline? it uses whatever is cached). Then:

  detected chip -> the asset whose name matches assets[chip] -> write_flash 0x0

NOTE: VAL3101 shares the VAL3100's chip (ESP32-C6), so a C6 board gets the C6
firmware for the selected product. Use the Force menu to override.

Runs a small Tk window; falls back to a console loop if Tk is unavailable.
Requires esptool (the launcher installs it for you).
"""
import os, sys, json, subprocess, threading, queue, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FW_ROOT = os.path.join(HERE, "firmware")
PRODUCTS_FILE = os.path.join(HERE, "products.json")
PY = sys.executable
FLASH_BAUD = "921600"

CHIP_LABEL = {"esp32c3": "ESP32-C3", "esp32c6": "ESP32-C6",
              "esp32c2": "ESP32-C2", "esp32s3": "ESP32-S3"}

DEFAULT_PRODUCTS = {
    "default": "Ropener",
    "products": {
        "Ropener":       {"repo": "Valar-Systems/Ropener",      "assets": {"esp32c3": "val3000", "esp32c6": "val3100"}},
        "Glasscalibur":  {"repo": "Valar-Systems/Glasscalibur", "assets": {"esp32c6": "glasscalibur"}},
        "Generic board": {"repo": "Valar-Systems/valar-motion", "assets": {"esp32c3": "val3000", "esp32c6": "val3100"}},
    },
}


def load_products():
    """Read products.json; create it from the default if missing."""
    if not os.path.exists(PRODUCTS_FILE):
        try:
            with open(PRODUCTS_FILE, "w") as f:
                json.dump(DEFAULT_PRODUCTS, f, indent=2)
        except Exception:
            pass
        return DEFAULT_PRODUCTS
    try:
        with open(PRODUCTS_FILE) as f:
            cfg = json.load(f)
        if cfg.get("products"):
            return cfg
    except Exception:
        pass
    return DEFAULT_PRODUCTS


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


# ---- serial + esptool ------------------------------------------------------
def list_ports():
    try:
        from serial.tools import list_ports as lp
    except Exception:
        return []
    esp, other = [], []
    for p in lp.comports():
        vid = getattr(p, "vid", None)
        if vid in (0x303A, 0x10C4, 0x1A86, 0x0403):
            esp.append(p.device)
        elif p.device:
            other.append(p.device)
    return esp if esp else other


def run_esptool(args, timeout=180):
    try:
        r = subprocess.run([PY, "-m", "esptool"] + args, capture_output=True, text=True, timeout=timeout)
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


def flash(port, chip, binpath):
    return run_esptool([
        "--chip", chip, "--port", port, "--baud", FLASH_BAUD,
        "--before", "default_reset", "--after", "hard_reset",
        "write_flash", "0x0", binpath,
    ], timeout=240)


# ===========================================================================
# GUI
# ===========================================================================
def run_gui(cfg):
    import tkinter as tk
    from tkinter import scrolledtext

    products = cfg["products"]
    default_product = cfg.get("default") or next(iter(products))

    root = tk.Tk()
    root.title("Valar Flasher")
    root.geometry("580x500")
    root.configure(bg="#111418")

    q = queue.Queue()
    state = {"busy": False, "auto": True, "force": "Auto",
             "product": default_product, "handled": {}}

    def log(m): q.put(("log", m))
    def set_status(m, c): q.put(("status", (m, c)))
    def set_version(t): q.put(("version", t))

    status = tk.Label(root, text="Plug in a board…", font=("Helvetica", 18, "bold"),
                      bg="#111418", fg="#e6e6e6", pady=10)
    status.pack(fill="x")
    counter = tk.Label(root, text="Flashed this session: 0",
                       font=("Helvetica", 11), bg="#111418", fg="#9aa0a6")
    counter.pack()
    version = tk.Label(root, text="Firmware: checking…",
                       font=("Helvetica", 10), bg="#111418", fg="#6e7681")
    version.pack()

    # product selector
    top = tk.Frame(root, bg="#111418"); top.pack(fill="x", padx=12, pady=(8, 0))
    tk.Label(top, text="Firmware:", bg="#111418", fg="#c9d1d9",
             font=("Helvetica", 12, "bold")).pack(side="left")
    product_var = tk.StringVar(value=default_product)
    tk.OptionMenu(top, product_var, *products.keys()).pack(side="left", padx=6)

    logbox = scrolledtext.ScrolledText(root, height=12, bg="#0b0d10", fg="#c9d1d9",
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
    tk.Label(controls, text="  Force:", bg="#111418", fg="#9aa0a6").pack(side="left")
    tk.OptionMenu(controls, force_var, "Auto", "VAL3000", "VAL3100").pack(side="left")

    counts = {"n": 0}

    def pcfg():
        return products[state["product"]]

    def refresh_version():
        p = state["product"]
        set_version(f"{p}: {cached_tag(p) or 'none yet'}"
                    f"{'' if have_bins(p, pcfg()) else '  (incomplete — Update)'}")

    def do_one_flash(port):
        state["busy"] = True
        p = state["product"]
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
            if state["auto"] and not state["busy"]:
                present = set(list_ports())
                for p in list(state["handled"]):
                    if p not in present:
                        del state["handled"][p]
                for p in present:
                    if p not in state["handled"]:
                        do_one_flash(p); break
            time.sleep(0.8)

    def manual_flash():
        if state["busy"]:
            return
        ports = list_ports()
        if not ports:
            log("No board detected. Plug one in."); return
        threading.Thread(target=do_one_flash, args=(ports[0],), daemon=True).start()

    def sync_selected(force=False):
        def go():
            sync_firmware(state["product"], pcfg(), log, force=force); refresh_version()
        threading.Thread(target=go, daemon=True).start()

    tk.Button(controls, text="Flash now", command=manual_flash).pack(side="right")
    tk.Button(controls, text="Update firmware", command=lambda: sync_selected(True)).pack(side="right", padx=6)

    def on_product(*_):
        state["product"] = product_var.get()
        state["handled"].clear()
        log(f"— firmware set to {state['product']} —")
        sync_selected(False)
    product_var.trace_add("write", on_product)

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
        except queue.Empty:
            pass
        root.after(120, pump)

    def startup():
        log(f"Firmware source: {state['product']}. Checking GitHub for the latest release…")
        sync_firmware(state["product"], pcfg(), log)
        refresh_version()
        log("Ready. Plug in a board.")
    threading.Thread(target=startup, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    root.after(120, pump)
    root.mainloop()


# ===========================================================================
# Console fallback
# ===========================================================================
def run_console(cfg):
    products = cfg["products"]
    product = cfg.get("default") or next(iter(products))
    print(f"Valar Flasher (console mode). Firmware source: {product}")
    sync_firmware(product, products[product], print)
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
                chip, raw = detect_chip(p)
                if not chip:
                    print(f"[{p}] chip not recognized"); handled[p] = 1; continue
                binp = bin_path(product, chip)
                if not os.path.exists(binp):
                    print(f"[{p}] {product}: missing {chip}.factory.bin"); handled[p] = 1; continue
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


if __name__ == "__main__":
    cfg = load_products()
    try:
        import tkinter  # noqa
        run_gui(cfg)
    except Exception:
        run_console(cfg)
