"""Smoke + invariant tests for paxel.py — stdlib only (no pytest), so `python3 -m unittest`
runs them anywhere, matching the project's zero-dependency ethos.

Strategy: tiny committed transcript fixtures (one per source) live under tests/fixtures/.
We point paxel's source-directory globals at them, run the WHOLE pipeline end-to-end into a
temp dir, and assert it produces a valid profile without crashing. This is the safety net a
future source-parser PR self-verifies against (the gap we hit hand-reviewing PR #1).
"""
import os
import sys
import io
import json
import re
import glob
import shutil
import tempfile
import subprocess
import contextlib
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, ROOT)
import paxel  # noqa: E402

# Redirect every source-discovery global at the fixtures so the run is hermetic
# (never touches the developer's real ~/.claude, ~/.codex, etc.).
SRC_DIRS = dict(
    BASE=os.path.join(FIX, "claude"),
    CODEX_DIR=os.path.join(FIX, "codex"),
    GEMINI_DIR=os.path.join(FIX, "gemini"),
    PI_DIR=os.path.join(FIX, "pi"),
    OPENCODE_DIR=os.path.join(FIX, "opencode"),
    CURSOR_DB=os.path.join(FIX, "__no_cursor__"),   # nonexistent → note_experimental() stays quiet
)
EXPECTED_SOURCES = {"claude", "codex", "gemini", "pi", "opencode"}
SCORED_AXES = {"Execution", "Planning", "Engineering"}


def _run(testcase, args):
    """Run paxel.main() over the fixtures into a fresh temp OUT_DIR; return (stdout, out_dir)."""
    out = tempfile.mkdtemp(prefix="paxel-test-")
    testcase.addCleanup(shutil.rmtree, out, ignore_errors=True)
    tern = os.path.join(ROOT, "tern.png")          # poster logo loads from OUT_DIR/tern.png
    if os.path.exists(tern):
        shutil.copy(tern, os.path.join(out, "tern.png"))
    argv = ["paxel.py"] + args + ["--no-open"]
    buf = io.StringIO()
    with mock.patch.multiple(paxel, OUT_DIR=out, **SRC_DIRS), \
            mock.patch.object(sys, "argv", argv), \
            contextlib.redirect_stdout(buf):
        paxel.main()
    return buf.getvalue(), out


class TestDiscovery(unittest.TestCase):
    def test_all_five_sources_discovered(self):
        with mock.patch.multiple(paxel, **SRC_DIRS):
            found = paxel.discover_sources(list(paxel.ALL_SOURCES))
        fmts = {fmt for _, _, fmt in found}
        self.assertEqual(fmts, EXPECTED_SOURCES,
                         f"a source fixture stopped being discovered: got {fmts}")


class TestPipeline(unittest.TestCase):
    def test_all_sources_end_to_end(self):
        out_text, out = _run(self, [])               # no args = all sources
        prof = os.path.join(out, "profile.html")
        self.assertTrue(os.path.exists(prof), "profile.html was not written")
        with open(prof, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("scorecard", html.lower())
        self.assertIn('class="steerread"', html, "Steering reading block missing")
        # stats.json must be valid JSON
        with open(os.path.join(out, "stats.json"), encoding="utf-8") as fh:
            json.load(fh)
        # the run reported real activity — sessions must be NON-ZERO, so a silent parser
        # regression (discovery works but parsing yields nothing) fails instead of false-greening
        self.assertRegex(out_text, r"sessions=[1-9]\d*")

    def test_each_source_runs_in_isolation(self):
        # A broken single parser should be pinpointed, not hidden behind the others.
        for src in sorted(EXPECTED_SOURCES):
            with self.subTest(source=src):
                _, out = _run(self, [src])
                self.assertTrue(os.path.exists(os.path.join(out, "profile.html")),
                                f"{src}-only run produced no profile")

    def test_profile_invariants(self):
        _, out = _run(self, [])
        with open(os.path.join(out, "profile.html"), encoding="utf-8") as fh:
            html = fh.read()
        # exactly the three scored axes render as bar rows
        for axis in SCORED_AXES:
            self.assertIn(f'<span class="name">{axis}</span>', html)
        # Steering is DESCRIBED, never a scored bar row
        self.assertNotIn('<span class="name">Steering</span>', html)
        # the article fix: no archetype should read "You're a The Architect"
        self.assertNotIn("You're a The ", html)
        # the poster's embedded CARD payload must be valid JSON (guards the _js() escaper)
        card_line = next((ln for ln in html.splitlines()
                          if ln.strip().startswith("var CARD=")), None)
        self.assertIsNotNone(card_line, "var CARD= line not found in profile.html")
        card_json = card_line.strip()[len("var CARD="):].rstrip(";")
        card = json.loads(card_json)
        self.assertEqual({s[0] for s in card["scores"]}, SCORED_AXES)
        self.assertIn("steering", card)              # described row present on the poster too

    @unittest.skipUnless(shutil.which("node"), "node not installed (CI installs it)")
    def test_poster_js_is_valid_syntax(self):
        # The poster JS is a hand-written raw string — the viral artifact. node --check it.
        _, out = _run(self, [])
        with open(os.path.join(out, "profile.html"), encoding="utf-8") as fh:
            html = fh.read()
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(blocks, "no <script> block found in profile.html")
        js_path = os.path.join(out, "poster.js")
        with open(js_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(blocks))
        res = subprocess.run([shutil.which("node"), "--check", js_path],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"poster JS has a syntax error:\n{res.stderr}")


class TestUnits(unittest.TestCase):
    def test_hero_lead_article(self):
        self.assertEqual(paxel._hero_lead("The Architect"), "You're")
        self.assertEqual(paxel._hero_lead("The Director"), "You're")
        self.assertEqual(paxel._hero_lead("Velocity Machine"), "You're a")
        self.assertEqual(paxel._hero_lead("Brute-Force Architect"), "You're a")
        self.assertEqual(paxel._hero_lead(""), "You're a")
        self.assertEqual(paxel._hero_lead(None), "You're a")   # the None-guard is the refactor's whole point

    def test_crashout_witching_hour(self):
        t = "why is this still broken"
        day = paxel._crashout_score(t, hour=14)
        self.assertGreater(paxel._crashout_score(t, hour=3), day)   # 3am gets the bump
        self.assertGreater(paxel._crashout_score(t, hour=2), day)   # 2am inclusive
        self.assertEqual(paxel._crashout_score(t, hour=6), day)     # 6am exclusive
        self.assertEqual(paxel._crashout_score(t), day)             # no hour == daytime

    def test_canon_tool_normalizes_lowercase(self):
        self.assertEqual(paxel._canon_tool("bash"), "Bash")
        self.assertEqual(paxel._canon_tool("read"), "Read")
        self.assertEqual(paxel._canon_tool("edit"), "Edit")

    def test_compute_scores_has_three_axes_not_steering(self):
        # empty-data guard returns the canonical axis set — guards against re-adding Steering.
        zero = paxel.compute_scores({"volume": {"total_sessions": 0, "tool_calls_total": 0},
                                     "behavior": {}, "velocity": {}})
        self.assertEqual(set(zero), SCORED_AXES)
        self.assertNotIn("Steering", zero)


if __name__ == "__main__":
    unittest.main(verbosity=2)
