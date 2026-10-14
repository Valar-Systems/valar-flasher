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
| **Blipscope** | `Valar-Systems/valar-scopes` | ESP32-S3 — pick the **variant** (see below) |

All four are defined in **`products.json`** next to the script. Adding a new
chip-select product is a one-line edit — no code change:

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

**Auto-flash starts OFF every time the window opens.** Pick the firmware in the
dropdown, click the **Auto-flash** switch (top right) to turn it on, then work the
pile: plug a board in → it auto-detects and flashes → **green DONE** → unplug →
next. Click the switch again to turn it off. A **red** line means reseat the cable
and replug that one.

- **The firmware you picked is shown in large type** at the top, and the window
  opens on the firmware you used last time.
- **Sounds:** one sound when a board is DONE, a different one when it FAILS, so
  you can work a pile without watching the screen. Untick **Sound** to mute it.
- **Flash log:** every board is added to `flash-log.csv` next to the script —
  date and time, firmware and version, the board's MAC, what firmware it had
  before, and whether it worked. Click **Flash log** to open it in your
  spreadsheet app. (If it's open in Excel while you flash, that board can't be
  added; the window says so and flashing carries on.)

## Other boards connected? They're left alone

The **Connected boards** list shows every board plugged in, each with an
**Ignore** switch. **Any board connected while auto-flash is off is ignored
automatically** — whatever was plugged in when you opened the window, or before
you switched auto-flash on. An ignored board is never touched: not flashed, not
even reset to read its chip. Boards you plug in **after** turning auto-flash on
are flashed.

So with two Blipscopes on the computer and a Glasscalibur to flash: open the
window (both Blipscopes show *ignored*), turn auto-flash on, plug in the
Glasscalibur — only it is flashed.

- Want an ignored board flashed after all? Flip its switch off; with auto-flash
  on it's flashed straight away, or click the **Flash** button on its row.
- An ignored board stays ignored if you unplug it and plug it back in (it's
  recognised by its serial number). A board that reports no serial number is
  recognised only by its port.
- **Flash now** flashes the one board that isn't ignored; with several, use the
  **Flash** button on the row you want.
- Bench mode leaves ignored boards out too.

## Wrong-firmware check

Ropener (VAL3100), Generic board (VAL3100) and Glasscalibur all use the **same
ESP32-C6**, so the chip alone can't tell their boards apart. Before writing, the
flasher reads the name of the firmware already on the board. If it is **another
product's** firmware — say a Glasscalibur board while Ropener is selected — it
stops and asks before replacing it (the default answer is **No**). Blank boards,
boards already running the selected firmware, and firmware it doesn't recognise
(an old build, say) flash without asking. It recognises the firmware of every
product this computer has downloaded.

## The flasher updates itself

When the window opens it also checks GitHub for a newer version of the flasher.
If there is one, a bar at the top says so: click **Update app**, then close and
reopen the window. Your own files — `products.local.json`, `github_token.txt`,
`provisioned.csv`, `flash-log.csv` and the downloaded firmware — are never
touched, and every file it replaces is kept in `.update-backup/`. (A folder that
is a git checkout is left to `git pull`.)

An update replaces `products.json` with the one on GitHub. To add a product on
just your computer, put it in `products.local.json` instead (same format, under
`"products"`) — it is merged over `products.json` and updates never touch it.

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

## Blipscope: pick the variant, keep the customer's settings

Every Blipscope SKU is the **same ESP32-S3**, so chip auto-detect cannot tell a
Kit S3 1.28" from any other SKU — flashing the wrong one looks like a dead screen.
So Blipscope has a second dropdown, **Variant**, and the chosen variant is shown
in **large type** at the top the whole time. Check it before every batch.

Each Blipscope release publishes, per variant, a factory image **and a flash
manifest** listing every region (bootloader, partition table, boot_app0, app)
with its offset, and marking the NVS region — Wi-Fi, location, the device key —
as *preserve*. So by default the flasher **updates a board without erasing its
settings**: it writes the regions and never the NVS span. Tick **Factory reset
(erase settings)** to write the whole factory image instead.

The files are checked against the manifest's sizes and SHA-256s on download and
again before every write, and the regions must reproduce the factory image
exactly; anything that does not is refused.

### Every image is scanned before it is written

Before **every** write — any product, CI-downloaded or local — the image is
scanned for secrets (`scan_image.py`, the same file Blipscope's CI runs; vendored
byte-for-byte and checked against upstream in CI). A hit refuses the write and
says why; nothing is written. Key-shaped strings that frameworks carry as public
data are allowed only when they trace to a public input: for Blipscope, the scan
report CI published beside the image; for Ropener, Glasscalibur and Generic
board, `known_public_runs.json` (mbedTLS constants and framework strings traced
to Espressif's packages). If a future release of those is refused for an
UNTRACED run, trace it on a machine with PlatformIO:
`python tools/trace_public_runs.py <image>` — it adds only runs it finds in the
framework, never "whatever the refused image contained".

**Why the default is the CI image, not a local build.** A factory image built on
your own machine can carry a `-DCLOUD_FEED_KEY=...` build flag, and that key would
then be in **every board you flash**. CI never sets the flag. So customer boards
are flashed from the release. A local build can still be flashed (set
`local_build` in `products.local.json` to a directory written by Blipscope's
`scripts/flash_manifest.py --out`); it is labelled **LOCAL BUILD** with that
warning everywhere it is used, and it is scanned like everything else.

## Bench mode: provisioning a batch

For the one machine that provisions new boards. Copy
`products.local.example.json` to `products.local.json` (gitignored) and point
`provisioner.command` at Blipscope's `scripts/provision_one.py`. **Bench mode**
then appears.

**Keys are minted by the Worker**, not derived on the bench: nobody holds
`DEVICE_KEY_SECRET`, and nothing here asks for it. What the bench holds is a
**provisioning token**. Put it once, from the password manager, in
`~/.config/valar-flasher/provision-token`
(`%USERPROFILE%\.config\valar-flasher\provision-token` on Windows). Bench mode
refuses to start without it. The flasher only checks that the file exists and hands
its path to the provisioner; the token is never shown in the window or printed.

Load a powered hub. Each board gets a tile, keyed by its MAC:
**waiting → flashing → provisioning → verifying → DONE**, or **FAILED (reason)**,
or **SKIPPED** (MAC already in `provisioned.csv`). **DONE means verified**: the key
was written, proven on the board, and accepted by the backend, and the board's row
is in `provisioned.csv`. The header counts *N of --count this session*, and every
failure is listed again at the end. Console form:

```
python valar_flasher.py --product Blipscope --bench --count 50 --ports COM18
```

**Other boards attached?** Every Valar board shows up with the same Espressif USB
ID, so three guards stand between bench mode and the wrong board:

- **Protected boards.** List them in `~/.config/valar-flasher/protected-macs.json`
  (never in this repo — it names your bench's hardware):
  `{"macs": {"90:70:69:32:6e:64": "COM6 -- configured bench unit"}}`.
  A protected board is refused on any port, in any mode: first by the USB serial
  number the OS already holds, so it is not even reset, then again by the MAC
  esptool reads, before any write. Its tile goes red. A list that exists but
  cannot be read refuses bench mode outright.
- **No `--ports`: confirm the list.** Bench mode prints every board it sees —
  port, MAC, and which are EXCLUDED as protected — and flashes nothing until you
  type `yes`. It then flashes only those MACs; a board plugged in afterwards is
  refused, not flashed unlisted. Re-run to add it.
- **`--ports COM18`** (comma-separate several): only those ports are ever seen.
- **A tagged release only.** Bench mode writes an image set synced from a
  tagged release and nothing else. A **prerelease** is refused at download
  **and** at write unless you pass `--allow-prerelease`. A **local build** is
  refused even with that flag. So is a cache synced before these checks
  existed: Update firmware first. The manifest's app region must be the
  release's own `firmware-<slug>.bin`, the binary OTA ships.

## Under the hood

Standard `esptool`. Chip-select products: `write_flash 0x0 <factory.bin>` — the
factory bin contains the bootloader, partition table, and app, so one write at
`0x0` is a complete flash. Blipscope: one `write_flash` with the manifest's
regions at their offsets (or the factory image at `0x0` for a factory reset).
Nothing custom, nothing that can brick a board a normal esptool flash wouldn't.

Tests (no board, no network): `python -m unittest discover -s tests -v`.
