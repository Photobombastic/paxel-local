# paxel-local

A fully-local recreation of YC's **Paxel** builder-profile tool. Same insights, nothing
leaves your machine.

## Why this exists

[Paxel](https://paxel.ycombinator.com/) reads your AI coding-agent session transcripts and
emails you a "how you build with AI" profile. The catch is in YC's own description: it runs
in a Docker container that mounts your home directory, **sends transcript excerpts (prompts,
agent responses, tool-call snippets) to an LLM proxy**, and **uploads a JSON of scores,
narratives, and session metadata to YC** (readable by any YC employee, retained indefinitely).

Your transcripts can contain private code, customer data, secrets, and unreleased ideas — and
Paxel's redaction, by its own docs, only strips credentials, not any of that. If you'd rather
not ship it off your laptop, this reproduces the same *output* with **zero data leaving the
machine**.

## Is this "exactly" Paxel?

No — and it can't be. **Paxel is closed-source** (a `curl | bash` → proprietary Docker image).
This is a *functional* recreation: it reproduces the metric set Paxel advertises (builder
archetype, autonomy score, planning ratio, code velocity, tool diversity, work-hour
distribution, error recovery, iteration depth, standout traits) using its own reasonable
formulas — Paxel's exact algorithm isn't published. Same *input* (your local transcripts),
same *experience*, not byte-for-byte parity.

## What you get

One command emits a complete, **branded, shareable `profile.html`** — open it in a browser
and you get an archetype, a 0–10 scorecard, a "what we noticed" card grid, and Share buttons
(Post on X / Copy caption / Download image). No manual step, no LLM call required.

The archetype and scores come from a **transparent rubric** (`compute_scores` / `pick_archetype`
in `paxel.py`) over the measured metrics — Paxel's real algorithm is closed, so this is a
reasoned estimate, not a replica. The counts are measured and reproducible; the scores are an
opinion, and the report says so.

Want a richer, prose narrative? `narrative_input.md` is also written — paste it into your own
Claude/GPT and it'll write you a deeper profile locally. That's optional; the HTML stands alone.

## Sources

Auto-detected and parsed (all reads local):

| Tool | Location | Status |
|---|---|---|
| **Claude Code** | `~/.claude/projects` | full |
| **Codex CLI** | `~/.codex/sessions` | full |
| **Gemini CLI** | `~/.gemini/tmp` | full |
| Cursor | `…/Cursor/.../state.vscdb` | detected, experimental (SQLite blobs — not yet parsed) |
| opencode | `~/.local/share/opencode` | detected, experimental (KV store — not yet parsed) |

Non-Claude formats are translated into a common event shape so every metric works across
tools (Claude-specific signals like skills/subagents/thinking are naturally richer).

## Run it

```bash
python3 paxel.py            # all detected sources → writes profile.html (+ report.md, stats.json)
python3 paxel.py claude     # restrict to one (or several) sources, e.g. just Claude Code
```

Then open `profile.html` in your browser. No dependencies beyond the Python 3 standard
library. **No network calls anywhere.** For
accurate churn it shells out to the local `git` CLI (`git log --numstat`) on the repos found
in your transcripts — still 100% on-device, nothing uploaded.

## How churn is measured (and why it matters)

Most "how you build" profilers only see the assistant's `Edit`/`Write` tool calls. But a huge
amount of real work happens through the **shell** — `cat <<EOF > file` heredocs, `>`/`>>`
redirects, `sed -i`, scripts that generate files. That work is invisible to the tool-call
path, which makes shell-heavy ("brute-force") builders look artificially clean.

So this reports churn three ways, honestly:

1. **Git churn (gold standard)** — `git log --numstat` over your authored commits in the
   window, deduped by repo identity (root commit) so multiple clones aren't double-counted.
   Captures *every* committed change however it was made. **Caveat:** only covers repos still
   on disk — the report tells you the coverage (e.g. "4/13 repos"), because work done in
   directories that no longer exist can't be counted.
2. **Tool churn** — lines via `Edit`/`Write`/`MultiEdit`. What naive profilers show.
3. **Shell-authored estimate** — file-writing Bash calls + lines of heredoc/redirect content.

Iteration depth is reported as mean / median / p90 / **max** (a single mean hides the
"hammered one file 100+ times" tail), and errors as a rate, so brute-forcing reads as
brute-forcing.

## Outputs

| file | contents |
|---|---|
| `report.md` | deterministic stats, human-readable |
| `stats.json` | all metrics, machine-readable |
| `narrative_input.md` | curated excerpts for the narrative pass — **stays local; may contain private content from your own prompts** |
| `profile.html` | **the deliverable** — branded, shareable builder profile (open in a browser) |

> **Note:** every output stays on your machine. Add them to `.gitignore` (this repo does) so
> you never accidentally commit your own data.

## Scope decisions

- **Multi-source** (Claude Code, Codex, Gemini), with per-source selection via args.
  Cursor/opencode are blob/KV stores that need real reverse-engineering — detected and
  flagged, not faked.
- **One-shot.** Just re-run to rebuild as sessions accumulate.

## Notable implementation details (faithfulness)

- **Genuine prompts** exclude `isMeta`, `isCompactSummary`, tool-results, and `isSidechain`
  subagent-dispatch instructions — only human-typed turns count.
- **Active time** uses capped inter-event gaps (10-min cap), *not* raw session span, because
  `sessionId` is reused across resumed sessions spanning weeks (raw span over-inflates time).
- **Subagent work counts** toward tool/churn totals (it's work you delegated) but never toward
  your prompt count.
- Timestamps are converted UTC → local timezone for the work-hour histogram.
- The **archetype and 0–10 axis scores are interpretive** (Paxel's rubric is closed). The
  *counts* are measured and reproducible; the scores are an opinion laid on top.

## Known limitations — PRs welcome 🐦

Honest about what it can't see. If you can close one of these, open a PR:

- **`sed -i` / runtime-generated files** — a command like `python build.py` writes files whose
  content never appears in the transcript, so the shell-authored estimate misses it. Git churn
  catches it *if* it was committed in a repo still on disk.
- **`~/.claude/history.jsonl`** (a separate flat prompt log) isn't parsed yet.
- **Cursor** (SQLite `state.vscdb` blobs) and **opencode** (KV store) are detected but not yet
  parsed — reverse-engineering either into the common event shape is a great first PR.
- **Codex tool churn** from `apply_patch` counts raw patch lines (diff markers included), so it
  over-estimates; the gold-standard git churn is unaffected.
- **Archetype scoring** is a hand-rolled rubric — a better, evidence-grounded scorer would be a
  great contribution.

Issues and pull requests welcome.
