#!/usr/bin/env python3
"""scan_image.py -- refuse a firmware image that carries a secret.

WHY THIS EXISTS. The shipping image must hold no credential: device keys live in
each board's NVS, never in the app. But platformio.ini invites a local
`-DCLOUD_FEED_KEY=...` build flag, and a factory image built on a machine that
sets it carries that key into EVERY board flashed from it. CI never sets the flag,
which is why the release publishes the factory image -- and why this scan runs on
every image CI publishes, and again in valar-flasher before every write.

ONE IMPLEMENTATION. valar-flasher vendors this file byte-for-byte and its CI diffs
the copy against this one; a second, hand-ported scanner is the one that drifts.
Stdlib only, Python 3.8+.

WHAT IT PRINTS: terms, counts, character classes and offsets. NEVER a matched
string -- a scanner that echoes what it found leaks the secret into the very log
that reports it.

A hit on sight, whatever else is known:
  * the NAMES DEVICE_KEY_SECRET and CLOUD_FEED_KEY
  * a GitHub token prefix, or "Bearer <token>"
A hit UNLESS TRACED to a public input of the build:
  * a 64-hex run -- the shape of a device key or HMAC secret
  * a 40-char token-shaped run [A-Za-z0-9_-]{40} -- a Cloudflare API token's shape
Frameworks carry both shapes as public data: the C3/C6 Arduino images hold six
64-hex mbedTLS constants from Espressif's prebuilt libmbedcrypto.a, and every
image holds framework strings of 40 chars. So each such run is TRACED -- found
verbatim in a public build input (--trace), or its sha256 listed in a report CI
published for this release or in a checked-in list (--public-runs) -- and only an
untraced one is a hit. A key baked in by a build flag is in no public input.
With neither --trace nor --public-runs, every 64-hex run is a hit (fail closed)
and 40-char runs are counted but not judged.

CONTROLS, run on every scan, before any result is believed:
  1. anchor   -- a string known to be in this image must be FOUND. "Zero hits"
                 from a scanner that cannot see the image's strings is not evidence.
  2. planted  -- a copy with a random 64-hex key appended must be FLAGGED.
  3. untraced -- (when a trace or list is given) a random 40-char run must come
                 back UNTRACED, or the trace is accepting everything and neither
                 traced rule protects anything.
A failed control is exit 3: the scan is untrustworthy, which is not the same as clean.

    scan_image.py IMAGE --anchor S --trace DIR [--trace DIR ...] [--emit REPORT.json]   (CI)
    scan_image.py IMAGE --anchor S --public-runs REPORT.json [--public-runs LIST.json]  (flasher)
    scan_image.py --selftest

Exit: 0 clean, 1 hit, 3 a control failed (untrustworthy), 2 usage.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys

MIN_STRING = 6
ON_SIGHT = [
    ("name DEVICE_KEY_SECRET", re.compile(rb"DEVICE_KEY_SECRET")),
    ("name CLOUD_FEED_KEY", re.compile(rb"CLOUD_FEED_KEY")),
    ("GitHub token prefix", re.compile(rb"(?:ghp_|gho_|ghu_|ghs_|github_pat_)[A-Za-z0-9_]{20,}")),
    ("Bearer token", re.compile(rb"Bearer\s+[A-Za-z0-9._-]{20,}")),
]
HEX64 = "64-hex run"
RUN40 = "40-char token-shaped run"
TRACED = [
    (HEX64, re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")),
    (RUN40, re.compile(rb"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{40}(?![A-Za-z0-9_-])")),
]
TRACE_EXT = re.compile(r"\.(c|cc|cpp|h|hpp|ino|s|ld|txt|csv|json|a|in|py|mk|cmake|inc|pem|crt)$", re.I)
TRACE_SKIP_DIRS = re.compile(r"^(\.git|docs?|examples?|tests?)$", re.I)


def strings(data: bytes):
    """(offset, bytes) for every printable-ASCII run of MIN_STRING or more."""
    for m in re.finditer(rb"[\x20-\x7e]{%d,}" % MIN_STRING, data):
        yield m.start(), m.group(0)


def klass(s: bytes) -> str:
    """A coarse, non-revealing description of a match: which alphabets it uses."""
    return "".join(c for c, ok in (("a", re.search(rb"[a-z]", s)), ("A", re.search(rb"[A-Z]", s)),
                                   ("9", re.search(rb"[0-9]", s)), ("_", re.search(rb"[_-]", s))) if ok)


def sha(s: bytes) -> str:
    return hashlib.sha256(s).hexdigest()


def find(data: bytes):
    """{label: [(offset, match_bytes)]} for every term."""
    out = {label: [] for label, _ in ON_SIGHT + TRACED}
    for off, s in strings(data):
        for label, rx in ON_SIGHT + TRACED:
            out[label] += [(off + m.start(), m.group(0)) for m in rx.finditer(s)]
    return out


def trace(runs: set, roots: list) -> dict:
    """{run: root it was found under}. A run found in no root is absent."""
    found, want = {}, set(runs)
    for root in roots:
        if not want:
            break
        if os.path.isfile(root):
            files = [root]
        else:
            files = []
            for d, subdirs, names in os.walk(root):
                subdirs[:] = [x for x in subdirs if not TRACE_SKIP_DIRS.match(x)]
                files += [os.path.join(d, n) for n in names if TRACE_EXT.search(n)]
        for p in files:
            if not want:
                break
            try:
                if os.path.getsize(p) > 64 * 1024 * 1024:
                    continue
                with open(p, "rb") as f:
                    blob = f.read()
            except OSError:
                continue
            for r in [r for r in want if r in blob]:
                found[r] = root
                want.discard(r)
    return found


class Verdict:
    def __init__(self):
        self.lines, self.hits, self.control_failed, self.report = [], 0, False, {}

    def say(self, s):
        self.lines.append(s)


def scan(data: bytes, anchor: str, trace_roots=None, public_runs=None, name="image") -> Verdict:
    v = Verdict()
    have_source = trace_roots is not None or public_runs is not None
    real = find(data)
    shaped = {label: {m for _, m in real[label]} for label, _ in TRACED}
    fake_hex = secrets.token_hex(32).encode()
    fake_run = secrets.token_urlsafe(30)[:40].encode()
    # ONE walk of the trace roots for every run AND both controls' plants: the
    # package tree is large, and walking it per question tripled the cost.
    where = (trace(set().union(*shaped.values()) | {fake_hex, fake_run}, trace_roots)
             if trace_roots is not None else {})

    def untraced(rs: set) -> set:
        if trace_roots is not None:
            return {r for r in rs if r not in where}
        if public_runs is not None:
            return {r for r in rs if sha(r) not in public_runs}
        return set(rs)

    v.say(f"image: {name} ({len(data)} bytes)")
    # CONTROL 1: the anchor.
    ok = anchor.encode() in data
    v.say(f"CONTROL anchor {anchor!r} found: {ok}")
    v.control_failed |= not ok
    # CONTROL 2: a random 64-hex key, appended to a copy, must be flagged.
    planted = {m for _, m in find(data + b"\0" + fake_hex + b"\0")[HEX64]}
    ok = fake_hex in planted and fake_hex in untraced(planted)
    v.say(f"CONTROL planted 64-hex key flagged: {ok}")
    v.control_failed |= not ok
    # CONTROL 3: a random 40-char run must come back untraced.
    if have_source:
        ok = fake_run in untraced({fake_run})
        v.say(f"CONTROL planted 40-char run comes back untraced: {ok}")
        v.control_failed |= not ok

    for label, _ in ON_SIGHT:
        hits = real[label]
        v.hits += len(hits)
        v.say(f"{len(hits):5}  {label}" + (f"   HIT at {', '.join(hex(o) for o, _ in hits[:5])}" if hits else ""))

    public = set()
    for label, _ in TRACED:
        runs = shaped[label]
        if label == RUN40 and not have_source:
            v.say(f"{len(runs):5}  {label} (distinct; not judged -- no trace and no public list)")
            continue
        bad = untraced(runs)
        public |= runs - bad
        v.hits += len(bad)
        detail = ""
        if bad:
            offs = [hex(o) for o, m in real[label] if m in bad][:5]
            detail = f"   UNTRACED {len(bad)}: classes {sorted(klass(m) for m in bad)} at {', '.join(offs)}"
        v.say(f"{len(runs):5}  {label} (distinct), {len(runs) - len(bad)} traced to public inputs{detail}")
        if trace_roots is not None:
            for root in trace_roots:
                n = sum(1 for r, w in where.items() if w == root and r in runs)
                if n:
                    v.say(f"         {n} found verbatim under {root}")
    if have_source:
        v.report = {"v": 1, "image_sha256": hashlib.sha256(data).hexdigest(), "anchor": anchor,
                    "public_runs": sorted(sha(r) for r in public)}

    v.say("VERDICT: " + ("UNTRUSTWORTHY -- a control failed; nothing above is evidence" if v.control_failed
                         else f"HIT -- {v.hits} finding(s); this image must not be published or flashed" if v.hits
                         else "CLEAN"))
    return v


def exit_code(v: Verdict) -> int:
    return 3 if v.control_failed else 1 if v.hits else 0


def selftest() -> int:
    """Each rule must be able to fire, and each trace must be able to excuse and
    to refuse. Plants never print their content."""
    anchor = "scopes.valarsystems.com"
    pub_hex, pub_run = b"c0ffee00" * 8, b"x" * 40
    base = b"\0".join([b"bootloader", anchor.encode(), b"hello world string", pub_run, pub_hex]) + b"\0"
    public = {sha(pub_run), sha(pub_hex)}
    cases = [
        ("clean, with a listed 40-char run and a listed 64-hex run", base, public, 0),
        ("name DEVICE_KEY_SECRET", base + b"DEVICE_KEY_SECRET=\0", public, 1),
        ("name CLOUD_FEED_KEY", base + b"-DCLOUD_FEED_KEY\0", public, 1),
        ("unlisted 64-hex key", base + b"0123456789abcdef" * 4 + b"\0", public, 1),
        ("github token", base + b"ghp_" + b"A1b2" * 8 + b"\0", public, 1),
        ("bearer", base + b"Bearer " + b"Zz9_" * 8 + b"\0", public, 1),
        ("unlisted 40-char run", base + b"Q7" * 20 + b"\0", public, 1),
        ("no anchor", base.replace(anchor.encode(), b"elsewhere.example.com"), public, 3),
        ("63-hex is not a key", base + b"a" * 63 + b"\0", public, 0),
        ("no list at all: any 64-hex is a hit (fail closed)", base, None, 1),
    ]
    bad = 0
    for name, data, pub, want in cases:
        got = exit_code(scan(data, anchor, public_runs=pub, name=name))
        ok = got == want
        bad += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: exit {got}, want {want}")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        with open(os.path.join(td, "lib.h"), "wb") as f:
            f.write(b'const char* s = "' + pub_run + b'"; const char* h = "' + pub_hex + b'";')
        for name, data, want in (("trace finds both shapes in a public input", base, 0),
                                 ("trace refuses a 40-char run in no public input", base + b"Q7" * 20 + b"\0", 1),
                                 ("trace refuses a 64-hex run in no public input",
                                  base + b"0123456789abcdef" * 4 + b"\0", 1)):
            got = exit_code(scan(data, anchor, trace_roots=[td], name=name))
            ok = got == want
            bad += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name}: exit {got}, want {want}")
    print("SELFTEST " + ("PASSED" if not bad else f"FAILED ({bad})"))
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Refuse a firmware image that carries a secret.")
    ap.add_argument("image", nargs="?")
    ap.add_argument("--anchor", help="a string that must be present in this image (control)")
    ap.add_argument("--trace", action="append", help="public build input to trace runs to (repeatable)")
    ap.add_argument("--public-runs", action="append",
                    help="a scan report or checked-in list of public run sha256s (repeatable)")
    ap.add_argument("--emit", help="write the scan report (image sha256 + traced-run hashes) here")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not a.image or not a.anchor:
        ap.print_usage()
        return 2
    with open(a.image, "rb") as f:
        data = f.read()
    public = None
    for path in a.public_runs or []:
        with open(path, encoding="utf-8") as f:
            public = (public or set()) | set(json.load(f).get("public_runs", []))
    v = scan(data, a.anchor, trace_roots=a.trace, public_runs=public, name=os.path.basename(a.image))
    print("\n".join(v.lines))
    if a.emit and exit_code(v) == 0 and v.report:
        with open(a.emit, "w", encoding="utf-8") as f:
            json.dump(v.report, f, indent=1)
            f.write("\n")
        print(f"report: {a.emit}")
    return exit_code(v)


if __name__ == "__main__":
    sys.exit(main())
