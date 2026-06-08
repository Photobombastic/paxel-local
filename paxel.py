#!/usr/bin/env python3
"""
paxel-local: a fully-local recreation of YC's Paxel builder-profile tool.

Paxel reads your AI coding-agent session transcripts and emits a "how you build
with AI" profile. The catch: it ships transcript-derived content to YC's LLM
proxy and uploads narratives + metadata to YC (readable by any YC employee,
retained indefinitely). This recreation does the same analysis with ZERO data
leaving your machine:

  - This script computes the metrics Paxel reports, deterministically, from
    ~/.claude/projects/**/*.jsonl  (Claude Code transcripts).
  - The qualitative half (Builder Archetype, Autonomy, standout traits) is written
    by YOUR OWN Claude/GPT session, reading narrative_input.md locally — i.e. the
    local stand-in for the LLM Paxel would otherwise send your data to.

Usage:
    python3 paxel.py            # reads ~/.claude/projects, writes outputs here

No dependencies beyond the Python 3 standard library. No NETWORK calls anywhere.
For accurate "gold-standard" churn it shells out to the local `git` CLI to read
`git log --numstat` on repos found in your transcripts — this captures every
committed change however it was made (Edit, Bash heredoc, sed, vim...), not just
the Edit/Write tool path. That git read is 100% on-device; nothing is uploaded.

Outputs (in this script's directory):
  - stats.json          machine-readable metrics
  - report.md           deterministic stats report (human-readable)
  - narrative_input.md  curated, LOCAL-ONLY excerpts for the narrative pass
                        (may contain names/PII from your prompts — keep local)

Sources: Claude Code, Codex CLI, and Gemini CLI (auto-detected). Cursor and
opencode are detected but not yet parsed (experimental — see README). Restrict
with args, e.g. `python3 paxel.py claude` for Claude-only; no args = all detected.
One-shot; just re-run to rebuild as sessions accumulate.
"""

import json
import os
import glob
import math
import re
import sys
import contextlib
import subprocess
import statistics
from collections import Counter, defaultdict
from datetime import datetime

BASE = os.path.expanduser("~/.claude/projects")
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- tool taxonomy -----------------------------------------------------------
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "NotebookRead"}
DISCOVER_TOOLS = {"WebSearch", "WebFetch", "ToolSearch"}
EXEC_TOOLS = {"Bash", "BashOutput", "KillShell"}
DELEGATE_TOOLS = {"Agent", "Task"}
PLAN_TOOLS = {"TodoWrite", "ExitPlanMode", "EnterPlanMode", "EnterWorktree",
              "ExitWorktree", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet"}
SCHEDULE_TOOLS = {"ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
                  "RemoteTrigger", "PushNotification", "Monitor"}
SKILL_TOOLS = {"Skill"}
ASK_TOOLS = {"AskUserQuestion"}

# verbs that mark an MCP tool as read/inspect rather than produce/act
MCP_INSPECT_HINTS = ("read", "get", "list", "search", "find", "describe",
                     "snapshot", "screenshot", "query", "fetch", "whoami",
                     "details", "status", "info", "show", "doc_")


def classify_tool(name: str) -> str:
    if name in WRITE_TOOLS:
        return "produce"
    if name in READ_TOOLS or name in DISCOVER_TOOLS or name in PLAN_TOOLS:
        return "explore"
    if name in EXEC_TOOLS:
        return "execute"
    if name in DELEGATE_TOOLS:
        return "delegate"
    if name in SKILL_TOOLS:
        return "execute"
    if name in SCHEDULE_TOOLS:
        return "execute"
    if name in ASK_TOOLS:
        return "ask"
    if name.startswith("mcp__"):
        last = name.split("__")[-1].lower()
        if any(h in last for h in MCP_INSPECT_HINTS):
            return "explore"
        return "produce"
    return "other"


def parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def line_count(s):
    if not s:
        return 0
    return s.count("\n") + (1 if s and not s.endswith("\n") else 0)


def strip_injections(text):
    """Remove injected wrappers so prompt length reflects what the human typed."""
    import re
    if not text:
        return ""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<command-name>.*?</command-name>", "", text, flags=re.S)
    text = re.sub(r"<command-message>.*?</command-message>", "", text, flags=re.S)
    text = re.sub(r"<command-args>.*?</command-args>", "", text, flags=re.S)
    text = re.sub(r"<local-command-stdout>.*?</local-command-stdout>", "", text, flags=re.S)
    return text.strip()


# a Bash command writes/modifies a file if it redirects (not to /dev/null),
# uses a heredoc, sed -i, or tee — used to estimate shell-authored churn the
# Edit/Write tools never see.
_REDIR = re.compile(r'(?<!2)>{1,2}(?!\s*(?:/dev/null|&\d))')


def bash_writes_file(cmd):
    return bool(_REDIR.search(cmd)
                or re.search(r'<<(?!<)', cmd)            # heredoc, not a <<< here-string
                or re.search(r'\bsed\s+-i', cmd)
                or re.search(r'\btee\s+(?![>|])', cmd))   # tee to a file, not a process sub


def _git(cwd, args, timeout=30):
    """Run a git command locally; return stdout or '' on any failure. Never raises."""
    try:
        p = subprocess.run(["git", "-C", cwd] + args, capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout if p.returncode == 0 else ""
    except Exception:
        return ""


def git_churn(cwds, since_iso, until_iso):
    """Gold-standard churn: real insertions/deletions from `git log --numstat`,
    capturing EVERY committed change regardless of how it was made (Edit, Bash,
    vim, etc.). 100% local — git reads .git on disk, nothing is uploaded.
    Repos that are missing/non-git are reported as unavailable, not silently dropped.
    """
    # Dedupe by repo IDENTITY (root-commit SHA), not path — otherwise multiple
    # clones/worktrees of the same project (e.g. a fork + a worktree + a copy)
    # each contribute the same commits and inflate the total.
    tops = {}                       # identity -> toplevel path (first seen)
    for cwd in cwds:
        if not cwd or not os.path.isdir(cwd):
            continue
        top = _git(cwd, ["rev-parse", "--show-toplevel"]).strip()
        if not top:
            continue
        root = _git(top, ["rev-list", "--max-parents=0", "HEAD"]).split()
        if root:
            ident = "root:" + ",".join(sorted(root))
        else:
            remote = _git(top, ["config", "remote.origin.url"]).strip()
            ident = "remote:" + remote if remote else "path:" + top
        tops.setdefault(ident, top)
    per_repo, ins_tot, del_tot, commits_tot = [], 0, 0, 0
    for top in sorted(tops.values()):
        email = _git(top, ["config", "user.email"]).strip()
        args = ["log", "--numstat", "--no-merges",
                f"--since={since_iso}", f"--until={until_iso}",
                "--pretty=tformat:__C__"]
        if email:
            args.append(f"--author={email}")
        out = _git(top, args)
        ins = dels = commits = 0
        for ln in out.splitlines():
            if ln == "__C__":
                commits += 1
                continue
            parts = ln.split("\t")
            if len(parts) == 3:
                a, d, _ = parts
                if a.isdigit():
                    ins += int(a)
                if d.isdigit():
                    dels += int(d)
        if ins or dels or commits:
            per_repo.append((os.path.basename(top), ins, dels, commits))
            ins_tot += ins
            del_tot += dels
            commits_tot += commits
    per_repo.sort(key=lambda x: -(x[1] + x[2]))
    return {
        "repos_seen": len(tops),
        "repos_with_commits": len(per_repo),
        "insertions": ins_tot,
        "deletions": del_tot,
        "churn": ins_tot + del_tot,
        "commits": commits_tot,
        "per_repo": per_repo[:12],
    }


def pctile(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100) * (len(sorted_vals) - 1)))))
    return sorted_vals[k]


# ---------------------------------------------------------------------------
# Multi-source discovery + translators. Each non-Claude format is translated
# into Claude-shaped event dicts so the single aggregation loop in main() works
# unchanged across tools. Every read is local — nothing is uploaded.
# Solid/tested: Claude Code, Codex CLI, Gemini CLI.
# Experimental (detected, not yet parsed): Cursor (SQLite blobs), opencode (KV).
# ---------------------------------------------------------------------------
CODEX_DIR = os.path.expanduser("~/.codex/sessions")
GEMINI_DIR = os.path.expanduser("~/.gemini/tmp")
CURSOR_DB = os.path.expanduser("~/Library/Application Support/Cursor/User/globalStorage/state.vscdb")
OPENCODE_DIR = os.path.expanduser("~/.local/share/opencode")
ALL_SOURCES = ("claude", "codex", "gemini")


def discover_sources(selected):
    out = []
    if "claude" in selected and os.path.isdir(BASE):
        for fp in sorted(glob.glob(os.path.join(BASE, "**", "*.jsonl"), recursive=True)):
            out.append(("claude", fp, "claude"))
    if "codex" in selected and os.path.isdir(CODEX_DIR):
        for fp in sorted(glob.glob(os.path.join(CODEX_DIR, "**", "*.jsonl"), recursive=True)):
            out.append(("codex", fp, "codex"))
    if "gemini" in selected and os.path.isdir(GEMINI_DIR):
        for fp in sorted(glob.glob(os.path.join(GEMINI_DIR, "**", "*.json"), recursive=True)):
            out.append(("gemini", fp, "gemini"))
    return out


def note_experimental():
    """Cursor (SQLite blobs) and opencode (KV store) need real reverse-engineering;
    flag them as detected-but-unsupported rather than ship a fragile guess parser."""
    found = [n for n, p in (("Cursor", CURSOR_DB), ("opencode", OPENCODE_DIR)) if os.path.exists(p)]
    if found:
        print(f"  note: {', '.join(found)} detected but not yet parsed "
              f"(experimental — PRs welcome, see README)")


def _texts(content):
    """Join text from a Claude/Codex/Gemini content list (or plain string)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict):
                out.append(b.get("text") or b.get("input_text") or b.get("output_text") or "")
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(x for x in out if x)
    return ""


def iter_events(fp, fmt):
    """Yield Claude-shaped event dicts for any supported source format."""
    if fmt == "claude":
        try:
            fh = open(fp, "r", errors="replace")
        except Exception:
            return
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    yield {"__bad__": True}
                    continue
                yield obj if isinstance(obj, dict) else {"__bad__": True}
    elif fmt == "codex":
        yield from _codex_events(fp)
    elif fmt == "gemini":
        yield from _gemini_events(fp)


def _codex_tool(p):
    """Map a Codex tool/function call to a Claude-shaped (name, input) tool_use."""
    pt = p.get("type")
    if pt == "web_search_call":
        return "WebSearch", {}
    name = p.get("name") or pt or "tool"
    try:
        args = json.loads(p.get("arguments") or "{}")
    except Exception:
        args = {}
    if not isinstance(args, dict):
        args = {}
    if pt == "local_shell_call" or name in ("exec_command", "shell", "local_shell", "bash"):
        return "Bash", {"command": args.get("cmd") or args.get("command") or str(p.get("action") or "")}
    if name in ("apply_patch", "patch", "edit_file", "write_file", "create_file"):
        return "Edit", {"new_string": args.get("patch") or args.get("content") or "",
                        "old_string": "", "file_path": args.get("path") or args.get("file") or ""}
    return name, args


def _codex_events(fp):
    rows = []
    try:
        for line in open(fp, "r", errors="replace"):
            line = line.strip()
            if line:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except Exception:
        return
    sid = os.path.basename(fp).split(".")[0]
    cwd = None
    for ev in rows:                       # first pass: session id + working dir
        p = ev.get("payload") or {}
        if ev.get("type") == "session_meta":
            sid = p.get("id") or sid
            cwd = p.get("cwd") or cwd
        elif ev.get("type") == "response_item" and p.get("type") == "function_call":
            try:
                a = json.loads(p.get("arguments") or "{}")
                cwd = cwd or (a.get("workdir") if isinstance(a, dict) else None)
            except Exception:
                pass
    base = {"sessionId": sid, "cwd": cwd}
    for ev in rows:
        if ev.get("type") != "response_item":
            continue
        ts = ev.get("timestamp")
        p = ev.get("payload") or {}
        pt = p.get("type")
        if pt == "message":
            role = p.get("role")
            text = _texts(p.get("content"))
            if role == "user" and text:
                yield {**base, "type": "user", "timestamp": ts,
                       "message": {"role": "user", "content": text}}
            elif role == "assistant":
                yield {**base, "type": "assistant", "timestamp": ts,
                       "message": {"role": "assistant",
                                   "content": [{"type": "text", "text": text}] if text else []}}
            # developer/system messages are tooling, not human prompts → skipped
        elif pt == "reasoning":
            yield {**base, "type": "assistant", "timestamp": ts,
                   "message": {"role": "assistant",
                               "content": [{"type": "thinking",
                                            "thinking": _texts(p.get("content")) or p.get("summary") or ""}]}}
        elif pt in ("function_call", "local_shell_call", "custom_tool_call", "web_search_call"):
            name, inp = _codex_tool(p)
            yield {**base, "type": "assistant", "timestamp": ts,
                   "message": {"role": "assistant",
                               "content": [{"type": "tool_use", "name": name, "input": inp}]}}
        elif pt == "function_call_output":
            out = p.get("output")
            is_err = isinstance(out, dict) and out.get("success") is False
            yield {**base, "type": "user", "timestamp": ts,
                   "message": {"role": "user",
                               "content": [{"type": "tool_result", "is_error": bool(is_err)}]}}


def _gemini_events(fp):
    try:
        d = json.load(open(fp, "r", errors="replace"))
    except Exception:
        return
    if not isinstance(d, dict):
        return
    base = {"sessionId": d.get("sessionId") or os.path.basename(fp), "cwd": None}
    for m in d.get("messages") or []:
        if not isinstance(m, dict):
            continue
        ts = m.get("timestamp")
        role = m.get("type") or m.get("role")
        content = m.get("content")
        text = _texts(content)
        if role == "user" and text:
            yield {**base, "type": "user", "timestamp": ts,
                   "message": {"role": "user", "content": text}}
        elif role in ("gemini", "model", "assistant"):
            blocks = [{"type": "text", "text": text}] if text else []
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("functionCall"), dict):
                        fc = part["functionCall"]
                        blocks.append({"type": "tool_use", "name": fc.get("name", "tool"),
                                       "input": fc.get("args") if isinstance(fc.get("args"), dict) else {}})
            yield {**base, "type": "assistant", "timestamp": ts,
                   "message": {"role": "assistant", "content": blocks}}


def main():
    # Sources to analyze: pass names as args (e.g. `python3 paxel.py claude`) to
    # restrict; default is every detected source. ("claude" keeps it to your own
    # Claude Code work; omit args to fold in Codex + Gemini too.)
    selected = [a.lower() for a in sys.argv[1:] if not a.startswith("-")] or list(ALL_SOURCES)
    unknown = [s for s in selected if s not in ALL_SOURCES]
    if unknown:
        print(f"  warning: unknown source(s) {unknown} ignored; valid: {', '.join(ALL_SOURCES)}")
    sources = discover_sources(selected)
    by_src = Counter(s for s, _, _ in sources)
    print(f"Found {len(sources)} transcript files across "
          f"{', '.join(f'{k}:{v}' for k, v in by_src.items()) or 'no sources'}")
    note_experimental()
    if not sources:
        print("\n  No transcripts found in ~/.claude/projects, ~/.codex/sessions, or ~/.gemini/tmp.")
        print("  Nothing to analyze — run this where you've actually used a coding agent.")
        return

    # ---- accumulators --------------------------------------------------------
    files_parsed = 0
    lines_total = 0
    lines_bad = 0

    session_ts = defaultdict(list)   # sessionId -> [epoch seconds]
    session_files = defaultdict(set)
    GAP_CAP_S = 600                   # cap idle gaps at 10 min when summing active time

    prompts_count = 0
    prompt_lengths = []        # chars of genuine typed prompts
    command_invocations = 0

    assistant_turns = 0
    text_blocks = 0
    thinking_blocks = 0
    thinking_chars = 0
    tool_use_total = 0
    tool_counter = Counter()
    cat_counter = Counter()    # explore/produce/execute/delegate/ask/other
    mcp_calls = 0
    native_calls = 0

    model_counter = Counter()
    skill_counter = Counter()
    subagent_counter = Counter()
    project_activity = Counter()   # cwd -> events
    project_sessions = defaultdict(set)

    lines_added = 0
    lines_removed = 0
    edits_per_file_events = []      # iteration depth samples (edits to a file before commit)
    git_commits = 0
    background_tasks = 0
    scheduled_actions = 0
    questions_asked = 0

    tool_errors = 0
    api_errors = 0
    recovered_errors = 0

    bash_write_calls = 0       # Bash calls that write/modify a file
    bash_authored_lines = 0    # newlines inside those commands (shell-authored content estimate)

    hour_hist = Counter()          # local hour 0-23
    weekday_hist = Counter()       # 0=Mon..6=Sun
    date_set = set()
    all_min_dt = None
    all_max_dt = None

    # narrative samples
    opening_prompts = []           # (dt, project, text) first genuine prompt per session
    longest_prompts = []           # kept small via periodic trim

    seen_session_open = set()
    source_files = Counter()             # source -> files
    source_sessions = defaultdict(set)   # source -> sessionIds
    source_prompts = Counter()           # source -> genuine prompts

    for cur_src, fp, fmt in sources:
        files_parsed += 1
        source_files[cur_src] += 1
        if files_parsed % 300 == 0:
            print(f"  ...{files_parsed}/{len(sources)}")
        # per-session, per-file ordered state for error-recovery + iteration depth
        pending_error = defaultdict(bool)        # sessionId -> unrecovered error flag
        file_edit_run = defaultdict(lambda: defaultdict(int))  # session -> file -> edits since commit

        # iter_events() yields Claude-shaped event dicts for every source format,
        # so the per-event logic below is identical across Claude / Codex / Gemini.
        with contextlib.nullcontext(iter_events(fp, fmt)) as _evs:
            for ev in _evs:
                if ev.get("__bad__"):
                    lines_bad += 1
                    continue
                lines_total += 1

                etype = ev.get("type")
                sid = ev.get("sessionId")
                cwd = ev.get("cwd")
                dt = parse_ts(ev.get("timestamp"))

                if dt is not None:
                    if all_min_dt is None or dt < all_min_dt:
                        all_min_dt = dt
                    if all_max_dt is None or dt > all_max_dt:
                        all_max_dt = dt
                    hour_hist[dt.hour] += 1
                    weekday_hist[dt.weekday()] += 1
                    date_set.add(dt.date().isoformat())
                    if sid:
                        session_ts[sid].append(dt.timestamp())
                if sid:
                    session_files[sid].add(fp)
                    source_sessions[cur_src].add(sid)
                if cwd:
                    project_activity[cwd] += 1
                    if sid:
                        project_sessions[cwd].add(sid)

                msg = ev.get("message") if isinstance(ev.get("message"), dict) else None

                # ---- API error / retry events (system + assistant) ----------
                if ev.get("isApiErrorMessage") or ev.get("apiErrorStatus"):
                    api_errors += 1
                if etype == "system" and ev.get("retryAttempt"):
                    api_errors += 1

                # ---- genuine user prompts -----------------------------------
                if etype == "user" and msg is not None:
                    if (ev.get("isMeta") or ev.get("isCompactSummary")
                            or ev.get("isVisibleInTranscriptOnly") or ev.get("isSidechain")):
                        pass  # injected / non-human / subagent-dispatch instruction
                    else:
                        content = msg.get("content")
                        text = None
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            parts = [b.get("text", "") for b in content
                                     if isinstance(b, dict) and b.get("type") == "text"]
                            if parts:
                                text = "\n".join(parts)
                        if text is not None:
                            is_command = ("<command-name>" in text or
                                          text.lstrip().startswith("<local-command"))
                            cleaned = strip_injections(text)
                            if is_command and not cleaned:
                                command_invocations += 1
                            elif cleaned:
                                prompts_count += 1
                                source_prompts[cur_src] += 1
                                prompt_lengths.append(len(cleaned))
                                if is_command:
                                    command_invocations += 1
                                proj = os.path.basename(cwd) if cwd else "?"
                                if sid and sid not in seen_session_open:
                                    seen_session_open.add(sid)
                                    opening_prompts.append((dt, proj, cleaned[:600]))
                                longest_prompts.append((len(cleaned), proj, cleaned[:600]))
                                if len(longest_prompts) > 400:
                                    longest_prompts.sort(key=lambda x: -x[0])
                                    del longest_prompts[120:]

                    # ---- tool results inside user turns ---------------------
                    content = msg.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if isinstance(b, dict) and b.get("type") == "tool_result":
                                if b.get("is_error"):
                                    tool_errors += 1
                                    if sid:
                                        pending_error[sid] = True

                # ---- assistant turns ---------------------------------------
                elif etype == "assistant" and msg is not None:
                    assistant_turns += 1
                    mdl = msg.get("model")
                    if mdl:
                        model_counter[mdl] += 1
                    if ev.get("attributionSkill"):
                        skill_counter[ev["attributionSkill"]] += 1
                    content = msg.get("content")
                    if isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            bt = b.get("type")
                            if bt == "text":
                                text_blocks += 1
                            elif bt == "thinking":
                                thinking_blocks += 1
                                thinking_chars += len(b.get("thinking", "") or "")
                            elif bt == "tool_use":
                                name = b.get("name", "?")
                                inp = b.get("input", {}) if isinstance(b.get("input"), dict) else {}
                                tool_use_total += 1
                                tool_counter[name] += 1
                                cat_counter[classify_tool(name)] += 1
                                if name.startswith("mcp__"):
                                    mcp_calls += 1
                                else:
                                    native_calls += 1

                                # a tool use after a pending error = recovery
                                if sid and pending_error.get(sid):
                                    recovered_errors += 1
                                    pending_error[sid] = False

                                if name == "Skill":
                                    s = inp.get("skill")
                                    if s:
                                        skill_counter[s] += 1
                                if name == "Agent":
                                    st = inp.get("subagent_type", "general-purpose")
                                    subagent_counter[st] += 1
                                if name in ASK_TOOLS:
                                    questions_asked += 1
                                if inp.get("run_in_background"):
                                    background_tasks += 1
                                if name in SCHEDULE_TOOLS:
                                    scheduled_actions += 1

                                # ---- code churn + iteration depth ----------
                                if name == "Edit":
                                    a = line_count(inp.get("new_string", ""))
                                    r = line_count(inp.get("old_string", ""))
                                    lines_added += a
                                    lines_removed += r
                                    fpth = inp.get("file_path")
                                    if sid and fpth:
                                        file_edit_run[sid][fpth] += 1
                                elif name == "Write":
                                    a = line_count(inp.get("content", ""))
                                    lines_added += a
                                    fpth = inp.get("file_path")
                                    if sid and fpth:
                                        file_edit_run[sid][fpth] += 1
                                elif name == "MultiEdit":
                                    for e in inp.get("edits", []) or []:
                                        if isinstance(e, dict):
                                            lines_added += line_count(e.get("new_string", ""))
                                            lines_removed += line_count(e.get("old_string", ""))
                                    fpth = inp.get("file_path")
                                    if sid and fpth:
                                        file_edit_run[sid][fpth] += 1
                                elif name == "NotebookEdit":
                                    lines_added += line_count(inp.get("new_source", ""))
                                    fpth = inp.get("notebook_path")
                                    if sid and fpth:
                                        file_edit_run[sid][fpth] += 1
                                elif name == "Bash":
                                    cmd = inp.get("command", "") or ""
                                    if bash_writes_file(cmd):
                                        bash_write_calls += 1
                                        bash_authored_lines += cmd.count("\n")
                                    if "git commit" in cmd:
                                        git_commits += 1
                                        # flush iteration-depth run for this session
                                        if sid in file_edit_run:
                                            for cnt in file_edit_run[sid].values():
                                                if cnt > 0:
                                                    edits_per_file_events.append(cnt)
                                            file_edit_run[sid].clear()

        # end of file: flush any remaining edit runs as iteration-depth samples
        for sdict in file_edit_run.values():
            for cnt in sdict.values():
                if cnt > 0:
                    edits_per_file_events.append(cnt)

    # ---- derive ----------------------------------------------------------------
    total_sessions = len(session_ts) or len(session_files)
    # Active time = sum of consecutive inter-event gaps, each capped at GAP_CAP_S,
    # so resumed-session reuse and overnight idle don't inflate engaged time.
    durations_min = []
    for ts_list in session_ts.values():
        ts_list.sort()
        active_s = 0.0
        for a, bnext in zip(ts_list, ts_list[1:]):
            active_s += min(bnext - a, GAP_CAP_S)
        durations_min.append(active_s / 60.0)
    active_hours = sum(durations_min) / 60.0
    avg_session_min = statistics.mean(durations_min) if durations_min else 0
    median_session_min = statistics.median(durations_min) if durations_min else 0

    avg_prompt_len = statistics.mean(prompt_lengths) if prompt_lengths else 0
    median_prompt_len = statistics.median(prompt_lengths) if prompt_lengths else 0

    total_churn = lines_added + lines_removed          # tool-authored only (Edit/Write)
    code_velocity = (total_churn / active_hours) if active_hours > 0 else 0

    # Gold-standard churn: real git insertions/deletions, capturing EVERY committed
    # change however it was made (Edit, Bash heredoc, sed, vim...). 100% local.
    gc = git_churn(list(project_activity.keys()),
                   all_min_dt.isoformat() if all_min_dt else "1970-01-01",
                   all_max_dt.isoformat() if all_max_dt else "2100-01-01")
    git_velocity = (gc["churn"] / active_hours) if active_hours > 0 else 0

    explore = cat_counter.get("explore", 0) + thinking_blocks
    produce = cat_counter.get("produce", 0)
    execute = cat_counter.get("execute", 0)
    delegate = cat_counter.get("delegate", 0)
    doing = produce + execute + delegate
    planning_ratio = (explore / doing) if doing else 0

    tool_diversity = len(tool_counter)
    # shannon entropy over tool distribution (bonus, normalized 0-1)
    tot = sum(tool_counter.values()) or 1
    entropy = -sum((c / tot) * math.log2(c / tot) for c in tool_counter.values())
    norm_entropy = entropy / math.log2(tool_diversity) if tool_diversity > 1 else 0

    error_recovery_ratio = (recovered_errors / tool_errors) if tool_errors else 0
    error_rate_per_100_tools = (tool_errors / tool_use_total * 100) if tool_use_total else 0
    _depths = sorted(edits_per_file_events)
    iteration_mean = statistics.mean(_depths) if _depths else 0
    iteration_median = statistics.median(_depths) if _depths else 0
    iteration_p90 = pctile(_depths, 90)
    iteration_max = max(_depths) if _depths else 0
    heavy_files = sum(1 for d in _depths if d > 15)   # files hammered >15x in one session

    actions_per_prompt = (tool_use_total / prompts_count) if prompts_count else 0
    # autonomy proxy 0-100: weighted blend, transparent + bounded
    auto_actions = min(actions_per_prompt / 25.0, 1.0) * 45          # heavy agentic loops
    auto_deleg = min(delegate / max(total_sessions, 1) / 1.5, 1.0) * 20  # subagent dispatch rate
    auto_sched = min((scheduled_actions + background_tasks) / max(total_sessions, 1), 1.0) * 15
    auto_lowq = (1 - min(questions_asked / max(prompts_count, 1) * 6, 1.0)) * 20  # rarely stops to ask
    autonomy_score = round(auto_actions + auto_deleg + auto_sched + auto_lowq, 1)

    span_days = (all_max_dt - all_min_dt).days + 1 if (all_min_dt and all_max_dt) else 0
    active_days = len(date_set)

    DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    tzname = datetime.now().astimezone().tzname()
    tzoffset = datetime.now().astimezone().strftime("%z")

    peak_hours = [h for h, _ in hour_hist.most_common(3)]
    preferred_days = [DOW[d] for d, _ in weekday_hist.most_common(3)]

    stats = {
        "scope": "Sources: " + (", ".join(sorted(source_files)) or "none"),
        "generated_local_only": True,
        "corpus": {
            "sources": {s: {"files": source_files[s], "sessions": len(source_sessions[s]),
                            "prompts": source_prompts[s]} for s in sorted(source_files)},
            "files_parsed": files_parsed,
            "lines_total": lines_total,
            "lines_unparseable": lines_bad,
            "date_range": [all_min_dt.isoformat() if all_min_dt else None,
                            all_max_dt.isoformat() if all_max_dt else None],
            "span_days": span_days,
            "active_days": active_days,
            "timezone": f"{tzname} (UTC{tzoffset[:3]}:{tzoffset[3:]})",
        },
        "volume": {
            "total_sessions": total_sessions,
            "total_prompts": prompts_count,
            "command_invocations": command_invocations,
            "avg_prompt_length_chars": round(avg_prompt_len, 1),
            "median_prompt_length_chars": round(median_prompt_len, 1),
            "assistant_turns": assistant_turns,
            "tool_calls_total": tool_use_total,
            "thinking_blocks": thinking_blocks,
        },
        "tools": {
            "tool_diversity": tool_diversity,
            "tool_entropy_normalized": round(norm_entropy, 3),
            "mcp_calls": mcp_calls,
            "native_calls": native_calls,
            "mcp_share": round(mcp_calls / (mcp_calls + native_calls), 3) if (mcp_calls + native_calls) else 0,
            "top_tools": tool_counter.most_common(15),
            "category_breakdown": dict(cat_counter),
        },
        "velocity": {
            "git_churn_total": gc["churn"],
            "git_insertions": gc["insertions"],
            "git_deletions": gc["deletions"],
            "git_commits_real": gc["commits"],
            "git_velocity_lines_per_hour": round(git_velocity, 1),
            "git_repos_with_commits": gc["repos_with_commits"],
            "git_repos_seen": gc["repos_seen"],
            "git_per_repo": gc["per_repo"],
            "tool_churn_edit_write": total_churn,
            "tool_lines_added": lines_added,
            "tool_lines_removed": lines_removed,
            "tool_velocity_lines_per_hour": round(code_velocity, 1),
            "shell_write_calls": bash_write_calls,
            "shell_authored_lines_est": bash_authored_lines,
            "active_hours": round(active_hours, 1),
            "git_commits_grep": git_commits,
        },
        "behavior": {
            "planning_ratio_explore_to_doing": round(planning_ratio, 2),
            "explore_actions": explore,
            "produce_actions": produce,
            "execute_actions": execute,
            "delegate_actions": delegate,
            "avg_session_minutes": round(avg_session_min, 1),
            "median_session_minutes": round(median_session_min, 1),
            "error_recovery_ratio": round(error_recovery_ratio, 3),
            "error_rate_per_100_tools": round(error_rate_per_100_tools, 1),
            "tool_errors": tool_errors,
            "recovered_errors": recovered_errors,
            "api_errors_retries": api_errors,
            "iteration_depth_mean": round(iteration_mean, 2),
            "iteration_depth_median": round(iteration_median, 2),
            "iteration_depth_p90": iteration_p90,
            "iteration_depth_max": iteration_max,
            "files_hammered_over_15x": heavy_files,
            "actions_per_prompt": round(actions_per_prompt, 1),
            "questions_asked": questions_asked,
            "background_tasks": background_tasks,
            "scheduled_actions": scheduled_actions,
        },
        "rhythm": {
            "hour_histogram_local": {str(h): hour_hist.get(h, 0) for h in range(24)},
            "weekday_histogram": {DOW[d]: weekday_hist.get(d, 0) for d in range(7)},
            "peak_hours_local": peak_hours,
            "preferred_days": preferred_days,
        },
        "stack": {
            "models": model_counter.most_common(),
            "top_skills": skill_counter.most_common(15),
            "subagent_types": subagent_counter.most_common(10),
            "top_projects": [(os.path.basename(p), c, len(project_sessions[p]))
                             for p, c in project_activity.most_common(12)],
        },
        "autonomy": {
            "autonomy_score_0_100": autonomy_score,
            "components": {
                "actions_per_prompt": round(auto_actions, 1),
                "delegation": round(auto_deleg, 1),
                "scheduling_background": round(auto_sched, 1),
                "low_question_rate": round(auto_lowq, 1),
            },
        },
    }

    with open(os.path.join(OUT_DIR, "stats.json"), "w") as f:
        json.dump(stats, f, indent=2, default=str)

    write_report(stats)
    write_narrative_input(stats, opening_prompts, longest_prompts)
    scores = compute_scores(stats)
    archetype, quote = pick_archetype(stats, scores)
    write_profile_html(stats, archetype, quote, scores)
    print("\nWrote stats.json, report.md, narrative_input.md, profile.html to", OUT_DIR)
    print(f"  archetype: {archetype}  scores: {scores}")
    print(f"  sources: " + ", ".join(f"{s}({source_files[s]}f/{len(source_sessions[s])}s)"
                                      for s in sorted(source_files)))
    print(f"  sessions={total_sessions}  prompts={prompts_count}  tool_calls={tool_use_total}")
    print(f"  git churn={gc['churn']:,} lines (gold std, {gc['repos_with_commits']}/{gc['repos_seen']} repos)  "
          f"vs tool-only={total_churn:,}  git velocity={git_velocity:.0f} ln/hr")
    print(f"  iteration depth: mean {iteration_mean:.1f} / max {iteration_max} ({heavy_files} files >15x)  "
          f"errors={tool_errors} ({error_rate_per_100_tools:.1f}/100 tools)")
    print(f"  autonomy={autonomy_score}/100  planning_ratio={planning_ratio:.2f}")


def bar(n, mx, width=28):
    if mx <= 0:
        return ""
    return "█" * max(1, round(n / mx * width)) if n else ""


def write_report(s):
    L = []
    A = L.append
    c = s["corpus"]; v = s["volume"]; t = s["tools"]; vel = s["velocity"]
    b = s["behavior"]; r = s["rhythm"]; st = s["stack"]; au = s["autonomy"]
    A("# Local Paxel — Builder Stats Report\n")
    A(f"_Scope: {s['scope']}. Generated entirely on-device — nothing uploaded._\n")
    A("## Corpus")
    if c.get("sources"):
        A("- Sources: " + ", ".join(
            f"**{name}** ({d['files']} files, {d['sessions']} sessions, {d['prompts']:,} prompts)"
            for name, d in c["sources"].items()))
    A(f"- Transcripts parsed: **{c['files_parsed']}** ({c['lines_total']:,} events, "
      f"{c['lines_unparseable']} unparseable)")
    A(f"- Date range: **{_d10(c['date_range'][0])} → {_d10(c['date_range'][1])}** "
      f"({c['span_days']} days span, **{c['active_days']} active days**)")
    A(f"- Timezone: {c['timezone']}\n")
    A("## Volume")
    A(f"- Sessions: **{v['total_sessions']}**")
    A(f"- Genuine prompts (human-typed): **{v['total_prompts']:,}**  "
      f"(+{v['command_invocations']} slash-command invocations)")
    A(f"- Avg prompt length: **{v['avg_prompt_length_chars']:.0f} chars** "
      f"(median {v['median_prompt_length_chars']:.0f})")
    A(f"- Assistant turns: {v['assistant_turns']:,} · tool calls: **{v['tool_calls_total']:,}** "
      f"· thinking blocks: {v['thinking_blocks']:,}\n")
    A("## Tools")
    A(f"- Tool diversity: **{t['tool_diversity']} distinct tools** "
      f"(normalized entropy {t['tool_entropy_normalized']})")
    A(f"- MCP share: **{t['mcp_share']*100:.0f}%** ({t['mcp_calls']:,} MCP / {t['native_calls']:,} native)")
    A("- Top tools:")
    mx = t["top_tools"][0][1] if t["top_tools"] else 1
    for name, cnt in t["top_tools"]:
        A(f"  - `{name}` · {cnt:,} {bar(cnt, mx)}")
    A(f"- Category mix: {t['category_breakdown']}\n")
    A("## Code velocity")
    A(f"- **Git churn (gold standard): {vel['git_churn_total']:,} lines** "
      f"(+{vel['git_insertions']:,} / -{vel['git_deletions']:,}) across {vel['git_commits_real']:,} commits "
      f"in {vel['git_repos_with_commits']}/{vel['git_repos_seen']} repos on disk")
    A(f"  - **{vel['git_velocity_lines_per_hour']:.0f} lines/hour** over {vel['active_hours']:,} active hours")
    if vel.get("git_per_repo"):
        A("  - By repo: " + ", ".join(f"{n} ({i+d:,})" for n, i, d, _c in vel["git_per_repo"][:6]))
    _gtot, _ttot = vel['git_churn_total'], max(vel['tool_churn_edit_write'], 1)
    _missing = vel['git_repos_seen'] - vel['git_repos_with_commits']
    if _missing > 0:
        _cov = (f" — note this is **partial**: only {vel['git_repos_with_commits']} of "
                f"{vel['git_repos_seen']} repos were counted (the rest are missing from disk, have no "
                f"commits under your git email, or were too large to scan in time). "
                f"The Execution score nudges its throughput term up modestly (≤1.4×) to avoid "
                f"penalizing you for repos paxel couldn't read")
    else:
        _cov = ""
    A(f"- Tool-only churn (Edit/Write — what most profilers see): {vel['tool_churn_edit_write']:,} lines. "
      f"Git/tool ratio: **{_gtot/_ttot:.1f}×**{_cov}")
    A(f"- Shell-authored work the Edit/Write path misses entirely: {vel['shell_write_calls']:,} file-writing Bash "
      f"calls, ~{vel['shell_authored_lines_est']:,} lines of heredoc/redirect content\n")
    A("## Behavior")
    A(f"- Planning ratio (explore : doing): **{b['planning_ratio_explore_to_doing']}** "
      f"(explore {b['explore_actions']:,} vs doing {b['produce_actions']+b['execute_actions']+b['delegate_actions']:,})")
    A(f"- Avg session: **{b['avg_session_minutes']:.0f} min** (median {b['median_session_minutes']:.0f})")
    A(f"- Errors: **{b['tool_errors']:,} tool errors** ({b['error_rate_per_100_tools']} per 100 tool calls); "
      f"{b['recovered_errors']:,} recovered ({b['error_recovery_ratio']*100:.0f}%); {b['api_errors_retries']} API retries")
    A(f"- Iteration depth (edits/file before commit): mean **{b['iteration_depth_mean']:.1f}**, "
      f"median {b['iteration_depth_median']:.0f}, p90 {b['iteration_depth_p90']}, "
      f"**max {b['iteration_depth_max']}** — {b['files_hammered_over_15x']} files hammered >15× in one session")
    A(f"- Actions per prompt: **{b['actions_per_prompt']:.1f}** · "
      f"questions asked: {b['questions_asked']} · background: {b['background_tasks']} · scheduled: {b['scheduled_actions']}\n")
    A("## Rhythm")
    A(f"- Peak hours (local): **{', '.join(f'{h:02d}:00' for h in r['peak_hours_local'])}**")
    A(f"- Preferred days: **{', '.join(r['preferred_days'])}**")
    A("- Hours:")
    hh = r["hour_histogram_local"]; hmx = max(hh.values()) if hh else 1
    for h in range(24):
        n = hh.get(str(h), 0)
        A(f"  - {h:02d} {bar(n, hmx, 24)} {n}")
    A("- Days:")
    wd = r["weekday_histogram"]; wmx = max(wd.values()) if wd else 1
    for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]:
        n = wd.get(d, 0)
        A(f"  - {d} {bar(n, wmx, 24)} {n}")
    A("")
    A("## Stack")
    A(f"- Models: {', '.join(f'{m} ({n})' for m, n in st['models'][:6])}")
    A(f"- Top skills: {', '.join(f'{k} ({n})' for k, n in st['top_skills'][:10]) or '—'}")
    A(f"- Subagent types: {', '.join(f'{k} ({n})' for k, n in st['subagent_types']) or '—'}")
    A("- Top projects (events, sessions):")
    for name, cnt, sess in st["top_projects"]:
        A(f"  - {name} · {cnt:,} events · {sess} sessions")
    A("")
    A("## Autonomy")
    A(f"- **Autonomy score: {au['autonomy_score_0_100']}/100**")
    A(f"- Components: {au['components']}")
    with open(os.path.join(OUT_DIR, "report.md"), "w") as f:
        f.write("\n".join(L))


def write_narrative_input(s, opening_prompts, longest_prompts):
    L = []
    A = L.append
    A("# Narrative input (LOCAL ONLY — for the archetype/traits pass)\n")
    A("Full metrics:\n```json")
    A(json.dumps(s, indent=2, default=str))
    A("```\n")
    A("## Opening prompts (first human message per session — characteristic asks)\n")
    op = [p for p in opening_prompts if p[0] is not None]
    op.sort(key=lambda x: x[0])
    # spread a sample across the timeline
    sample = op[:: max(1, len(op) // 60)] if op else []
    for dt, proj, text in sample[:60]:
        A(f"- [{dt.date()} · {proj}] {text.replace(chr(10), ' ')[:280]}")
    A("\n## Longest prompts (most detailed specs)\n")
    longest_prompts.sort(key=lambda x: -x[0])
    for ln, proj, text in longest_prompts[:20]:
        A(f"- [{ln} chars · {proj}] {text.replace(chr(10), ' ')[:280]}")
    with open(os.path.join(OUT_DIR, "narrative_input.md"), "w") as f:
        f.write("\n".join(L))


# ---------------------------------------------------------------------------
# User-facing profile: a transparent rubric turns the measured metrics into an
# archetype + 0-10 scores (no LLM needed), then we emit a branded, shareable
# profile.html. The COUNTS are measured; the scores/archetype are a rubric and
# the report says so. narrative_input.md is still written for optional LLM polish.
#
# The four score axes are NOT an arbitrary rubric — each one is grounded in
# Garry Tan's open-source gstack (github.com/garrytan/gstack), the same
# Garry-Tan-world framework YC's Paxel comes out of. gstack frames building as a
# sprint — Think → Plan → Build → Review → Test → Ship → Reflect — on top of
# three ethos pillars: "Boil the Lake" (completeness is cheap, do the complete
# thing), "Search Before Building" (know what exists first), and "User
# Sovereignty" (AI recommends, the human decides — and per Anthropic's own
# research, experts interrupt MORE, not less). Each axis below maps a slice of
# that framework onto the metrics paxel can honestly measure from transcripts.
# ---------------------------------------------------------------------------
REPO_URL = "https://github.com/Photobombastic/paxel-local"

# Plain-language explanation shown under each score bar — what the axis measures, in
# human terms, no jargon. (The gstack grounding lives in the disclaimer + README, not here.)
SCORE_NOTES = {
    "Execution": "How much you ship, and how fast — committed code, work you hand off to "
                 "agents, and long focused build sessions.",
    "Planning": "How much you think before you build — reading and exploring before writing, "
                "and laying out a plan first.",
    "Steering": "How hands-on you stay — interrupting, redirecting, and asking questions "
                "instead of letting the agent run unchecked.",
    "Engineering": "How clean your work is — focused changes, not re-editing the same file "
                   "over and over, and checking your work.",
    "Product Instinct": "Whether you rethink the problem before building it — brainstorming and "
                        "questioning the ask, not just doing the ticket. ⚠ Our softest read — "
                        "transcripts barely show this.",
}


def _clamp(x):
    return max(0.0, min(1.0, x))


def _d10(x):
    """First 10 chars of an ISO date, or '—' when missing (empty/timestampless corpus)."""
    return (x or "")[:10] or "—"


def _skill_uses(stats, needle):
    return sum(n for k, n in stats["stack"].get("top_skills", []) if needle in k.lower())


def _skill_uses_any(stats, needles):
    return sum(n for k, n in stats["stack"].get("top_skills", [])
               if any(nd in k.lower() for nd in needles))


def compute_scores(stats):
    # Five axes, each DERIVED FROM Garry Tan's gstack (see the module note above) —
    # one paxel subagent per axis read the real gstack role/skill definitions and
    # mapped them onto the metrics paxel can honestly measure. Weights sum to 1.0
    # per axis; every term is clamped to 0..1 against a justified, gstack-anchored
    # target. The skill-ceremony terms match the BEHAVIOR gstack prescribes
    # (plan/review/qa/investigate), detected via whatever skills implement it —
    # gstack's own command names plus the wider ecosystem's equivalents.
    #
    # The 5th axis, "Product Instinct", is DELIBERATELY THE SOFTEST — coding transcripts
    # barely reveal product judgment, so we proxy it from reframe-before-build skill use +
    # premise-challenging questions and flag it as soft on the card itself, rather than
    # either faking a confident number or dropping it. Show the seams, don't fake them.
    v, b, vel = stats["volume"], stats["behavior"], stats["velocity"]
    if v["total_sessions"] == 0 or v["tool_calls_total"] == 0:
        # No real activity → don't manufacture a flattering "Quality Guardian 9.0"
        return {"Execution": 0.0, "Planning": 0.0, "Steering": 0.0, "Engineering": 0.0,
                "Product Instinct": 0.0}
    sess = max(v["total_sessions"], 1)
    prompts = max(v["total_prompts"], 1)
    hours = max(vel["active_hours"], 0.1)

    # EXECUTION — gstack's BUILD phase + the "Golden Age" ethos: shipped output at
    # AI leverage. The throughput signal is gold-standard git churn (what actually
    # shipped, not tool-churn which inflates via iteration — Garry himself disclaims
    # raw LOC). But git often sees only some of a corpus's repos (non-git dirs,
    # cwd→repo gaps), so we nudge the rate up by git coverage rather than penalize a
    # builder for repos paxel couldn't read. The boost is deliberately MODEST — floored
    # at 0.7, i.e. at most ~1.4× — because the unseen repos may not have shipped at the
    # same rate; we nudge, we don't extrapolate, and the report DISCLOSES when this
    # correction fired (honesty > flattery). Normalized per active hour so a long corpus
    # can't game it; clamped so a brute-forcer can't run it to 10.
    git_cov = max(vel["git_repos_with_commits"] / max(vel["git_repos_seen"], 1), 0.7)
    eff_git_ln_per_hr = (vel["git_churn_total"] / hours) / git_cov
    execution = 10 * (
        0.35 * _clamp(eff_git_ln_per_hr / 400)                            # shipped git output rate, coverage-corrected
        + 0.30 * _clamp((b["delegate_actions"] + b["background_tasks"]) / max(prompts * 0.3, 1))  # delegation/parallelism
        + 0.20 * _clamp(b["actions_per_prompt"] / 12)                     # leverage: work per human prompt
        + 0.15 * _clamp(b["avg_session_minutes"] / 75))                   # sustained build sessions

    # PLANNING — gstack's THINK+PLAN phases + "Search Before Building": explore and
    # search before writing, run plan/spec ceremony, reason deeply before deciding.
    plan_skills = _skill_uses_any(stats, ("brainstorm", "writing-plan", "plan", "spec",
                                          "office-hours", "autoplan", "grill", "ceo-review",
                                          "eng-review", "design-review"))
    planning = 10 * (
        0.35 * _clamp(b["planning_ratio_explore_to_doing"] / 0.65)        # explore-to-doing: think before build
        + 0.25 * _clamp(plan_skills / 8.0)                                # plan/spec ceremony (gstack's defining layer)
        + 0.20 * _clamp((v["thinking_blocks"] / sess) / 12.0)            # reasoning depth per session
        + 0.15 * _clamp((b["explore_actions"] / max(b["produce_actions"] + b["execute_actions"], 1)) / 1.5)  # search before building
        + 0.05 * _clamp(v["avg_prompt_length_chars"] / 500.0))           # forcing-question depth (substantive prompts)

    # STEERING — gstack's "User Sovereignty": AI recommends, the human decides.
    # Per the Anthropic finding gstack cites, experts interrupt MORE, not less.
    # Anchored to RATIOS (per-prompt) so verbosity ≠ steering. Avoids
    # error_recovery_ratio (saturates ~1.0 for everyone → no signal).
    steer_skills = _skill_uses_any(stats, ("review", "careful", "investigate",
                                           "office-hours", "code-review"))
    steering = 10 * (
        0.38 * _clamp((15 - b["actions_per_prompt"]) / 11)               # hands-on cadence: fewer actions/prompt = more in the loop
        + 0.32 * _clamp((10 - b["iteration_depth_p90"]) / 8)             # break-in: lower p90 = redirects sooner
        + 0.22 * _clamp((b["questions_asked"] / prompts) / 0.05)         # interrogation rate (per prompt, not raw count)
        + 0.08 * _clamp(steer_skills / max(sess * 0.5, 1)))             # staying in the verification loop

    # ENGINEERING — gstack's "Boil the Lake" + Review/Test/Reflect: clean low-rework
    # changes plus evidence of the quality ceremonies. NOTE: paxel can't see test
    # COVERAGE from transcripts, so completeness is proxied by (a) ceremony-skill use
    # and (b) low rework (changes that didn't need hammering). Ceremony weight trimmed
    # to 0.22 (from the axis author's 0.30) so the axis isn't dominated by named-skill
    # detection for users who don't run skills.
    churn_back = vel["git_deletions"] / max(vel["git_insertions"], 1)
    eng_skills = _skill_uses_any(stats, ("review", "test", "tdd", "qa", "investigate",
                                         "retro", "learn", "cso", "karpathy", "debug"))
    engineering = 10 * (
        0.28 * (1 - _clamp((churn_back - 0.20) / 0.40))                  # low rework: some deletion healthy, lots = thrash
        + 0.22 * (1 - _clamp((b["iteration_depth_p90"] - 3) / 9))        # clean iteration: low typical depth = lands right
        + 0.18 * (1 - _clamp((b["files_hammered_over_15x"] / sess) / 0.25))  # focused: few hammered files
        + 0.22 * _clamp((eng_skills / sess) / 3.0)                       # Boil-the-Lake ceremonies: review/qa/investigate/retro
        + 0.10 * (1 - _clamp(b["error_rate_per_100_tools"] / 10)))       # low error rate: root-cause discipline

    # PRODUCT INSTINCT — gstack's CEO / office-hours layer: do you rethink the PRODUCT
    # before building it (reframe the request, challenge premises, find the 10-star
    # version) instead of just executing the ticket as written? This is the SOFTEST axis
    # by design — coding transcripts barely see product judgment — so we proxy it from
    # the gstack "reframe before build" skills (brainstorm / office-hours / ceo-review /
    # design / cso / spec), premise-challenging questions, and how broadly you build.
    # The scorecard flags it as the softest-signal axis so we stay honest about it.
    reframe = _skill_uses_any(stats, ("brainstorm", "office-hours", "ceo-review", "cso",
                                      "design-consultation", "design-shotgun", "spec", "grill"))
    breadth = len(stats["stack"].get("top_projects", []))
    product = 10 * (
        0.50 * _clamp((reframe / sess) / 1.5)                            # reframe-before-build ceremony
        + 0.30 * _clamp((b["questions_asked"] / prompts) / 0.05)        # challenge the premise (ask, don't assume)
        + 0.10 * _clamp(breadth / 8)                                     # build across many products
        + 0.10 * _clamp(b["planning_ratio_explore_to_doing"] / 0.65))   # explore the problem space first

    return {"Execution": round(execution, 1), "Planning": round(planning, 1),
            "Steering": round(steering, 1), "Engineering": round(engineering, 1),
            "Product Instinct": round(product, 1)}


def pick_archetype(stats, scores):
    b, vel, r = stats["behavior"], stats["velocity"], stats["rhythm"]
    peak = (r["peak_hours_local"] or [12])[0]
    brute = b["iteration_depth_max"] >= 40 or (vel["shell_authored_lines_est"] > 50000
                                               and b["error_rate_per_100_tools"] >= 3)
    plan_hi, exec_hi, eng_hi = scores["Planning"] >= 7.5, scores["Execution"] >= 8, scores["Engineering"] >= 7.5
    night = peak >= 22 or peak <= 4
    if plan_hi and brute:
        name, q = "Brute-Force Architect", "You plan and scaffold like an architect — then grind the hard parts by hand, in the shell, until they work."
    elif plan_hi:
        name, q = "The Architect", "You plan first, codify your decisions, and build scaffolding that compounds."
    elif exec_hi and brute:
        name, q = "The Bulldozer", "You point yourself at the problem and push through it until it gives."
    elif exec_hi:
        name, q = "Velocity Machine", "You move fast, delegate hard, and keep a lot of plates spinning at once."
    elif eng_hi:
        name, q = "Quality Guardian", "You keep churn low and the bar high — measured changes, reviewed twice."
    elif night:
        name, q = "Night Owl", "Your best work lands after dark, in long uninterrupted runs."
    else:
        name, q = "The Builder", "Steady, pragmatic, and tool-driven — you just keep shipping."
    return name, q


def signature_moves(stats):
    """Named decision-patterns ('signature moves') drawn from real session behavior,
    each tagged with the gstack sprint stage it expresses. Only moves whose gate
    actually fires are returned (we never pad) — top 5 by a comparable 0..1 strength.
    Cites measured numbers, NEVER raw prompt text, so the profile stays shareable
    without leaking session content. NOTE for maintainers: evidence HTML is trusted /
    safe-by-construction — never interpolate user/transcript-derived strings (skill,
    project, tool names) here without html.escape; today every value is a number or a
    static template (the lone tool-name use is gated to == "Bash" and emits a literal)."""
    v, b, vel, t, st = (stats["volume"], stats["behavior"], stats["velocity"],
                        stats["tools"], stats["stack"])
    sess = max(v["total_sessions"], 1)
    prompts = max(v["total_prompts"], 1)

    def sk(*needles):
        return sum(n for k, n in st.get("top_skills", []) if any(nd in k.lower() for nd in needles))

    top_tool = (str(t["top_tools"][0][0]) if t["top_tools"] else "")
    deleg = b["delegate_actions"] + b["background_tasks"]
    pool = []   # (strength 0..1, gstack-tag, title, evidence_html)

    rev = sk("review", "code-review")
    if rev >= 50 and rev >= sess * 0.5:
        pool.append((_clamp(rev / (sess * 2)), "Review",
            "You review more than you write",
            f'<b>{rev:,}</b> code-review passes — one of your most-used skills. '
            f'You don\'t trust a diff until a second set of eyes has seen it.'))

    if b["planning_ratio_explore_to_doing"] >= 0.55 and b["iteration_depth_max"] >= 40:
        pool.append((_clamp(b["iteration_depth_max"] / 100.0), "Think → Build",
            "Plan wide, then grind narrow",
            f'A <b>{b["planning_ratio_explore_to_doing"]:.2f}</b> explore-to-build ratio — you read and '
            f'search far more than you type — yet you\'ll hammer one file <b>{b["iteration_depth_max"]}×</b> '
            f'rather than re-architect. Blueprint, then bulldozer.'))

    if deleg >= 100 and deleg >= prompts * 0.3:
        shell = " with the shell as your top tool" if top_tool == "Bash" else ""
        pool.append((_clamp(deleg / (prompts * 0.8)), "Build",
            "You run a team, not a tool",
            f'<b>{deleg:,}</b> delegated &amp; backgrounded agent runs{shell}. '
            f'You parallelize and grind rather than babysit one chat.'))

    tb = v["thinking_blocks"]
    if tb / sess >= 8:
        pool.append((_clamp((tb / sess) / 30.0), "Think",
            "You think before you touch the diff",
            f'<b>{tb:,}</b> reasoning blocks (~{tb // sess}/session) before edits land — '
            f'you deliberate hard, then commit.'))

    plan = sk("brainstorm", "writing-plan", "autoplan", "spec")
    if plan >= 30 and plan >= sess * 0.35:
        pool.append((_clamp(plan / float(sess)), "Plan",
            "You write the plan before the code",
            f'<b>{plan:,}</b> planning &amp; brainstorming runs — you scaffold the decision '
            f'before the implementation, gstack-style.'))

    qrate = b["questions_asked"] / prompts
    if qrate < 0.03 and prompts > 200:
        pool.append((0.45, "User Sovereignty",
            "You direct, you don't deliberate",
            f'You asked the agent a question on just <b>{qrate*100:.0f}%</b> of {prompts:,} prompts — '
            f'you steer by command, not by committee.'))

    if vel["shell_authored_lines_est"] >= 20000 and top_tool == "Bash":
        pool.append((_clamp(vel["shell_authored_lines_est"] / 80000.0), "Build",
            "You live in the shell",
            f'~<b>{vel["shell_authored_lines_est"]:,}</b> lines authored through Bash heredocs and '
            f'redirects — real work most profilers never even see.'))

    pool.sort(key=lambda x: -x[0])
    return [(tag, title, ev) for _, tag, title, ev in pool[:5]]


def growth_edges(stats, scores):
    """Specific next-steps keyed off the user's OWN weakest signals — not generic advice.
    Each leads with a PRACTICE the reader can adopt today, then names the gstack skill
    that embodies it (in parens) as an optional, installable upgrade — so the advice is
    actionable whether or not they run gstack. Only gated edges are returned; top 3,
    most-urgent first. NOTE for maintainers: advice HTML is trusted/safe-by-construction
    — never interpolate user/transcript-derived strings (skill, project, tool names)
    here without html.escape; today every interpolated value is a number or static."""
    v, b, vel, st = (stats["volume"], stats["behavior"], stats["velocity"], stats["stack"])
    sess = max(v["total_sessions"], 1)
    prompts = max(v["total_prompts"], 1)

    def sk(*needles):
        return sum(n for k, n in st.get("top_skills", []) if any(nd in k.lower() for nd in needles))

    lowest = min(scores, key=scores.get) if scores else ""
    qrate = b["questions_asked"] / prompts
    rev = sk("review", "code-review")
    tdd = sk("test", "tdd", "qa")
    pool = []   # (priority: lower = more urgent / shows first, eyebrow, title, advice_html)

    # Gate on the Steering SCORE (not question-rate alone) so a high-Steering user who
    # simply steers via short commands is never told to "steer harder."
    if scores.get("Steering", 10) < 7:
        lead = (f'Steering is your lowest axis at <b>{scores.get("Steering")}</b>'
                if lowest == "Steering" else f'Steering sits at <b>{scores.get("Steering")}</b>')
        pool.append((1.0, "Steer harder",
            "Interrupt during, not just review after",
            f'{lead} — you questioned the agent on only <b>{qrate*100:.0f}%</b> of prompts. '
            f'Break in on risky steps and redirect long chains <i>while</i> they run, instead of only '
            f'reviewing after. Experts interrupt more, not less. (gstack names this guardrail <code>/careful</code>.)'))

    if rev >= 50 and tdd < max(rev * 0.1, 5):
        pool.append((1.5, "Add a reflex",
            "Pair your review reflex with a test reflex",
            f'<b>{rev:,}</b> code-reviews vs <b>{tdd}</b> test runs. Make the double-check a <i>regression '
            f'test</i>: write one for every bug you fix before you move on. Tests are the cheapest thing to '
            f'add with AI. (gstack\'s <code>/qa</code> does this automatically.)'))

    if b["iteration_depth_max"] >= 40 or b["files_hammered_over_15x"] >= 10:
        pool.append((2.0, "Stop the grind",
            "Root-cause instead of whack-a-mole",
            f'<b>{b["iteration_depth_max"]}×</b> on one file, <b>{b["files_hammered_over_15x"]}</b> files past '
            f'15 edits. When a file resists past ~15 tries, stop and find the root cause before the next edit '
            f'instead of re-trying. (gstack names this discipline <code>/investigate</code> — no fixes without investigation.)'))

    if scores.get("Planning", 10) < 6:
        pool.append((scores.get("Planning", 10), "Plan first",
            "Spend more time in Think + Plan",
            f'Planning is <b>{scores.get("Planning")}</b>. Sketch the plan and reframe the ask <i>before</i> '
            f'writing code — it\'s the cheapest place to catch a wrong turn. '
            f'(gstack front-loads this with <code>/office-hours</code> + <code>/autoplan</code>.)'))

    eng_skills = sk("review", "qa", "investigate", "retro")
    if scores.get("Engineering", 10) < 6 and eng_skills < sess * 0.3:
        pool.append((scores.get("Engineering", 10) + 0.1, "Boil the lake",
            "Run a quality pass before you ship",
            f'Engineering is <b>{scores.get("Engineering")}</b>. Add one deliberate review-and-test pass on '
            f'every branch before you ship — that\'s where craft compounds. '
            f'(gstack\'s back half: <code>/review</code>, <code>/qa</code>, <code>/investigate</code>, <code>/retro</code>.)'))

    if scores.get("Product Instinct", 10) < 5:
        pool.append((scores.get("Product Instinct", 10) + 0.2, "Reframe first",
            "Challenge the ask before you build it",
            f'Product Instinct is <b>{scores.get("Product Instinct")}</b> (our softest axis). Before building, '
            f'write down the real problem and 2–3 alternatives — catch the wrong product before you ship the '
            f'right code for it. (gstack\'s <code>/office-hours</code> runs six forcing questions for exactly this.)'))

    if not pool:
        pool.append((9.0, "Go deeper",
            "You're balanced — your edge is depth",
            'You\'re even across the build sprint, so the next gear isn\'t a weak spot to patch — it\'s depth. '
            'Add a short retro after each session and let the learnings compound session over session. '
            '(gstack names this <code>/retro</code> — the Reflect stage.)'))

    pool.sort(key=lambda x: x[0])
    return [(eb, title, adv) for _, eb, title, adv in pool[:3]]


def _pretty_model(m):
    # "claude-opus-4-7" -> "Opus 4.7"; "claude-3-5-sonnet-20241022" -> "Sonnet 3.5"
    m = re.sub(r"^claude-", "", m or "")
    m = re.sub(r"-\d{6,}$", "", m)              # drop trailing date
    parts = [p for p in m.split("-") if p]
    words = [p for p in parts if not p.isdigit()]
    nums = [p for p in parts if p.isdigit()]
    name = (words[0].upper() if words and len(words[0]) <= 3 else
            words[0].capitalize()) if words else (m or "?")
    ver = ".".join(nums[:2])
    return f"{name} {ver}".strip() if ver else name


def _img_data_uri(path):
    try:
        import base64
        with open(path, "rb") as fh:
            return "data:image/png;base64," + base64.b64encode(fh.read()).decode()
    except Exception:
        return ""


_PROFILE_CSS = """<style>
  :root{--slate:#313941;--beak:#ED7379;--beak-deep:#D14E57;--bg:#eef1f3;--panel:#fff;
    --line:#d9dee2;--text:#16191d;--muted:#5e6a73;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --display:"Josefin Sans","Futura","Century Gothic","Trebuchet MS",sans-serif;
    --serif:"Merriweather",Georgia,"Times New Roman",serif;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.5;-webkit-font-smoothing:antialiased}
  #report{background:var(--bg)} .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  a{color:var(--slate);text-decoration:none} a:hover{text-decoration:underline}
  .wrap{max-width:900px;margin:0 auto;padding:0 22px 70px}
  .topbar{background:var(--panel);border-bottom:1px solid var(--line);padding:13px 0}
  .topbar .wrap{display:flex;align-items:center;gap:12px;padding:0}
  .brandlink{display:flex;align-items:center;gap:12px;color:var(--text)} .brandlink:hover{text-decoration:none;color:var(--beak-deep)}
  .chip{width:40px;height:40px;flex:0 0 auto;background:#fff;border:1px solid var(--line);border-radius:9px;display:flex;align-items:center;justify-content:center}
  .chip img{width:32px;height:32px;object-fit:contain}
  .brand{font-family:var(--display);font-weight:700;font-size:20px;letter-spacing:.05em} .brand .dim{opacity:.6;font-weight:600;font-size:15px}
  .badge{margin-left:auto;font-size:12px;font-weight:600;color:var(--slate);background:#e8edf0;padding:5px 11px;border-radius:999px;border:1px solid var(--line)}
  .hero{padding:54px 0 30px} .eyebrow{color:var(--muted);font-size:14px;margin:0 0 16px}
  .hero h1{font-family:var(--serif);font-size:50px;line-height:1.06;margin:0 0 8px;font-weight:700;letter-spacing:-.01em} .hero h1 .accent{color:var(--beak)}
  .hero .quote{font-family:var(--serif);font-size:19px;font-style:italic;color:#3b444b;margin:18px 0 0;max-width:660px;line-height:1.55}
  .hero .sub{color:var(--muted);margin-top:18px;font-size:15px;max-width:680px} .hero .sub b{color:var(--text)}
  .stat-strip{display:flex;flex-wrap:wrap;gap:24px;margin-top:28px;padding-top:24px;border-top:1px solid var(--line)}
  .stat-strip div{display:flex;flex-direction:column} .stat-strip .n{font-family:var(--serif);font-size:25px;font-weight:700} .stat-strip .l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
  .share{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:24px} .share .lbl{font-size:13px;color:var(--muted)}
  .btn{display:inline-flex;align-items:center;gap:8px;padding:9px 15px;border-radius:999px;cursor:pointer;font-weight:600;font-size:14px;color:#fff;border:1px solid transparent;font-family:var(--sans)}
  .btn:hover{text-decoration:none;opacity:.9} .btn.x{background:#000} .btn.ghost{background:#fff;color:var(--slate);border-color:var(--line)}
  .btn svg{width:15px;height:15px} .btn.x svg{fill:#fff}
  h2.section{font-family:var(--display);font-size:14px;text-transform:uppercase;letter-spacing:.18em;color:var(--slate);margin:54px 0 14px;font-weight:700}
  p.lead{color:var(--muted);font-size:14.5px;margin:-4px 0 20px;max-width:700px;line-height:1.55}
  .card code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;background:#eef1f3;color:var(--beak-deep);padding:1px 5px;border-radius:4px}
  .disclaimer{background:#fff;border:1px solid var(--line);border-left:4px solid var(--beak);border-radius:6px;padding:14px 16px;margin:-6px 0 24px;font-size:13.5px;color:#48535b;line-height:1.55} .disclaimer b{color:var(--text)}
  .score{display:grid;grid-template-columns:160px 1fr 46px;align-items:center;gap:14px;margin:0 0 14px} .score .name{font-weight:600;font-size:15px}
  .score .track{height:12px;background:#dde2e6;border-radius:999px;overflow:hidden} .score .fill{height:100%;background:linear-gradient(90deg,var(--beak-deep),var(--beak));border-radius:999px}
  .score .val{font-weight:800;text-align:right} .score .note{grid-column:1/-1;color:var(--muted);font-size:13px;margin:-6px 0 4px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px 18px 16px;box-shadow:0 1px 2px rgba(20,30,40,.04)} .card.flag{border-left:4px solid var(--beak)}
  .card .q{color:var(--beak-deep);font-size:12.5px;font-weight:700;margin:0 0 8px;text-transform:uppercase;letter-spacing:.03em}
  .card .a{font-family:var(--serif);font-size:19px;font-weight:700;margin:0 0 6px} .card .d{color:var(--muted);font-size:13.5px;margin:0}
  footer{margin-top:54px;padding-top:22px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;line-height:1.7} footer .lock{color:var(--beak-deep);font-weight:700} footer .by{color:var(--text)}
</style>"""


def _card(q, a, d, flag=False):
    # q/a/d are injected RAW (no escaping) so callers can use intentional <b>/<code>/<i>
    # markup. Every caller must pass safe-by-construction strings: numbers, static
    # templates, or html.escape()'d values — NEVER raw user/transcript-derived text.
    cls = "card flag" if flag else "card"
    return f'<div class="{cls}"><p class="q">{q}</p><p class="a">{a}</p><p class="d">{d}</p></div>'


def write_profile_html(stats, archetype, quote, scores):
    import html as _h
    v, vel, b, r, t, st, c = (stats["volume"], stats["velocity"], stats["behavior"],
                              stats["rhythm"], stats["tools"], stats["stack"], stats["corpus"])
    logo = _img_data_uri(os.path.join(OUT_DIR, "tern.png"))
    chip = f'<span class="chip"><img src="{logo}" alt="Roadmap tern"></span>' if logo else ""

    peak = (r["peak_hours_local"] or [12])[0]
    tod = ("Night owl" if (peak >= 22 or peak <= 4) else "Morning person" if peak <= 11
           else "Daytime builder" if peak <= 16 else "Dusk builder")
    wd = r["weekday_histogram"]
    wknd = wd.get("Sat", 0) + wd.get("Sun", 0)
    wkday_avg = sum(wd.get(d, 0) for d in ["Mon", "Tue", "Wed", "Thu", "Fri"]) / 5 or 1
    weekend_a = "No days off" if wknd / 2 >= wkday_avg * 0.6 else "Weekday warrior"
    models = st.get("models", [])
    mtot = sum(n for _, n in models) or 1
    model_a = _h.escape(" → ".join(_pretty_model(m) for m, _ in models[:2]) or "—")
    model_d = _h.escape((", ".join(f"{_pretty_model(m)} {round(n/mtot*100)}%" for m, n in models[:2]) + " of turns.") if models else "—")
    top_tool = (t["top_tools"][0] if t["top_tools"] else ["—", 0])
    top_tool_name = _h.escape(str(top_tool[0]))
    sess = max(v["total_sessions"], 1)
    per_sess = round(b["delegate_actions"] / sess, 1)
    git_pct = f'{vel["git_repos_with_commits"]}/{vel["git_repos_seen"]}'

    cards = [
        _card("How much did you ship?", "Three numbers, honestly",
              f'{vel["tool_churn_edit_write"]:,} lines via Edit/Write, ~{vel["shell_authored_lines_est"]:,} more in the shell — '
              f'but git-committed (gold standard) only <b>{vel["git_churn_total"]:,}</b> across {git_pct} repos on disk.', flag=True),
        _card("Iteration depth", f'{b["iteration_depth_max"]}× on one file',
              f'Max edits to a single file in one session; {b["files_hammered_over_15x"]} files >15×. Mean is just {b["iteration_depth_mean"]}.',
              flag=b["iteration_depth_max"] >= 40),
        _card("Course-correction", f'{b["tool_errors"]:,} errors, {round(b["error_recovery_ratio"]*100)}% recovered',
              f'{b["error_rate_per_100_tools"]} errors per 100 tool calls — recovered on the fly.'),
        _card("Model of choice", model_a, model_d),
        _card("Most productive", tod, f'Peak activity around {peak:02d}:00 local.'),
        _card("Weekends?", weekend_a, f'Busiest day: {(r["preferred_days"] or ["—"])[0]}.'),
        _card("Prompt length", "Two gears" if v["avg_prompt_length_chars"] > v["median_prompt_length_chars"]*2 else "Consistent",
              f'Median {v["median_prompt_length_chars"]:.0f} chars, mean {v["avg_prompt_length_chars"]:.0f}.'),
        _card("Agents run", f'{b["delegate_actions"]:,} subagents',
              f'~{per_sess} per session, plus {b["background_tasks"]:,} background &amp; {b["scheduled_actions"]} scheduled.'),
        _card("Tool of choice", top_tool_name, f'{top_tool[1]:,} calls — your most-used tool by far.'),
    ]

    score_rows = "".join(
        f'<div class="score"><span class="name">{name}</span>'
        f'<span class="track"><span class="fill" style="width:{val*10:.0f}%"></span></span>'
        f'<span class="val mono">{val}</span>'
        + (f'<span class="note">{_h.escape(SCORE_NOTES[name])}</span>' if name in SCORE_NOTES else "")
        + '</div>'
        for name, val in scores.items())

    moves = signature_moves(stats)
    edges = growth_edges(stats, scores)
    moves_html = "".join(_card(tag, title, ev) for tag, title, ev in moves)
    edges_html = "".join(_card(eb, title, adv, flag=True) for eb, title, adv in edges)

    # Data for the canvas-drawn 1200×630 share card (no foreignObject → no canvas taint
    # → the download actually works in every browser, not just Chromium).
    card_data = json.dumps({
        "arch": archetype,
        "quote": quote,
        "scores": [[k, val] for k, val in scores.items()],
        "stats": [[f'{v["total_sessions"]}', "sessions"],
                  [f'{v["total_prompts"]:,}', "prompts"],
                  [f'{v["tool_calls_total"]:,}', "tool calls"],
                  [f'{vel["git_churn_total"]:,}', "git lines"]],
        "logo": logo,
    })

    caption = ("My “how I build with AI” profile, computed 100% locally — nothing uploaded. "
               "Made with paxel-local, an MIT rebuild of YC's Paxel that keeps your sessions on your machine. "
               "Run your own: " + REPO_URL)

    eyebrow = (f'{v["total_sessions"]} sessions · {v["total_prompts"]:,} prompts · '
               f'{v["tool_calls_total"]:,} tool calls · {_d10(c["date_range"][0])} → {_d10(c["date_range"][1])}')

    parts = []
    P = parts.append
    P("<!DOCTYPE html>")
    P("<!-- Generated locally by paxel.py. Zero data left this machine. Counts measured; archetype/scores are a rubric. -->")
    P('<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">')
    P("<title>Builder Profile — Roadmap</title>")
    if logo:
        P(f'<link rel="icon" href="{logo}">')
    P(_PROFILE_CSS)
    P('</head><body><div id="report">')
    P('<div class="topbar"><div class="wrap">'
      f'<a class="brandlink" href="https://www.roadmap.chat/community" target="_blank" rel="noopener" title="Roadmap — find your flock">'
      f'{chip}<span class="brand">Roadmap <span class="dim">· Builder Profile</span></span></a>'
      '<span class="badge">🔒 Generated locally · nothing uploaded</span></div></div>')
    P('<div class="wrap"><section class="hero">')
    P(f'<p class="eyebrow">{eyebrow}</p>')
    P(f'<h1>You\'re a<br><span class="accent">{_h.escape(archetype)}.</span></h1>')
    P(f'<p class="quote">“{_h.escape(quote)}”</p>')
    P(f'<p class="sub">Plumbing: <b>{v["thinking_blocks"]:,} reasoning blocks</b> before the diff. '
      f'Grind: <b>{b["iteration_depth_max"]}× on one file</b>, <b>~{vel["shell_authored_lines_est"]:,} lines</b> in the shell, '
      f'<b>{b["tool_errors"]:,} errors</b> recovered. The counts are real; the verdict is a rubric.</p>')
    P('<div class="stat-strip">'
      f'<div><span class="n mono">{vel["git_churn_total"]:,}</span><span class="l">git lines (gold-std)</span></div>'
      f'<div><span class="n mono">{vel["tool_churn_edit_write"]:,}</span><span class="l">tool-authored lines</span></div>'
      f'<div><span class="n mono">~{vel["shell_authored_lines_est"]:,}</span><span class="l">shell-authored lines</span></div>'
      f'<div><span class="n mono">{b["iteration_depth_max"]}</span><span class="l">max edits, one file</span></div>'
      f'<div><span class="n mono">{b["delegate_actions"]:,}</span><span class="l">subagents</span></div></div>')
    P('<div class="share"><span class="lbl">Share:</span>'
      '<a id="share-x" class="btn x" href="#" target="_blank" rel="noopener">'
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231 5.45-6.231Zm-1.161 17.52h1.833L7.084 4.126H5.117L17.083 19.77Z"/></svg>Post on X</a>'
      '<button id="share-copy" class="btn ghost" type="button">📋 Copy caption</button>'
      '<button id="share-img" class="btn ghost" type="button">🖼 Download image</button></div>')
    P('</section><h2 class="section">Your scorecard</h2>')
    P('<div class="disclaimer"><b>How to read this.</b> The counts are <b>measured from your real transcripts and reproducible</b>. '
      'The 0–10 scores are a <b>transparent rubric grounded in <a href="https://github.com/garrytan/gstack" target="_blank" rel="noopener">Garry Tan\'s gstack</a></b> — '
      'each axis is derived from gstack\'s actual sprint (Think → Plan → Build → Review → Test → Ship → Reflect) and ethos, not an arbitrary scale. '
      'Paxel\'s own algorithm is closed, so this is a reasoned estimate, <b>not a replica</b>. Numbers = fact, scores = opinion.</div>')
    P(score_rows)
    if moves:
        P('<h2 class="section">Your signature moves</h2>')
        P('<p class="lead">The patterns in how you direct the AI — pulled from your real sessions, each '
          'tagged to the gstack sprint stage it expresses.</p>')
        P(f'<div class="grid">{moves_html}</div>')
    if edges:
        P('<h2 class="section">Your growth edge</h2>')
        P('<p class="lead">A few specific things to try next — keyed to your <i>own</i> weakest signals, not '
          'generic advice. Each is a habit you can adopt today; the <code>/commands</code> in parentheses are '
          'optional skills from <a href="https://github.com/garrytan/gstack" target="_blank" rel="noopener">gstack</a> '
          '(Garry Tan\'s toolkit) — the named, installable version of that habit if you want it.</p>')
        P(f'<div class="grid">{edges_html}</div>')
    P('<h2 class="section">What we noticed</h2><div class="grid">')
    P("".join(cards))
    P('</div>')
    P('<footer><span class="lock">🔒 Generated entirely on-device</span> by <span class="mono">paxel.py</span> — '
      'the same analysis Paxel runs, with zero data sent anywhere. Counts measured from your transcripts; '
      'archetype &amp; scores are a rubric. Raw metrics in <span class="mono">stats.json</span>.<br>'
      'Built by <a class="by" href="https://github.com/Photobombastic" target="_blank" rel="noopener">Max Schilling</a>, '
      '<a href="https://www.roadmap.chat/community" target="_blank" rel="noopener">Roadmap</a></footer>')
    P('</div></div>')
    P("<script>(function(){")
    P(f'var repo={json.dumps(REPO_URL)};var caption={json.dumps(caption)};')
    P('var x=document.getElementById("share-x");if(x)x.href="https://x.com/intent/tweet?text="+encodeURIComponent(caption);')
    P('var cb=document.getElementById("share-copy");if(cb)cb.addEventListener("click",function(){'
      'var d=function(){var o=cb.textContent;cb.textContent="✓ Copied";setTimeout(function(){cb.textContent=o;},1500);};'
      'if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(caption).then(d).catch(fb);}else{fb();}'
      'function fb(){var ta=document.createElement("textarea");ta.value=caption;document.body.appendChild(ta);ta.select();'
      'try{document.execCommand("copy");}catch(e){}document.body.removeChild(ta);d();}});')
    P('var CARD=' + card_data + ';')
    P(r'''var ib=document.getElementById("share-img");
if(ib)ib.addEventListener("click",function(){
  try{
    var W=1200,H=630,s=2,slate="#313941",beak="#ED7379",beakD="#D14E57",text="#16191d",mut="#5e6a73",line="#d9dee2",track="#dde2e6";
    var cv=document.createElement("canvas");cv.width=W*s;cv.height=H*s;
    var c=cv.getContext("2d");c.scale(s,s);c.textBaseline="alphabetic";
    c.fillStyle="#ffffff";c.fillRect(0,0,W,H);c.fillStyle=beak;c.fillRect(0,0,W,6);
    function wrap(t,x,y,mw,lh,max){var ws=t.split(" "),ln="",n=0;for(var i=0;i<ws.length;i++){var tn=ln?ln+" "+ws[i]:ws[i];if(c.measureText(tn).width>mw&&ln){c.fillText(ln,x,y);y+=lh;ln=ws[i];n++;if(max&&n>=max){c.fillText(ln+"…",x,y);return y;}}else{ln=tn;}}c.fillText(ln,x,y);return y;}
    function draw(){
      var bx0=CARD.logo?130:56;
      c.fillStyle=slate;c.font="700 26px -apple-system,'Segoe UI',Roboto,sans-serif";c.fillText("Roadmap",bx0,64);
      var bw=c.measureText("Roadmap").width;c.fillStyle=mut;c.font="500 17px -apple-system,sans-serif";c.fillText("· Builder Profile",bx0+bw+10,64);
      c.textAlign="right";c.fillStyle=mut;c.font="600 13px -apple-system,sans-serif";c.fillText("Generated locally · nothing uploaded",W-56,58);c.textAlign="left";
      c.strokeStyle=line;c.lineWidth=1;c.beginPath();c.moveTo(56,92);c.lineTo(W-56,92);c.stroke();
      c.fillStyle=mut;c.font="700 13px -apple-system,sans-serif";c.fillText("YOUR BUILDER PROFILE",56,136);
      var fs=58;c.font="800 "+fs+"px Georgia,'Times New Roman',serif";while(c.measureText(CARD.arch+".").width>W-112&&fs>30){fs-=2;c.font="800 "+fs+"px Georgia,serif";}
      c.fillStyle=beak;c.fillText(CARD.arch+".",56,150+fs);
      c.fillStyle="#3b444b";c.font="italic 21px Georgia,serif";var qy=wrap("“"+CARD.quote+"”",56,150+fs+42,W-130,30,2);
      var sy=qy+44;c.fillStyle=mut;c.font="700 13px -apple-system,sans-serif";c.fillText("GSTACK SCORECARD",56,sy);
      var rows=CARD.scores.length,base=sy+16,rh=Math.min(34,(H-70-base)/rows);
      for(var i=0;i<rows;i++){var ry=base+18+i*rh,nm=CARD.scores[i][0],vl=CARD.scores[i][1];
        c.fillStyle=slate;c.font="600 15px -apple-system,sans-serif";c.fillText(nm,56,ry+4);
        var bx=270,bw2=600,bh=12,by=ry-7;c.fillStyle=track;c.fillRect(bx,by,bw2,bh);
        var g=c.createLinearGradient(bx,0,bx+bw2,0);g.addColorStop(0,beakD);g.addColorStop(1,beak);c.fillStyle=g;c.fillRect(bx,by,bw2*(vl/10),bh);
        c.fillStyle=text;c.font="800 15px ui-monospace,Menlo,monospace";c.textAlign="right";c.fillText(vl.toFixed(1),W-56,ry+4);c.textAlign="left";}
      c.fillStyle=mut;c.font="500 13px -apple-system,sans-serif";c.fillText("Generated 100% on-device · github.com/Photobombastic/paxel-local",56,H-24);
      cv.toBlob(function(bl){if(!bl){alert("Image export failed — try a screenshot.");return;}var a=document.createElement("a");a.href=URL.createObjectURL(bl);a.download="builder-profile.png";a.click();});
    }
    if(CARD.logo){var im=new Image();im.onload=function(){c.drawImage(im,56,32,46,46);draw();};im.onerror=draw;im.src=CARD.logo;}else{draw();}
  }catch(e){alert("Image export failed — try a screenshot.");}
});''')
    P("})();</script></body></html>")
    with open(os.path.join(OUT_DIR, "profile.html"), "w") as f:
        f.write("\n".join(parts))


if __name__ == "__main__":
    main()
