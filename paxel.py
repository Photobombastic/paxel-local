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


# Politeness markers in your own prompts (for the "how polite are you" card). Word-boundaried.
_POLITE_RE = re.compile(r'\b(thanks|thank you|thank u|thx|please|pls|appreciate|'
                        r'much appreciated|good (?:job|work)|nice work|well done)\b', re.I)

# --- "In your own words" cards: pulled VERBATIM from your real prompts. These quote raw
# session text, so they render ONLY on the local page and are deliberately kept OUT of the
# shareable download card (see card_data). HTML-escape every quote before injecting it. ---
_TYPO_WORDS = {"teh", "hte", "thge", "wrok", "adn", "nad", "recieve", "seperate", "definately",
               "thier", "alot", "wtih", "wiht", "taht", "thta", "jsut", "becuase", "plz", "pls",
               "u", "ur", "r", "y", "k", "yea", "yeah", "yep", "yup", "nope", "lol", "lmao", "idk",
               "dont", "wont", "cant", "doesnt", "didnt", "couldnt", "wouldnt", "isnt", "wasnt",
               "youre", "theyre", "thats", "whats", "hows", "im", "ive", "ill", "id", "hes", "shes",
               "wodn", "fo", "ot", "si", "hmm", "hmmm", "wat", "wut", "tho", "thru", "fix", "undo",
               "nvm", "rn", "btw", "fr", "ok", "okay", "kk", "gah", "ugh", "argh", "oof",
               "wtf", "wth", "omg", "ya", "nah", "meh", "huh", "welp", "oop", "oops", "aight"}


def _typo_score(text):
    """Rough 'how garbled/casual is this' score — counts likely-typo / texting tokens.
    Heuristic, not a spell-checker; only used to surface a genuinely odd REAL prompt."""
    s = 0
    for t in re.findall(r"[a-z0-9']+", text.lower()):
        if t in _TYPO_WORDS:
            s += 1
        elif len(t) >= 4 and not re.search(r'[aeiou]', t):   # a vowel-less chunk
            s += 1
        elif re.search(r'(.)\1\1', t):                        # 3+ of the same letter (loool, yesss)
            s += 1
        elif re.search(r'[a-z]\d|\d[a-z]', t):                # digits glued into a word
            s += 1
        elif "'" not in t and t.endswith(("nt", "re", "ll", "ve")) and t in _TYPO_WORDS:
            s += 1                                            # missing apostrophe (dont, youre)
    return s


def _caps_ratio(text):
    letters = [c for c in text if c.isalpha()]
    return (sum(1 for c in letters if c.isupper()) / len(letters)) if letters else 0.0


# Frustration / distress markers for the "biggest crash-out" card — these gate it, so a
# clean all-caps EXCITEMENT prompt ("ONWARDS", "PUSH THRU") doesn't read as a meltdown.
_RAGE_RE = re.compile(r'\b(wtf|wth|ffs|ugh+|argh+|seriously|literally|stop+|nope|why+|'
                      r'are you (?:kidding|serious|sure|joking)|come on|for real|already said|'
                      r'i said|told you|do ?not|dont|cant|never|jesus|christ|damn|hell|crap|'
                      r'shit|fuck\w*|wrong|broke|broken|nightmare|stuck|fail\w*|hate|pressure|'
                      r'stress\w*|overwhelm\w*|dying|exhaust\w*|help|no+\b|not\b)\b', re.I)


def _crashout_score(text):
    """How 'heated' a prompt reads — caps, exclamation pile-ups, ALLCAPS words, frustration
    words, and BREVITY (terse all-caps menace — 'NO STOP', 'SOMETHING IS WRONG' — is funnier
    than a long rant). Pulls a REAL prompt, never invents one."""
    wc = len(text.split())
    caps = _caps_ratio(text)
    bangs = min(text.count("!") + text.count("?"), 5)
    allcaps = min(len(re.findall(r'\b[A-Z]{3,}\b', text)), 4)
    rage = min(len(_RAGE_RE.findall(text)), 3)
    brevity = max(0, 9 - wc) * 0.5
    return caps * 2.5 + brevity + allcaps * 0.4 + rage * 0.5 + bangs * 0.3


_FEELS_RE = re.compile(r'\b(worried|scared|nervous|anxious|stressed|exhausted|confused|'
                       r'stupid|dumb|idiot|hopeless|unemploy\w*|crying|sobbing|sad|miserable|'
                       r'overwhelmed|panic\w*|dying|losing my mind|cant anymore|please work)\b', re.I)
_EMOTICON_RE = re.compile(r"[:;=]['\-^]?[\(\)\[\]\/\\|dpox3<>]", re.I)
# Content-free affirmations/fillers — an "off the cuff" card needs more than "yep :)".
_FILLER = {"ok", "okay", "yes", "yep", "yup", "yeah", "ya", "sure", "nice", "great", "cool",
           "perfect", "thanks", "thank", "you", "done", "k", "kk", "good", "awesome", "love",
           "got", "it", "this", "that", "lol", "haha", "nvm", "fine", "right", "correct", "exactly"}


def _cryptic_score(text):
    """The funniest off-the-cuff prompts: tiny, typo'd, lowercase, vague, and — the gold —
    a stray emoticon or a flash of human vulnerability ('Im worried im unemploybale :(')."""
    wc = len(text.split())
    typ = _typo_score(text)
    vague = len(re.findall(r'\b(it|that|this|the thing|those|them|stuff|one)\b', text, re.I))
    lower = 1 if text == text.lower() else 0
    nopunct = 1 if not re.search(r'[.?!]', text.strip()) else 0
    short = max(0, 7 - wc) * 0.25
    emo = 1.6 if _EMOTICON_RE.search(text) else 0
    feels = 1.3 if _FEELS_RE.search(text) else 0
    return typ * 1.0 + vague * 0.55 + lower * 0.5 + nopunct * 0.35 + short + emo + feels


# A prompt can be surfaced verbatim only if it's actually the user's words — not a harness
# marker, and not carrying a secret. We NEVER alter a shown prompt (Max: zero redaction); we
# just refuse to SELECT one that's a credential or a system artifact rather than a real prompt.
_SECRET_RE = re.compile(r'eyJ[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9]{16,}|gh[posru]_[A-Za-z0-9]{16,}|'
                        r'AKIA[0-9A-Z]{12,}|Bearer\s+\S{16,}|[A-Fa-f0-9]{32,}|[A-Za-z0-9_\-]{36,}', re.I)
_SYS_MARKER_RE = re.compile(r'\[request interrupted|\[image\b|\[image\s*#|\[pasted|\[tool|'
                            r'<system|<command|<local-command|this block is not|tool_use|caveat:', re.I)


def _safe_quote(text):
    if not text or len(text) > 140:
        return False
    if _SYS_MARKER_RE.search(text) or _SECRET_RE.search(text):
        return False
    if any(len(tok) > 32 for tok in text.split()):   # a giant unbroken token = key/url/hash, not a word
        return False
    toks = text.split()
    if len(toks) == 1 and re.fullmatch(r"[A-Z0-9]*\d[A-Z0-9]*", toks[0]) and len(toks[0]) >= 7:
        return False                                  # a lone caps+digits token = Slack/ID, not a prompt
    # PII guard — don't auto-SURFACE someone else's email / phone / long digit run. (This is a
    # safe DEFAULT for arbitrary users; it never alters a prompt, it just won't select this one.)
    if re.search(r'[\w.+-]+@[\w-]+\.[a-z]{2,}|\b\d{3}[\s.\-]?\d{3}[\s.\-]?\d{4}\b|\b\d{6,}\b', text, re.I):
        return False
    return True


def _open_in_browser(path):
    """Best-effort: pop the finished profile in the default browser. Silent if it can't
    (headless / SSH / CI) — we just fall back to printing the path. Pass --no-open to skip."""
    try:
        import webbrowser
        if webbrowser.open("file://" + os.path.abspath(path)):
            print("  opened profile.html in your browser (pass --no-open to skip)")
            return
    except Exception:
        pass
    print("  open it yourself:", path)


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
    polite_prompts = 0         # prompts that say please / thanks / etc.
    prompt_lengths = []        # chars of genuine typed prompts
    # "In your own words" cards — pulled VERBATIM from real prompts (local page only,
    # never the shared image). go-to phrase / most cryptic / biggest crash-out.
    phrase_counts = Counter()      # normalized short prompt -> times seen
    phrase_repr = {}               # normalized -> first original spelling
    phrase_sess = defaultdict(set) # normalized -> session ids it appeared in
    cryptic_cands = []             # [(score, verbatim text)] — ranked at the end
    crashout_cands = []            # [(score, verbatim text)]
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
                                if _POLITE_RE.search(cleaned):
                                    polite_prompts += 1
                                # collect verbatim-quote candidates (short prompts only, and
                                # only if safe to surface — no secrets / no harness markers)
                                _wc = len(cleaned.split())
                                if _safe_quote(cleaned) and 1 <= _wc <= 6:
                                    _norm = re.sub(r"\s+", " ", cleaned.strip().lower()).strip("?.!,. ")
                                    if len(_norm) >= 2:
                                        phrase_counts[_norm] += 1
                                        phrase_repr.setdefault(_norm, cleaned.strip())
                                        phrase_sess[_norm].add(sid)
                                if _safe_quote(cleaned):
                                    if 3 <= _wc <= 14:
                                        _words = re.findall(r"[a-z']+", cleaned.lower())
                                        if _words and not all(w in _FILLER for w in _words):
                                            _csc = _cryptic_score(cleaned)
                                            if _csc >= 1.8:
                                                cryptic_cands.append((round(_csc, 2), cleaned.strip()))
                                    if sum(c.isalpha() for c in cleaned) >= 6 and _wc <= 16:
                                        _bangs = cleaned.count("!") + cleaned.count("?")
                                        # gate: must read NEGATIVE (frustration word or !!-level
                                        # punctuation) — caps alone is excitement, not a crash-out
                                        if _RAGE_RE.search(cleaned) or _bangs >= 2:
                                            _xsc = _crashout_score(cleaned)
                                            if _xsc >= 2.0:
                                                crashout_cands.append((round(_xsc, 2), cleaned.strip()))
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
    longest_burst_s = 0.0
    BURST_GAP_S = 1800               # a gap > 30 min ends a contiguous work "run"
    for ts_list in session_ts.values():
        ts_list.sort()
        active_s = 0.0
        for a, bnext in zip(ts_list, ts_list[1:]):
            active_s += min(bnext - a, GAP_CAP_S)
        durations_min.append(active_s / 60.0)
        # Longest *contiguous* burst (no gap > 30 min). sessionId is reused across
        # resumed sessions, so a single id can span weeks — max(session duration) is
        # meaningless; the longest unbroken burst is the honest "longest run."
        bstart = bprev = None
        for t in ts_list:
            if bprev is None:
                bstart = bprev = t
            elif t - bprev > BURST_GAP_S:
                longest_burst_s = max(longest_burst_s, bprev - bstart)
                bstart = bprev = t
            else:
                bprev = t
        if bstart is not None:
            longest_burst_s = max(longest_burst_s, bprev - bstart)
    active_hours = sum(durations_min) / 60.0
    avg_session_min = statistics.mean(durations_min) if durations_min else 0
    median_session_min = statistics.median(durations_min) if durations_min else 0
    longest_run_min = longest_burst_s / 60.0

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
            "longest_run_minutes": round(longest_run_min, 1),
            "polite_prompts": polite_prompts,
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
    # "In your own words" — pick the go-to phrase (most-repeated short prompt seen in >=2
    # sessions), most cryptic, biggest crash-out. VERBATIM, never stored in stats.json.
    goto = None
    for ph, cnt in phrase_counts.most_common(25):
        if cnt >= 3 and len(phrase_sess.get(ph, ())) >= 2:
            goto = (phrase_repr[ph], cnt, len(phrase_sess[ph]))
            break
    def _dedup_rank(cands):   # keep highest score per unique prompt, ranked (deterministic)
        best = {}
        for sc, tx in cands:
            k = tx.lower()
            if k not in best or sc > best[k][0]:
                best[k] = (sc, tx)
        return sorted(best.values(), key=lambda x: (-x[0], x[1]))   # tie-break on text → reproducible
    cryptic_cands = _dedup_rank(cryptic_cands)
    crashout_cands = _dedup_rank(crashout_cands)
    # Each card shows the SINGLE best quote; a ↻ button rerolls through this small pool
    # (top few within striking distance of #1 — quality only, no weak tail).
    def _quote_pool(cands, n=6, floor=0.5):
        if not cands:
            return []
        top = cands[0][0]
        return [tx for sc, tx in cands if sc >= top * floor][:n]
    rage_pool = _quote_pool([(sc, tx) for sc, tx in crashout_cands if len(tx.split()) <= 9])
    cuff_pool = _quote_pool(cryptic_cands)
    voice = {"goto": goto, "crashouts": rage_pool, "cryptics": cuff_pool}
    write_profile_html(stats, archetype, quote, scores, voice)
    print("\nWrote stats.json, report.md, narrative_input.md, profile.html to", OUT_DIR)
    if "--no-open" not in sys.argv:
        _open_in_browser(os.path.join(OUT_DIR, "profile.html"))
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
#
# The rubric was then AUDITED by running the real installed gstack skills
# (/plan-eng-review, /plan-ceo-review, /review) via independent subagents. That
# audit drove the current design: each metric is owned by EXACTLY ONE axis (so no
# two axes silently move together), Execution/Steering no longer anti-correlate,
# and a 5th "Product Instinct" axis was CUT — the audit showed it was mostly
# skill-detection plus terms recycled from other axes, i.e. it didn't honestly
# measure product judgment. Coding transcripts don't reveal that, so we don't fake it.
# ---------------------------------------------------------------------------
REPO_URL = "https://github.com/Photobombastic/paxel-local"

# Plain-language explanation shown under each score bar — what the axis measures, in
# human terms, no jargon. (The gstack grounding lives in the disclaimer + README, not here.)
SCORE_NOTES = {
    "Execution": "How much you ship, and how fast — your committed-code rate, how much of what "
                 "you generate actually lands in git, and how hard you delegate to agents.",
    "Planning": "How much you think before you build — exploring before writing, structured "
                "prompts, reasoning depth, and laying out a plan first.",
    "Steering": "How hands-on you stay — short agent chains and frequent check-ins, "
                "rather than pointing the agent and letting it run unchecked.",
    "Engineering": "How clean your work is — getting files right early, not re-editing the same "
                   "one over and over, low error rate, and checking your work.",
}

# One-line versions of the axis notes for the shareable poster image — the full SCORE_NOTES
# don't fit on a single line under a bar on the card.
SCORE_NOTES_SHORT = {
    "Execution": "Shipped output, at AI leverage",
    "Planning": "Think before you build",
    "Steering": "Hands-on direction of the agent",
    "Engineering": "Craft, with little rework",
}


def _clamp(x):
    return max(0.0, min(1.0, x))


def _d10(x):
    """First 10 chars of an ISO date, or '—' when missing (empty/timestampless corpus)."""
    return (x or "")[:10] or "—"


_MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _mon_yr(iso):
    """'2025-06-08' -> 'Jun 2025' (human-readable timeframe for the share poster)."""
    iso = iso or ""
    if len(iso) >= 7 and iso[4] == "-":
        try:
            return f"{_MONTHS[int(iso[5:7])]} {iso[0:4]}"
        except (ValueError, IndexError):
            pass
    return (iso[:10] or "—")


def _js(obj):
    """json.dumps for embedding INSIDE a <script> tag. Python's json.dumps does not escape
    '<', '>', '&', so a prompt containing '</script>' (a real web-dev question) would close
    the script element early and break the whole page. Escape them to \\uXXXX (still valid
    JSON/JS), plus the U+2028/U+2029 line separators that break JS string literals."""
    return (json.dumps(obj)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace(" ", "\\u2028").replace(" ", "\\u2029"))


def _skill_uses(stats, needle):
    return sum(n for k, n in stats["stack"].get("top_skills", []) if needle in k.lower())


def _skill_uses_any(stats, needles):
    return sum(n for k, n in stats["stack"].get("top_skills", [])
               if any(nd in k.lower() for nd in needles))


def _evidence(stats):
    """How much activity we actually have to judge habits on, 0..1. ~1.0 for any real
    corpus, near 0 for a thin one. Used to stop 'absence of a signal' from reading as
    'did it perfectly' in the inverse score terms — a barely-used corpus shouldn't grade
    as a flawless builder. (See _ev and the LOW_DATA flag in write_profile_html.)
    Saturates at ~2000 tool calls (≈15 real sessions) so the gating actually has a
    gradient across thin→mid corpora, not just sub-30-minute ones."""
    return _clamp(stats["volume"]["tool_calls_total"] / 2000)


def _ev(credit, ev):
    """Pull an ABSENCE-reward score term toward a neutral 0.5 when evidence (ev) is low,
    so 'no data' lands at the midpoint (admitted uncertainty) instead of a flattering 1.0.
    At ev=1.0 (any real corpus) this returns `credit` unchanged — a true no-op for real
    users; it only ever bites thin corpora. Apply ONLY to inverse terms (those that score
    high when a 'bad' metric is low/zero); presence terms already score 0 for 'didn't do it'."""
    return 0.5 * (1 - ev) + ev * credit


def compute_scores(stats):
    # FOUR axes, grounded in gstack (module note above) and then hardened by a gstack
    # self-audit. Design rules the audit enforced:
    #   1. Each metric is owned by EXACTLY ONE axis — no metric drives two axes, so the
    #      axes are genuinely independent (no hidden correlation).
    #   2. actions_per_prompt lives ONLY in Steering (hands-on cadence); Execution no
    #      longer rewards it, so the two axes don't anti-correlate by construction.
    #   3. iteration_depth_p90 lives ONLY in Engineering; questions_asked ONLY in Steering.
    #   4. Skill-detection terms are kept but de-weighted (a builder who plans in Notion
    #      and reviews on GitHub shouldn't score 0) — behavior carries the axes.
    # Weights sum to 1.0 per axis; every term is clamped 0..1 against a justified target;
    # `_ev` pulls the INVERSE terms toward neutral on a thin corpus.
    v, b, vel = stats["volume"], stats["behavior"], stats["velocity"]
    if v["total_sessions"] == 0 or v["tool_calls_total"] == 0:
        # No real activity → don't manufacture a flattering "Quality Guardian 9.0"
        return {"Execution": 0.0, "Planning": 0.0, "Steering": 0.0, "Engineering": 0.0}
    sess = max(v["total_sessions"], 1)
    prompts = max(v["total_prompts"], 1)
    hours = max(vel["active_hours"], 0.1)
    ev = _evidence(stats)   # 0..1 confidence; gates the inverse terms so a thin corpus
                            # can't read as flawless (no-op at ev=1.0 for any real user).

    # EXECUTION — shipped output at AI leverage. Three signals, no overlap with other axes:
    #   (a) RATE: gold-standard git churn per active hour (coverage-corrected — git often
    #       sees only some repos; we nudge ≤1.4× by coverage rather than penalize, and the
    #       report discloses it). (b) FIDELITY: how much of what you GENERATED actually got
    #       committed — git churn vs tool churn — the audit's headline "are you shipping or
    #       just exploring" signal (also coverage-corrected). (c) DELEGATION/parallelism.
    #   Dropped vs the old version: actions_per_prompt (moved to Steering) and raw session
    #   length (the audit called it noise — a long distracted session isn't execution).
    git_cov = max(vel["git_repos_with_commits"] / max(vel["git_repos_seen"], 1), 0.7)
    eff_git_churn = vel["git_churn_total"] / git_cov
    fidelity = eff_git_churn / max(vel["tool_churn_edit_write"], 1)
    execution = 10 * (
        0.40 * _clamp((eff_git_churn / hours) / 400)                      # committed-code rate, coverage-corrected
        + 0.25 * _clamp(fidelity / 0.5)                                   # ship-vs-generate fidelity (committed / generated)
        + 0.35 * _clamp((b["delegate_actions"] + b["background_tasks"]) / max(prompts * 0.3, 1)))  # delegation/parallelism

    # PLANNING — think before you build. Behavior-led (the duplicate explore-ratio term the
    # audit flagged is gone — planning_ratio already captures it); prompt sophistication
    # raised (the audit's "most honest proxy for the human's thinking"); skill term de-weighted.
    plan_skills = _skill_uses_any(stats, ("brainstorm", "writing-plan", "plan", "spec",
                                          "office-hours", "autoplan", "grill", "ceo-review",
                                          "eng-review", "design-review"))
    planning = 10 * (
        0.35 * _clamp(b["planning_ratio_explore_to_doing"] / 0.65)        # explore-before-build (behavioral)
        + 0.25 * _clamp(v["avg_prompt_length_chars"] / 600.0)            # prompt sophistication — the human's thinking
        + 0.20 * _clamp((v["thinking_blocks"] / sess) / 12.0)           # reasoning depth per session
        + 0.20 * _clamp((plan_skills / sess) / 0.8))                     # plan/spec ceremony, session-normalized, de-weighted

    # STEERING — User Sovereignty: how much the human stays in control. PURELY BEHAVIORAL
    # (no skill-detection — review/qa are craft, they live in Engineering): owns
    # actions_per_prompt (hands-on cadence) and questions_asked (how often the agent stopped
    # to ask YOU — low = you let it run). p90 was removed (it's Engineering's only).
    steering = 10 * (
        0.55 * _ev(_clamp((15 - b["actions_per_prompt"]) / 11), ev)      # hands-on cadence: fewer agent actions per prompt = more in the loop
        + 0.45 * _clamp((b["questions_asked"] / prompts) / 0.05))        # how often the agent checked in with you (per prompt)

    # ENGINEERING — craft / low rework. The old churn_back term (deletion ratio) was CUT:
    # it scored a clean refactor as "thrash" and gave a perfect score to anyone who never
    # committed. Replaced by iteration_depth_mean ("did you get the file right early"), the
    # honest rework signal. p90 + file-hammering stay here (their only home). Ceremony de-weighted.
    # "code-review" (not bare "review") so this doesn't greedily match Planning's
    # plan-eng-review / plan-design-review / ceo-review ceremonies (which live in plan_skills).
    eng_skills = _skill_uses_any(stats, ("code-review", "test", "tdd", "qa", "investigate",
                                         "retro", "learn", "cso", "karpathy", "debug"))
    engineering = 10 * (
        0.30 * _ev(1 - _clamp((b["iteration_depth_mean"] - 2) / 8), ev)  # low rework: got files right early
        + 0.25 * _ev(1 - _clamp((b["iteration_depth_p90"] - 3) / 9), ev)  # clean iteration: low typical depth
        + 0.20 * _ev(1 - _clamp((b["files_hammered_over_15x"] / sess) / 0.25), ev)  # focused: few hammered files
        + 0.15 * _clamp((eng_skills / sess) / 3.0)                       # quality ceremonies: review/qa/investigate
        + 0.10 * _ev(1 - _clamp(b["error_rate_per_100_tools"] / 10), ev))  # low error rate: root-cause discipline

    return {"Execution": round(execution, 1), "Planning": round(planning, 1),
            "Steering": round(steering, 1), "Engineering": round(engineering, 1)}


def pick_archetype(stats, scores):
    b, vel = stats["behavior"], stats["velocity"]
    # brute = a HABITUAL grinder (high typical iteration), not one 40-edit outlier session
    brute = b["iteration_depth_p90"] >= 12 or (vel["shell_authored_lines_est"] > 50000
                                               and b["error_rate_per_100_tools"] >= 3)
    plan_hi = scores.get("Planning", 0) >= 7.5
    exec_hi = scores.get("Execution", 0) >= 8
    eng_hi = scores.get("Engineering", 0) >= 7.5
    steer_hi = scores.get("Steering", 0) >= 7.5
    # when both Execution and Engineering qualify, let the dominant one win the label
    exec_hi = exec_hi and scores.get("Execution", 0) >= scores.get("Engineering", 0)
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
    elif steer_hi:
        name, q = "The Director", "You stay in the loop — short chains, frequent check-ins, no runaway agents."
    else:
        # No single mode dominates. (Time-of-day isn't a build style — it's a 'what we
        # noticed' card, not an identity — so Night Owl was retired as an archetype.)
        name, q = "The Builder", "Balanced and pragmatic — no single mode dominates; you adapt to the problem in front of you."
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
            f'The agent stopped to ask you on just <b>{qrate*100:.0f}%</b> of {prompts:,} prompts — '
            f'you point it and let it run, rather than getting pulled into a back-and-forth.'))

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
        lead = (f'Steering is your lowest axis at <b>{scores.get("Steering", "?")}</b>'
                if lowest == "Steering" else f'Steering sits at <b>{scores.get("Steering", "?")}</b>')
        pool.append((1.0, "Steer harder",
            "Interrupt during, not just review after",
            f'{lead} — the agent stopped to check with you on only <b>{qrate*100:.0f}%</b> of prompts, '
            f'so it mostly ran without your input. '
            f'Break in on risky steps and redirect long chains <i>while</i> they run, instead of only '
            f'reviewing after. (gstack calls this <code>/careful</code>.)'))

    if rev >= 50 and tdd < max(rev * 0.1, 5):
        pool.append((1.5, "Add a reflex",
            "Pair your review reflex with a test reflex",
            f'<b>{rev:,}</b> code-reviews vs <b>{tdd}</b> test runs. Make the double-check a <i>regression '
            f'test</i>: write one for every bug you fix before you move on. (gstack\'s <code>/qa</code> does this automatically.)'))

    if b["iteration_depth_max"] >= 40 or b["files_hammered_over_15x"] >= 10:
        pool.append((2.0, "Stop the grind",
            "Root-cause instead of whack-a-mole",
            f'<b>{b["iteration_depth_max"]}×</b> on one file, <b>{b["files_hammered_over_15x"]}</b> files past '
            f'15 edits. When a file resists past ~15 tries, stop and find the root cause before the next edit '
            f'instead of retrying. (gstack names this discipline <code>/investigate</code> — no fixes without investigation.)'))

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
  .share{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:34px} .share .lbl{font-size:13px;color:var(--muted)}
  .btn{display:inline-flex;align-items:center;gap:8px;padding:9px 15px;border-radius:999px;cursor:pointer;font-weight:600;font-size:14px;color:#fff;border:1px solid transparent;font-family:var(--sans)}
  .btn:hover{text-decoration:none;opacity:.9} .btn.x{background:#000} .btn.ghost{background:#fff;color:var(--slate);border-color:var(--line)}
  .btn svg{width:15px;height:15px} .btn.x svg{fill:#fff}
  h2.section{font-family:var(--display);font-size:15px;text-transform:uppercase;letter-spacing:.18em;color:var(--slate);margin:60px 0 14px;font-weight:700}
  p.lead{color:var(--muted);font-size:14.5px;margin:-4px 0 20px;max-width:700px;line-height:1.55}
  .card code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;background:#eef1f3;color:var(--beak-deep);padding:1px 5px;border-radius:4px}
  .disclaimer{background:#fff;border:1px solid var(--line);border-left:4px solid var(--beak);border-radius:6px;padding:14px 16px;margin:-6px 0 24px;font-size:13.5px;color:#48535b;line-height:1.55} .disclaimer b{color:var(--text)}
  .score{display:grid;grid-template-columns:160px 1fr 46px;align-items:center;gap:14px;margin:0 0 14px} .score .name{font-weight:600;font-size:15px}
  .score .track{display:block;height:12px;background:#dde2e6;border-radius:999px;overflow:hidden} .score .fill{display:block;height:100%;min-width:8px;background:linear-gradient(90deg,var(--beak-deep),var(--beak));border-radius:999px}
  .score .val{font-weight:800;text-align:right} .score .note{grid-column:1/-1;color:var(--muted);font-size:13px;margin:-6px 0 4px;padding-left:174px}
  @media(max-width:560px){.score .note{padding-left:0}}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:18px 18px 16px;box-shadow:0 1px 2px rgba(20,30,40,.04)} .card.flag{border-left:4px solid var(--beak)}
  .card .q{color:var(--beak-deep);font-size:12.5px;font-weight:700;margin:0 0 8px;text-transform:uppercase;letter-spacing:.03em}
  .card .reroll{font-family:var(--sans);text-transform:none;letter-spacing:0;font-size:11px;font-weight:600;color:var(--beak-deep);background:none;border:1px solid var(--line);border-radius:999px;padding:1px 8px;margin-left:8px;cursor:pointer;vertical-align:middle}
  .card .reroll:hover{background:#fff;border-color:var(--beak)}
  .card .a{font-family:var(--serif);font-size:19px;font-weight:700;margin:0 0 6px} .card .d{color:var(--muted);font-size:13.5px;margin:0}
  footer{margin-top:54px;padding-top:22px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;line-height:1.7} footer .lock{color:var(--beak-deep);font-weight:700} footer .by{color:var(--text)}
</style>"""


def _plain(s):
    """Flatten the trusted <b>/<i>/<code> markup (and the few HTML entities we emit) out of
    a card string so it can be drawn as plain text on the canvas poster. Inputs are
    safe-by-construction (numbers / static templates — see _card)."""
    s = re.sub(r"<[^>]+>", "", s or "")
    for a, b in (("&amp;", "&"), ("&rsquo;", "’"), ("&lsquo;", "‘"),
                 ("&ldquo;", "“"), ("&rdquo;", "”"), ("&mdash;", "—")):
        s = s.replace(a, b)
    return s


def _card(q, a, d, flag=False):
    # q/a/d are injected RAW (no escaping) so callers can use intentional <b>/<code>/<i>
    # markup. Every caller must pass safe-by-construction strings: numbers, static
    # templates, or html.escape()'d values — NEVER raw user/transcript-derived text.
    cls = "card flag" if flag else "card"
    return f'<div class="{cls}"><p class="q">{q}</p><p class="a">{a}</p><p class="d">{d}</p></div>'


def write_profile_html(stats, archetype, quote, scores, voice=None):
    import html as _h
    v, vel, b, r, t, st, c = (stats["volume"], stats["velocity"], stats["behavior"],
                              stats["rhythm"], stats["tools"], stats["stack"], stats["corpus"])
    logo = _img_data_uri(os.path.join(OUT_DIR, "tern.png"))
    chip = f'<span class="chip"><img src="{logo}" alt="Roadmap tern"></span>' if logo else ""

    peak = (r["peak_hours_local"] or [12])[0]
    tod = ("Night owl" if (peak >= 22 or peak <= 4) else "Morning person" if peak <= 11
           else "Afternoon" if peak <= 16 else "Evening")
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
    prompts = max(v["total_prompts"], 1)
    per_sess = round(b["delegate_actions"] / sess, 1)
    git_pct = f'{vel["git_repos_with_commits"]}/{vel["git_repos_seen"]}'

    # The coral left-bar (flag=True) means exactly one thing: "this is an action item."
    # It is used ONLY on the Growth-edge cards. Signature-move and What-we-noticed cards
    # are descriptive, so they stay plain — no flags here.
    h12 = f'{(peak - 1) % 12 + 1}{"am" if peak < 12 else "pm"}'   # 17 -> "5pm", 0 -> "12am"
    _DAYFULL = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
                "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}
    busy_day = _DAYFULL.get((r["preferred_days"] or ["—"])[0], (r["preferred_days"] or ["—"])[0])
    weekend_d = (f'Your busiest day is {busy_day} — and you logged time most days, weekends included.'
                 if weekend_a == "No days off" else f'Your busiest day is {busy_day}; weekends stay quiet.')
    two_gears = v["avg_prompt_length_chars"] > v["median_prompt_length_chars"] * 2
    prompt_a = "Short, with the odd essay" if two_gears else "Consistent length"
    prompt_d = (f'Half run under {v["median_prompt_length_chars"]:,.0f} characters — quick commands — '
                f'but the average is {v["avg_prompt_length_chars"]:,.0f}.' if two_gears else
                f'Median {v["median_prompt_length_chars"]:,.0f} characters, average {v["avg_prompt_length_chars"]:,.0f} — pretty steady.')
    polite_n = b.get("polite_prompts", 0)
    polite_rate = polite_n / prompts
    polite_a = ("You say thanks a lot" if polite_rate >= 0.12 else
                "Polite enough" if polite_rate >= 0.04 else "All business")
    polite_d = (f'You said please or thank-you in <b>{polite_n:,}</b> of your {v["total_prompts"]:,} prompts '
                f'({polite_rate*100:.0f}%).' + (" When the robots take over, they'll remember."
                                                if polite_rate >= 0.12 else ""))
    lr = b.get("longest_run_minutes", 0)
    lr_h, lr_m = int(lr // 60), int(lr % 60)
    longrun_a = f'{lr_h}h {lr_m}m' if lr_h else f'{lr_m}m'
    qrate_c = b["questions_asked"] / prompts
    teammate = polite_rate >= 0.05 or qrate_c >= 0.04
    agent_a = "Like a teammate" if teammate else "Like a tool"
    agent_d = ('You bounce ideas off it and ask for pushback — more collaborator than command line.'
               if teammate else 'You hand it work and check the result — more command line than collaborator.')
    # "What we noticed" — question-framed eyebrows + plain second-person copy (no jargon).
    cards = [
        _card("How much did you ship?", "Depends how you count",
              f'Edit/Write touched <b>{vel["tool_churn_edit_write"]:,}</b> lines and the shell ~{vel["shell_authored_lines_est"]:,} '
              f'more — but only <b>{vel["git_churn_total"]:,}</b> actually landed in committed git history. '
              f'That committed number is the honest one.'),
        _card("How hard do you grind?", f'{b["iteration_depth_max"]}× on one file',
              f'Your deepest single-file grind in one session — and {b["files_hammered_over_15x"]} files went past 15 edits. '
              f'Your typical file, though? About {b["iteration_depth_mean"]:.1f}.'),
        _card("How often do things break?", f'{b["tool_errors"]:,} errors, {round(b["error_recovery_ratio"]*100)}% recovered',
              f'Roughly {b["error_rate_per_100_tools"]} per 100 tool calls — and you kept going after almost all of them.'),
        _card("Which model do you reach for?", model_a, model_d),
        _card("When do you do your best work?", tod, f'You do your heaviest work around {h12}.'),
        _card("Do you take weekends off?", weekend_a, weekend_d),
        _card("How long are your prompts?", prompt_a, prompt_d),
        _card("How many agents do you run?", f'{b["delegate_actions"]:,} subagents',
              f'About {per_sess} per session, plus {b["background_tasks"]:,} background tasks and {b["scheduled_actions"]} scheduled runs.'),
        _card("How do you see your agent?", agent_a, agent_d),
        _card("How polite are you to it?", polite_a, polite_d),
        _card("What's your longest run?", longrun_a,
              'Your longest unbroken stretch of active work in a single session.'),
        _card("What's your go-to tool?", top_tool_name, f'{top_tool[1]:,} calls — more than any other tool.'),
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

    # Data for the canvas-drawn share POSTER (one tall 1200px-wide PNG, drawn at download
    # time so there's no foreignObject → no canvas taint → it works in every browser).
    # Carries: archetype + tagline + timeframe, the scorecard (with one-line notes) paired
    # with the headline numbers, and the curated insight/quote cards. SUBSTANCE FIRST: the
    # signature move + insights lead; the two quote cards (funny, low-substance) come LAST.
    # Quotes are routed through _safe_quote, and the off-the-cuff card is re-read LIVE from
    # the page (#q-cuff) at click time, so a reroll is honored and the user is the gate.
    _voc = voice or {}
    poster_cards = []
    if moves:
        _mtag, _mtitle, _mev = moves[0]
        poster_cards.append({"mode": "insight", "eyebrow": _plain(_mtag),
                             "headline": _plain(_mtitle), "sub": _plain(_mev)})
    if b["delegate_actions"] > 0:
        poster_cards.append({"mode": "insight", "eyebrow": "How many agents?",
                             "headline": f'{b["delegate_actions"]:,} subagents',
                             "sub": f'About {per_sess} per session, plus {b["background_tasks"]:,} '
                                    f'background tasks and {b["scheduled_actions"]} scheduled runs.'})
    poster_cards += [
        {"mode": "insight", "eyebrow": "Best work?", "headline": tod,
         "sub": f"Heaviest work around {h12}."},
        {"mode": "insight", "eyebrow": "Weekends?", "headline": weekend_a, "sub": _plain(weekend_d)},
        {"mode": "insight", "eyebrow": "Your agent is…", "headline": agent_a, "sub": _plain(agent_d)},
        {"mode": "insight", "eyebrow": "Polite to it?", "headline": polite_a, "sub": _plain(polite_d)},
    ]
    if _voc.get("goto") and _safe_quote(_voc["goto"][0]):
        _ph, _cnt, _ns = _voc["goto"]
        poster_cards.append({"mode": "quote", "eyebrow": "Go-to prompt",
                             "headline": _ph, "sub": f"Most-repeated — {_cnt:,}×."})
    if _voc.get("cryptics"):
        poster_cards.append({"mode": "quote", "eyebrow": "Off the cuff", "live": "cuff",
                             "headline": _voc["cryptics"][0], "sub": "Straight from the keyboard."})

    _tag = quote.strip()
    if _tag:
        _tag = _tag[0].upper() + _tag[1:]
        if _tag[-1] not in ".!?":
            _tag += "."
    _dr = c["date_range"] or ["", ""]
    # timeframe + sessions only — prompts/tool-calls already live in "By the numbers".
    _context = f'{_mon_yr(_dr[0])} → {_mon_yr(_dr[1])}  ·  {v["total_sessions"]:,} sessions'
    # Lead "By the numbers" stat adapts so a non-delegating user (most Codex/Gemini users, and
    # plenty of Claude ones) never gets a weak "0.0 agents / session" headline: agents/session
    # for real delegators → else reasoning blocks → else lines edited (all universally non-zero).
    if b["delegate_actions"] >= 20 and per_sess >= 0.5:
        _first_stat = [f'{per_sess}', "agents / session"]
    elif v["thinking_blocks"] >= 50:
        _first_stat = [f'{v["thinking_blocks"]:,}', "reasoning blocks"]
    else:
        _first_stat = [f'{vel["tool_churn_edit_write"]:,}', "lines edited"]
    card_data = _js({
        "arch": archetype,
        "tagline": _tag,
        "context": _context,
        "scores": [[k, val, SCORE_NOTES_SHORT.get(k, "")] for k, val in scores.items()],
        "stats": [_first_stat,
                  [f'{v["total_prompts"]:,}', "prompts"],
                  [f'{v["tool_calls_total"]:,}', "tool calls"],
                  [f'{vel["git_churn_total"]:,}', "git lines"]],
        "cards": poster_cards,
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
    P(f'<p class="sub"><b>{v["thinking_blocks"]:,} reasoning blocks</b> before the diffs, '
      f'<b>{b["delegate_actions"]:,} subagents</b> dispatched, and <b>{b["tool_errors"]:,} errors</b> recovered from along the way.</p>')
    P('<div class="stat-strip">'
      f'<div><span class="n mono">{vel["git_churn_total"]:,}</span><span class="l">lines committed to git</span></div>'
      f'<div><span class="n mono">{vel["tool_churn_edit_write"]:,}</span><span class="l">lines via Edit/Write</span></div>'
      f'<div><span class="n mono">~{vel["shell_authored_lines_est"]:,}</span><span class="l">lines in the shell</span></div>'
      f'<div><span class="n mono">{b["iteration_depth_max"]}</span><span class="l">max edits, one file</span></div>'
      f'<div><span class="n mono">{b["delegate_actions"]:,}</span><span class="l">agents you ran</span></div></div>')
    P('<div class="share"><span class="lbl">Share:</span>'
      '<a id="share-x" class="btn x" href="#" target="_blank" rel="noopener">'
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231 5.45-6.231Zm-1.161 17.52h1.833L7.084 4.126H5.117L17.083 19.77Z"/></svg>Post on X</a>'
      '<button id="share-copy" class="btn ghost" type="button">📋 Copy caption</button>'
      '<button id="share-img" class="btn ghost" type="button">🖼 Download image</button></div>')
    P('</section><h2 class="section">Your scorecard</h2>')
    P('<div class="disclaimer"><b>How to read this.</b> The <b>counts are measured</b> from your real sessions — '
      'reproducible, not guessed. The <b>0–10 scores are an opinion</b>: a transparent rubric grounded in '
      '<a href="https://github.com/garrytan/gstack" target="_blank" rel="noopener">Garry Tan\'s gstack</a>, '
      'not a copy of Paxel\'s closed algorithm. Short version: <b>numbers = fact, scores = opinion.</b></div>')
    if _evidence(stats) < 0.5:   # < ~1000 tool calls: too thin to read habits confidently
        P(f'<div class="disclaimer" style="border-left-color:var(--muted)">⚠ <b>Limited data.</b> '
          f'Just {v["total_sessions"]} sessions and {v["tool_calls_total"]:,} tool calls here — not enough to read '
          f'your habits with confidence, so these scores lean toward the middle. Run more and check back.</div>')
    P(score_rows)
    if moves:
        P('<h2 class="section">Your signature moves</h2>')
        P('<p class="lead">The patterns in how you direct the AI, pulled from your real sessions. The tag on each '
          'card is the gstack stage it maps to.</p>')
        P(f'<div class="grid">{moves_html}</div>')
    if edges:
        P('<h2 class="section">Your growth edge</h2>')
        P('<p class="lead">A few habits to try — each pulled from your own data, not a generic checklist. The '
          '<code>/commands</code> in parentheses are optional tools from '
          '<a href="https://github.com/garrytan/gstack" target="_blank" rel="noopener">gstack</a> '
          'if you\'d rather automate one of them.</p>')
        P(f'<div class="grid">{edges_html}</div>')
    P('<h2 class="section">What we noticed</h2><div class="grid">')
    P("".join(cards))
    P('</div>')

    # "In your own words" — VERBATIM prompt quotes (each already _safe_quote-filtered for
    # secrets/PII upstream). Rendered on the local page (gitignored). The go-to and the
    # off-the-cuff also feed the shareable poster (see poster_cards above) — the off-cuff via
    # a LIVE read of #q-cuff at download, so a reroll is honored. The crash-out stays
    # page-only. Escape every quote (raw user text → XSS). Only render cards that exist.
    voice = voice or {}
    vcards = []
    quote_js = {}   # target -> [raw quotes] for the ↻ reroll button (cycles the pool client-side)

    def _quote_card(eyebrow, target, pool, desc):
        quote_js[target] = pool
        reroll = (f' <button type="button" class="reroll" data-target="{target}">&#8635; another</button>'
                  if len(pool) > 1 else "")
        return (f'<div class="card"><p class="q">{eyebrow}{reroll}</p>'
                f'<p class="a" id="q-{target}">&ldquo;{_h.escape(pool[0])}&rdquo;</p>'
                f'<p class="d">{desc}</p></div>')

    if voice.get("goto"):
        ph, cnt, ns = voice["goto"]
        vcards.append(_card("What's your go-to prompt?", f'&ldquo;{_h.escape(ph)}&rdquo;',
              f'Your most-repeated prompt — <b>{cnt:,}</b> times across {ns} sessions.'))
    if voice.get("crashouts"):
        vcards.append(_quote_card("Your biggest crash-out?", "crashout", voice["crashouts"],
              "One of your most heated prompts. We&rsquo;ve all been there."))
    if voice.get("cryptics"):
        vcards.append(_quote_card("Off the cuff?", "cuff", voice["cryptics"],
              "One of your more unfiltered asks — straight from the keyboard, unedited."))
    if vcards:
        P('<h2 class="section">In your own words</h2>')
        P('<p class="lead">Pulled <b>verbatim</b> from your real prompts (filtered for secrets &amp; PII) — '
          'hit <b>&#8635; another</b> to reroll. Your go-to and off-the-cuff lines also land on the shareable '
          'image; the off-the-cuff one uses <b>whichever you&rsquo;ve rerolled to</b>, so land on one you like '
          'before you download. Everything else stays on this local page, on your machine.</p>')
        P(f'<div class="grid">{"".join(vcards)}</div>')

    P('<footer><span class="lock">🔒 Generated entirely on-device</span> by <span class="mono">paxel.py</span> — '
      'the same analysis Paxel runs, with zero data sent anywhere. Counts measured from your transcripts; '
      'archetype &amp; scores are a rubric. Raw metrics in <span class="mono">stats.json</span>.<br>'
      'Built by <a class="by" href="https://github.com/Photobombastic" target="_blank" rel="noopener">Max Schilling</a>, '
      '<a href="https://www.roadmap.chat/community" target="_blank" rel="noopener">Roadmap</a></footer>')
    P('</div></div>')
    P("<script>(function(){")
    P('var QUOTES=' + _js(quote_js) + ';var QIDX={};')
    P('document.querySelectorAll(".reroll").forEach(function(b){b.addEventListener("click",function(){'
      'var t=b.getAttribute("data-target"),arr=QUOTES[t];if(!arr||arr.length<2)return;'
      'QIDX[t]=((QIDX[t]||0)+1)%arr.length;var el=document.getElementById("q-"+t);'
      'if(el)el.textContent="\\u201c"+arr[QIDX[t]]+"\\u201d";});});')
    P(f'var repo={_js(REPO_URL)};var caption={_js(caption)};')
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
    var W=1200,M=48,s=3,IW=W-2*M;   // 3x supersample so the logo art stays crisp when zoomed/retina
    var beak="#ED7379",beakD="#D14E57",slate="#313941",mut="#5e6a73",line="#dfe3e7",track="#dde2e6";
    function L(c,t,mw){var ws=String(t).split(" "),ln="",o=[];for(var i=0;i<ws.length;i++){var tn=ln?ln+" "+ws[i]:ws[i];if(c.measureText(tn).width>mw&&ln){o.push(ln);ln=ws[i];}else ln=tn;}o.push(ln);return o;}
    function rr(c,x,y,w,h,r){c.beginPath();c.moveTo(x+r,y);c.arcTo(x+w,y,x+w,y+h,r);c.arcTo(x+w,y+h,x,y+h,r);c.arcTo(x,y+h,x,y,r);c.arcTo(x,y,x+w,y,r);c.closePath();}
    function box(c,x,y,w,h){c.save();c.shadowColor="rgba(40,50,60,.10)";c.shadowBlur=20;c.shadowOffsetY=7;c.fillStyle="#fff";rr(c,x,y,w,h,16);c.fill();c.restore();c.strokeStyle="#e8ebee";c.lineWidth=1;rr(c,x,y,w,h,16);c.stroke();}
    function mini(c,x,y,w,h,card){
      var pad=20,iw=w-2*pad,cx=x+w/2;box(c,x,y,w,h);c.textAlign="center";
      c.fillStyle=beakD;c.font="700 10.5px -apple-system,sans-serif";if(c.letterSpacing!==undefined)c.letterSpacing="1px";
      var el=L(c,String(card.eyebrow).toUpperCase(),iw);if(el.length>2)el=el.slice(0,2);
      for(var k=0;k<el.length;k++)c.fillText(el[k],cx,y+pad+11+k*14);if(c.letterSpacing!==undefined)c.letterSpacing="0px";
      // TOP-ANCHOR the content (not vertically centered) so every headline in a row shares
      // a baseline; leftover whitespace collects at the bottom of shorter cards.
      var ebBot=y+pad+11+(el.length-1)*14+6,gTop=ebBot+14,q=card.mode==="quote";
      var hfs=q?29:23;function hf(z){return (q?"italic 800 ":"800 ")+z+"px Georgia,serif";}
      var head=q?("“"+card.headline+"”"):card.headline;c.font=hf(hfs);var hl=L(c,head,iw);
      while(hl.length>2&&hfs>16){hfs-=1;c.font=hf(hfs);hl=L(c,head,iw);}
      if(hl.length>2){hl=hl.slice(0,2);hl[1]=hl[1].replace(/[\s—-]+$/,"")+"…";}
      var hLH=hfs+4;c.fillStyle="#181c1f";c.font=hf(hfs);for(var k=0;k<hl.length;k++)c.fillText(hl[k],cx,gTop+hfs-2+k*hLH);
      c.font="400 13px -apple-system,sans-serif";var sl=L(c,card.sub,iw),smax=q?2:3;
      if(sl.length>smax){sl=sl.slice(0,smax);sl[smax-1]=sl[smax-1].replace(/[\s—-]+$/,"")+"…";}
      var sTop=gTop+hl.length*hLH+12;c.fillStyle=mut;c.font="400 13px -apple-system,sans-serif";
      for(var k=0;k<sl.length;k++)c.fillText(sl[k],cx,sTop+11+k*18);
      c.textAlign="left";
    }
    // WYSIWYG: pull whatever quote is showing on the page now into any "live" card (off-cuff reroll)
    var cards=CARD.cards.map(function(cd){
      if(cd.live){var el=document.getElementById("q-"+cd.live);
        if(el){var t=el.textContent.replace(/^[\s“"]+/,"").replace(/[\s”"]+$/,"");if(t)cd=Object.assign({},cd,{headline:t});}}
      return cd;});
    // layout — tightened top gap; height grows with the card count
    var mc=document.createElement("canvas").getContext("2d");
    var afs=88;mc.font="800 "+afs+"px Georgia,serif";while(mc.measureText(CARD.arch+".").width>IW&&afs>48){afs-=2;mc.font="800 "+afs+"px Georgia,serif";}
    mc.font="italic 27px Georgia,serif";var tll=L(mc,CARD.tagline,IW);if(tll.length>2){tll=tll.slice(0,2);tll[1]=tll[1].replace(/[\s—-]+$/,"")+"…";}   // tagline can wrap to 2 lines
    var heroY=96,archB=heroY+12+Math.round(afs*0.74),tagY=archB+40,tagLH=33,ctxY=tagY+(tll.length-1)*tagLH+30,scY=ctxY+42,scH=348,scEnd=scY+scH;
    var gridY=scEnd+30,gc=4,rows=Math.ceil(cards.length/gc),cardH=184,gapY=22;
    var gridEnd=rows>0?gridY+rows*cardH+(rows-1)*gapY:scEnd,footerY=gridEnd+44,H=footerY+34;
    var cv=document.createElement("canvas");cv.width=W*s;cv.height=H*s;
    var c=cv.getContext("2d");c.scale(s,s);c.textBaseline="alphabetic";c.textAlign="left";
    c.imageSmoothingEnabled=true;c.imageSmoothingQuality="high";   // proper resample of the logo, not a cheap box filter
    function finish(){cv.toBlob(function(bl){if(!bl){alert("Image export failed — try a screenshot.");return;}var a=document.createElement("a");a.href=URL.createObjectURL(bl);a.download="builder-profile.png";a.click();});}
    c.fillStyle="#edeff2";c.fillRect(0,0,W,H);c.fillStyle=beak;c.fillRect(0,0,W,8);
    var bx0=CARD.logo?M+76:M;
    c.fillStyle=slate;c.font="700 26px -apple-system,sans-serif";c.fillText("Roadmap",bx0,64);
    c.font="600 13px -apple-system,sans-serif";var bt="Generated locally · nothing uploaded",btw=c.measureText(bt).width,btx=W-M-btw;
    c.save();c.strokeStyle=mut;c.fillStyle=mut;c.lineWidth=1.5;c.beginPath();c.arc(btx-12,55,3,Math.PI,2*Math.PI);c.stroke();rr(c,btx-17,55,10,8,2);c.fill();c.restore();
    c.fillStyle=mut;c.fillText(bt,btx,61);
    c.fillStyle=beakD;c.font="700 13px -apple-system,sans-serif";if(c.letterSpacing!==undefined)c.letterSpacing="2px";c.fillText("YOUR BUILDER PROFILE",M,heroY);if(c.letterSpacing!==undefined)c.letterSpacing="0px";
    c.font="800 "+afs+"px Georgia,serif";c.fillStyle=beak;c.fillText(CARD.arch+".",M,archB);
    c.fillStyle="#3b444b";c.font="italic 27px Georgia,serif";for(var ti=0;ti<tll.length;ti++)c.fillText(tll[ti],M,tagY+ti*tagLH);
    if(CARD.context){c.fillStyle=mut;c.font="500 15px -apple-system,sans-serif";c.fillText(CARD.context,M,ctxY);}
    box(c,M,scY,IW,scH);
    var pad=40,ix=M+pad,half=IW/2;
    c.fillStyle=mut;c.font="700 13px -apple-system,sans-serif";if(c.letterSpacing!==undefined)c.letterSpacing="1px";c.fillText("GSTACK SCORECARD",ix,scY+50);
    var stx=M+half+38;c.fillText("BY THE NUMBERS",stx,scY+50);if(c.letterSpacing!==undefined)c.letterSpacing="0px";
    var sc=CARD.scores,base=scY+96,rh=64,barL=ix+150,barR=M+half-86,valX=barR+46;
    for(var i=0;i<sc.length;i++){var ry=base+i*rh,nm=sc[i][0],vl=sc[i][1],note=sc[i][2]||"";
      c.fillStyle=slate;c.font="700 16px -apple-system,sans-serif";c.fillText(nm,ix,ry);
      var bw2=barR-barL,bh=13,by=ry-13;c.fillStyle=track;rr(c,barL,by,bw2,bh,6);c.fill();
      var g=c.createLinearGradient(barL,0,barL+bw2,0);g.addColorStop(0,beakD);g.addColorStop(1,beak);c.fillStyle=g;rr(c,barL,by,Math.max(bh,bw2*(vl/10)),bh,6);c.fill();
      c.fillStyle="#16191d";c.font="800 17px ui-monospace,Menlo,monospace";c.textAlign="right";c.fillText(Number(vl).toFixed(1),valX,ry);c.textAlign="left";
      c.fillStyle=mut;c.font="400 13px -apple-system,sans-serif";c.fillText(note,ix,ry+22);}
    var dvx=M+half+4;c.strokeStyle=line;c.lineWidth=1;c.beginPath();c.moveTo(dvx,scY+38);c.lineTo(dvx,scY+scH-34);c.stroke();
    var stt=CARD.stats||[];for(var j=0;j<stt.length&&j<sc.length;j++){var syy=base+j*rh;
      c.fillStyle=slate;c.font="800 30px ui-monospace,Menlo,monospace";c.fillText(stt[j][0],stx,syy);var w2=c.measureText(stt[j][0]).width;
      c.fillStyle=mut;c.font="500 14px -apple-system,sans-serif";c.fillText(stt[j][1],stx+w2+12,syy);}
    var gapX=16,colW=(IW-(gc-1)*gapX)/gc;
    for(var i=0;i<cards.length;i++){var row=Math.floor(i/gc),col=i%gc;
      var inRow=Math.min(gc,cards.length-row*gc);            // cards in THIS row (last row may be partial)
      var x0=M+(IW-(inRow*colW+(inRow-1)*gapX))/2;           // center the row → partial rows don't jam left
      mini(c,x0+col*(colW+gapX),gridY+row*(cardH+gapY),colW,cardH,cards[i]);}
    c.fillStyle=mut;c.font="500 14px -apple-system,sans-serif";c.textAlign="center";c.fillText("Generated 100% on-device — nothing uploaded · github.com/Photobombastic/paxel-local",W/2,footerY);c.textAlign="left";
    if(CARD.logo){var im=new Image();im.onload=function(){
      var k=Math.min(54/im.width,54/im.height),lw=im.width*k,lh=im.height*k;   // contain-fit: never squish the logo's aspect
      c.drawImage(im,M+(54-lw)/2,32+(54-lh)/2,lw,lh);finish();};im.onerror=finish;im.src=CARD.logo;}else{finish();}
  }catch(e){alert("Image export failed — try a screenshot.");}
});''')
    P("})();</script></body></html>")
    with open(os.path.join(OUT_DIR, "profile.html"), "w") as f:
        f.write("\n".join(parts))


if __name__ == "__main__":
    main()
