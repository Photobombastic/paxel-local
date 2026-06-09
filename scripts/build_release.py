#!/usr/bin/env python3
"""Build the self-contained, single-file release asset of paxel.py.

The repo's paxel.py keeps `_TERN_B64 = ""` (clean source — clone users have tern.png on disk, which
takes precedence). This injects the base64 of tern.png so the DOWNLOADABLE one-file build is
standalone: the shareable poster keeps its branding with no companion file. That single file is what
we attach to a GitHub Release, so its `download_count` becomes a live, real-time "how many people
tried it" meter (no telemetry in the tool itself — counting a download is upstream of running it).

Run from the repo root:  python3 scripts/build_release.py   ->   dist/paxel.py
"""
import base64
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "paxel.py"), encoding="utf-8").read()
with open(os.path.join(ROOT, "tern.png"), "rb") as fh:
    b64 = base64.b64encode(fh.read()).decode()

NEEDLE = '_TERN_B64 = ""'
if src.count(NEEDLE) != 1:
    sys.exit('error: expected exactly one `_TERN_B64 = ""` marker in paxel.py — aborting.')
built = src.replace(NEEDLE, f'_TERN_B64 = "{b64}"', 1)

os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
out = os.path.join(ROOT, "dist", "paxel.py")
with open(out, "w", encoding="utf-8") as fh:
    fh.write(built)
print(f"✓ wrote {out}  ({len(built):,} bytes, tern logo embedded)")
