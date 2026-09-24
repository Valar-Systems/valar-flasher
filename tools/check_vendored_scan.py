#!/usr/bin/env python3
"""scan_image.py here must be byte-for-byte the Blipscope repo's.

Two comparisons, LF-normalised (git stores LF):
  1. against the file at the commit recorded in scan_image.SOURCE.json -- proves the
     copy is faithful to a real upstream version;
  2. against valar-scopes MAIN -- fails when upstream has moved on, so the copy
     cannot quietly go stale. (Before the upstream PR merges, main has no such file:
     that is reported and not counted as drift.)

    python tools/check_vendored_scan.py [--selftest]
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fetch(repo, ref, path):
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "valar-flasher-ci"}),
                                    timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def norm(b):
    return b.replace(b"\r\n", b"\n")


def verdict(local, at_commit, at_main):
    """(ok, lines). Pure, so the selftest can drive every branch."""
    lines, ok = [], True
    if at_commit is None:
        return False, ["the recorded upstream commit has no scan_image.py -- SOURCE is wrong"]
    if norm(local) != norm(at_commit):
        ok = False
        lines.append("DIFFERS from the recorded upstream commit -- it was edited here; change it upstream")
    else:
        lines.append("identical to the recorded upstream commit")
    if at_main is None:
        lines.append("valar-scopes main has no scan_image.py yet (upstream PR not merged) -- not drift")
    elif norm(local) != norm(at_main):
        ok = False
        lines.append("DIFFERS from valar-scopes main -- upstream moved; re-vendor and update SOURCE")
    else:
        lines.append("identical to valar-scopes main")
    return ok, lines


def selftest():
    a, b = b"x = 1\n", b"x = 2\n"
    cases = [("clean", (a, a, a), True), ("crlf is not drift", (a.replace(b"\n", b"\r\n"), a, a), True),
             ("edited locally", (b, a, a), False), ("upstream moved", (a, a, b), False),
             ("upstream not merged yet", (a, a, None), True), ("bad SOURCE commit", (a, None, a), False)]
    bad = 0
    for name, args, want in cases:
        got = verdict(*args)[0]
        bad += got != want
        print(f"  {'PASS' if got == want else 'FAIL'}  {name}")
    print("SELFTEST " + ("PASSED" if not bad else "FAILED"))
    return 1 if bad else 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    src = json.load(open(os.path.join(ROOT, "scan_image.SOURCE.json")))
    local = open(os.path.join(ROOT, "scan_image.py"), "rb").read()
    ok, lines = verdict(local, fetch(src["repo"], src["commit"], src["path"]),
                        fetch(src["repo"], "main", src["path"]))
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
