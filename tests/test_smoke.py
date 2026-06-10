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
    CURSOR_DIR=os.path.join(FIX, "cursor", "projects"),
    CURSOR_DB=os.path.join(FIX, "cursor", "state.vscdb"),
)
EXPECTED_SOURCES = {"claude", "codex", "gemini", "pi", "opencode", "cursor"}
EXPECTED_FMTS = EXPECTED_SOURCES - {"cursor"} | {"cursor-jsonl", "cursor-sqlite"}
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
    def test_all_sources_discovered(self):
        with mock.patch.multiple(paxel, **SRC_DIRS):
            found = paxel.discover_sources(list(paxel.ALL_SOURCES))
        fmts = {fmt for _, _, fmt in found}
        self.assertEqual(fmts, EXPECTED_FMTS,
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

    def test_bash_command_as_argv_list_does_not_crash(self):
        # Regression for issue #6: Codex's local_shell_call stores the command as an argv
        # LIST (["bash","-lc","…"]), which crashed bash_writes_file/bash_runs_tests with
        # "TypeError: expected string or bytes-like object, got 'list'". _cmd_str joins it.
        self.assertEqual(paxel._cmd_str(["bash", "-lc", "echo hi > f.txt"]),
                         "bash -lc echo hi > f.txt")
        self.assertEqual(paxel._cmd_str("echo hi"), "echo hi")
        self.assertEqual(paxel._cmd_str(None), "")
        # the joined string still triggers the redirect + test detectors (don't crash, stay correct)
        self.assertTrue(paxel.bash_writes_file(["bash", "-lc", "echo hi > f.txt"]))
        self.assertTrue(paxel.bash_runs_tests(["uv", "run", "pytest", "-q"]))
        self.assertFalse(paxel.bash_writes_file(["ls", "-la"]))

    def test_cursor_tool_name_maps_cursor_tools(self):
        # SQLite-era snake_case names
        self.assertEqual(paxel._cursor_tool_name("read_file_v2"), "Read")
        self.assertEqual(paxel._cursor_tool_name("run_terminal_command_v2"), "Bash")
        self.assertEqual(paxel._cursor_tool_name("task_v2"), "Agent")
        # modern JSONL CamelCase names (the StrReplace miss zeroed Cursor tool churn)
        self.assertEqual(paxel._cursor_tool_name("StrReplace"), "Edit")
        self.assertEqual(paxel._cursor_tool_name("ApplyPatch"), "Edit")
        self.assertEqual(paxel._cursor_tool_name("ReadFile"), "Read")
        self.assertEqual(paxel._cursor_tool_name("ReadLints"), "Read")
        self.assertEqual(paxel._cursor_tool_name("Shell"), "Bash")
        self.assertEqual(paxel._cursor_tool_name("rg"), "Grep")
        self.assertEqual(paxel._cursor_tool_name("SemanticSearch"), "Grep")
        self.assertEqual(paxel._cursor_tool_name("Delete"), "Edit")
        self.assertEqual(paxel._cursor_tool_name("AskQuestion"), "AskUserQuestion")
        self.assertEqual(paxel._cursor_tool_name("CreatePlan"), "EnterPlanMode")
        self.assertEqual(paxel._cursor_tool_name("Subagent"), "Agent")
        self.assertEqual(paxel._cursor_tool_name("Task"), "Agent")

    def test_cursor_call_mcp_tool_counts_as_mcp(self):
        name, inp = paxel._cursor_tool("CallMcpTool",
                                       {"server": "linear", "toolName": "search_issues"})
        self.assertEqual(name, "mcp__linear__search_issues")
        self.assertEqual(paxel.classify_tool(name), "explore")

    def test_cursor_apply_patch_string_params_become_churn(self):
        patch = "*** Update File: src/foo.py\n+new line\n+another\n"
        name, inp = paxel._cursor_tool("ApplyPatch", patch)
        self.assertEqual(name, "Edit")
        self.assertEqual(inp["file_path"], "src/foo.py")
        self.assertGreater(paxel.line_count(inp["new_string"]), 0)

    def test_cursor_clean_prompt_extracts_user_query(self):
        wrapped = ("<attached_files>\n<file>x.py</file>\n</attached_files>\n"
                   "<user_query>\nfix the bug\n</user_query>")
        self.assertEqual(paxel._cursor_clean_prompt(wrapped), "fix the bug")
        # no <user_query>: harness wrapper blocks are stripped, human text kept
        bare = "<attached_files>\n<file>x.py</file>\n</attached_files>\nplain question"
        self.assertEqual(paxel._cursor_clean_prompt(bare), "plain question")

    def test_cursor_project_cwd_from_slug(self):
        cwd = paxel._cursor_project_cwd("Users-demo-cursorproj")
        self.assertEqual(cwd, "/Users/demo/cursorproj")

    def test_cursor_subagent_meta_attributes_to_parent_session(self):
        fp = os.path.join(SRC_DIRS["CURSOR_DIR"], "Users-demo-cursorproj",
                          "agent-transcripts", "demo-session", "subagents",
                          "sub-explore-1.jsonl")
        sid, cwd, sidechain = paxel._cursor_jsonl_meta(fp)
        self.assertTrue(sidechain)
        self.assertEqual(sid, "demo-session")
        self.assertEqual(cwd, "/Users/demo/cursorproj")

    def test_cursor_prefers_sqlite_copy_over_jsonl_twin(self):
        # SQLite bubbles carry timestamps + tool statuses the JSONL lacks, so the
        # JSONL twin of a composer in state.vscdb must be dropped — but subagent
        # sidechains and JSONL-only sessions must survive the dedup.
        with mock.patch.multiple(paxel, **SRC_DIRS):
            sources = paxel.discover_sources(["cursor"])
            kept, twins = paxel._cursor_dedup(sources)
        kept_jsonl = {os.path.basename(fp) for _, fp, fmt in kept if fmt == "cursor-jsonl"}
        self.assertNotIn("demo-session.jsonl", kept_jsonl)         # covered by sqlite
        self.assertIn("jsonl-only-session.jsonl", kept_jsonl)      # not in the DB
        self.assertIn("sub-explore-1.jsonl", kept_jsonl)           # sidechain, always kept
        self.assertEqual(twins.get("demo-session", {}).get("cwd"), "/Users/demo/cursorproj")

    def test_cursor_sqlite_events_carry_timestamps_and_cwd(self):
        db = SRC_DIRS["CURSOR_DB"]
        events = list(paxel._cursor_sqlite_events(
            db, {"demo-session": {"cwd": "/Users/demo/cursorproj"}}))
        prompts = [e for e in events if e.get("type") == "user"
                   and isinstance(e.get("message", {}).get("content"), str)]
        texts = [e["message"]["content"] for e in prompts]
        self.assertIn("legacy sqlite-only question thanks", texts)
        self.assertIn("duplicate prompt should not count", texts)  # canonical copy now
        demo = [e for e in events if e.get("sessionId") == "demo-session"]
        self.assertTrue(all(e.get("cwd") == "/Users/demo/cursorproj" for e in demo))
        self.assertTrue(any(e.get("timestamp") for e in demo))
        # tool statuses: one error, one cancelled (cancelled must NOT count as an error)
        results = [b for e in events if e.get("sessionId") == "demo-session"
                   for b in (e.get("message", {}).get("content") or [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        self.assertEqual(sum(1 for b in results if b.get("is_error")), 1)

    def test_cursor_sqlite_edit_churn_backfilled_from_jsonl_twin(self):
        # Real edit_file_v2 sqlite params carry only the file PATH — the old/new strings
        # exist only in the JSONL twin, so churn must be backfilled from it.
        jsonl = os.path.join(SRC_DIRS["CURSOR_DIR"], "Users-demo-cursorproj",
                             "agent-transcripts", "demo-session", "demo-session.jsonl")
        events = list(paxel._cursor_sqlite_events(
            SRC_DIRS["CURSOR_DB"],
            {"demo-session": {"cwd": "/Users/demo/cursorproj", "jsonl": jsonl}}))
        edits = [b for e in events if e.get("sessionId") == "demo-session"
                 for b in (e.get("message", {}).get("content") or [])
                 if isinstance(b, dict) and b.get("type") == "tool_use"
                 and b.get("name") == "Edit"]
        self.assertTrue(edits, "no Edit tool_use came out of the sqlite reader")
        self.assertEqual(edits[0]["input"]["new_string"], "a = 2\nb = 3\n")
        self.assertEqual(edits[0]["input"]["old_string"], "a = 1\n")
        self.assertEqual(edits[0]["input"]["file_path"], "/Users/demo/cursorproj/main.py")

    def test_compute_scores_has_three_axes_not_steering(self):
        # empty-data guard returns the canonical axis set — guards against re-adding Steering.
        zero = paxel.compute_scores({"volume": {"total_sessions": 0, "tool_calls_total": 0},
                                     "behavior": {}, "velocity": {}})
        self.assertEqual(set(zero), SCORED_AXES)
        self.assertNotIn("Steering", zero)


if __name__ == "__main__":
    unittest.main(verbosity=2)
