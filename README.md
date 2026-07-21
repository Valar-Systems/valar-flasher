# Valar Flasher

Double-click, pick which firmware, plug in a board — it flashes. It reads each
board's chip and installs the matching binary for the firmware you selected. You
never type anything; a big green **DONE** shows when each board finishes, then you
unplug and plug in the next one.

## Selectable firmware

A **Firmware** dropdown at the top picks the source. Out of the box:

| Firmware | Repo it pulls from | Chips it covers |
|---|---|---|
| **Ropener** | `Valar-Systems/Ropener` | ESP32-C3 (VAL3000), ESP32-C6 (VAL3100) |
| **Glasscalibur** | `Valar-Systems/Glasscalibur` | ESP32-C6 |
| **Generic board** | `Valar-Systems/valar-motion` | ESP32-C3 (VAL3000), ESP32-C6 (VAL3100) |

All three are defined in **`products.json`** next to the script. Adding a new
product is a one-line edit — no code change:

```json
"MyProduct": { "repo": "Valar-Systems/MyProduct", "assets": { "esp32c6": "myproduct" } }
```

`assets` maps a **chip** to a substring found in that release's factory-bin
filename (e.g. `esp32c6: "val3100"` matches `Ropener-VAL3100.factory.bin`).

## Firmware comes from GitHub automatically

When you launch (or switch firmware) it checks that product's
`releases/latest` and downloads the factory bins if there's a newer release than
cached. The window shows the loaded version (e.g. `Ropener: v2.6.4`). Click
**Update firmware** to force a re-check.

- **Offline?** It uses the last-downloaded bins — flashing is never blocked.
- **Manual override:** drop a bin at `firmware/<Product>/<chip>.factory.bin`.
- **Private repo?** Set a `GITHUB_TOKEN` env var, or put a token in
  `github_token.txt` next to the script.

## One-time setup

You need **Python 3** ([python.org](https://www.python.org/downloads/) — the
installer includes everything).

- **Windows:** tick *"Add Python to PATH"* in the installer.
- **Linux:** also `sudo apt install python3-tk python3-venv`.

Then run the setup launcher for your OS once (installs the flashing engine into a
local `.venv`, nothing system-wide) — or just run the flash launcher, which
self-installs on first run:

- **Windows:** `Setup (Windows).bat`
- **macOS:** `Setup (Mac).command`
- **Linux:** `./valar-flasher-linux.sh` (self-installs)

## Flash boards

Double-click the launcher for your OS:

- **Windows:** `Valar Flasher (Windows).bat`
- **macOS:** `Valar Flasher (Mac).command`
- **Linux:** `ValarFlasher.desktop` (or `./valar-flasher-linux.sh`)

Pick the firmware in the dropdown, then work the pile: plug a board in → it
auto-detects and flashes → **green DONE** → unplug → next. Leave *Auto-flash on
plug-in* ticked for hands-off flashing, or untick it and press **Flash now** per
board. A **red** line means reseat the cable and replug that one.

## VAL3101 shares the VAL3100's chip

The VAL3101 uses the **same ESP32-C6** as the VAL3100, so chip auto-detect can't
tell them apart — a C6 board gets the C6 firmware of the selected product. Use the
**Force** menu to override, or add a dedicated product entry in `products.json`.

## Troubleshooting

- **"esptool is not installed"** → run the Setup launcher first.
- **Board never detected** → almost always a charge-only USB-C cable; use a data
  cable.
- **macOS "unidentified developer"** → right-click the `.command` → Open → Open
  (first time only).
- **Linux permission denied on the port** → `sudo usermod -aG dialout $USER`,
  then log out/in.

## Under the hood

Standard `esptool` with `write_flash 0x0 <factory.bin>`. The factory bin contains
the bootloader, partition table, and app, so one write at `0x0` is a complete
flash — nothing custom, nothing that can brick a board a normal esptool flash
wouldn't.
