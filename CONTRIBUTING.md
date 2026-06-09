# Contributing to paxel-local 🐦

Thanks for being here. paxel reads your local AI-coding transcripts and emits a shareable
builder profile — **fully locally, no network calls, ever.** That constraint is the whole
project, so it shapes everything below.

There are two very different kinds of contribution, and they're held to different bars.

---

## House rules (both tiers)

- **Local-only, stdlib-only.** No network calls. No third-party pip dependencies — the
  standard library is the whole toolbox (that's why the tests use `unittest`, not `pytest`).
  The only subprocess paxel runs is local `git`.
- **No real data in the repo.** Fixtures must be synthetic. Never commit a real `profile.html`,
  `stats.json`, or anyone's transcript — they're gitignored for a reason.
- **It has to run as one file.** `paxel.py` is deliberately a single script you can read
  top-to-bottom. Keep new sources/metrics inside it unless there's a strong reason not to.
- **Tests are part of "done."** `python3 -m unittest discover -s tests` must stay green, and
  new behavior comes with a fixture or an invariant. CI (GitHub Actions) runs it on every PR.

---

## Tier 1 — more sources & signals (great first PRs)

This is the safe, additive, mechanical work, and it's genuinely valuable. "Can paxel see more
of what I did?" There's a ground truth (does it parse? are the numbers coherent?), so these are
easy to review and easy to merge.

Open invitations (see **Known limitations** in the README for the live list):

- **A new agent source.** A coding agent paxel doesn't read yet. Pi, opencode, and Cursor all
  arrived this way. The pattern: write a translator that turns the tool's transcripts into
  Claude-shaped event dicts, so the one aggregation loop in `main()` works unchanged.
- **A blind spot in an existing source.** e.g. `~/.claude/history.jsonl`, the Bash-only builder
  whose work is invisible to the Edit/Write churn signal, Windows path coverage.

**How:** drop a tiny synthetic fixture under `tests/fixtures/<your-source>/` matching the real
on-disk layout, add it to `SRC_DIRS` + `EXPECTED_SOURCES` in `tests/test_smoke.py`, and the
end-to-end harness covers you. That's the whole bar: it parses, it produces a valid profile,
the numbers are sane.

---

## Tier 2 — changing what gets *measured* (the hard, interesting one)

This is the part most worth getting right, and the part where good intentions do the most
damage. **Read this whole section before touching `compute_scores`.**

### The honest frame

paxel measures **AI-usage style and volume, not skill.** Several axes can run *opposite* to
seniority — experts prompt terser, ship cleaner, and lean on the agent harder, and a naive
metric reads each of those as "less." We know this firsthand: we shipped, and then had to kill,
a scoring term that gave a spammer a 9.4, and a "Steering" axis whose math scored a 30-year
engineer **1 out of 10** for being hands-off. So the bar for a scoring change is not "does this
feel right on my own profile" — it's "does this survive the adversarial suite."

### Rules of engagement

A change to the scoring math is welcome **if it can clear all of these:**

1. **It survives `tests/test_scoring_invariants.py`.** Those ten invariants are frozen memories
   of real bugs (a spammer out-scoring a shipper, volume inflating a score, verbosity buying
   "Planning," autonomy being penalized). If your change turns one red, that test is the lesson —
   read its docstring before you "fix" it.
2. **It can't be gamed by doing more.** If sending more prompts, or generating more uncommitted
   churn, raises the number, it's a vanity metric. The `test_volume_alone_does_not_inflate_score`
   invariant is the line.
3. **One metric, one axis.** Each signal owns exactly one graded axis (see the design rules atop
   `compute_scores`). No double-counting a metric into two scores to make them move together.
4. **No good/bad call you can't defend from the transcript.** If the data can't actually
   distinguish skill from style for a signal (e.g. hands-on vs hands-off cadence), **describe it,
   don't grade it** — that's why Steering is a `steering_reading()` sentence, not a 0–10.

### How to argue a score is wrong

Don't just reweight a coefficient and eyeball your own profile. **Encode the disagreement as a
test:**

1. Add a profile to `tests/adversarial_profiles.py` — a synthetic `stats` dict that isolates the
   trait you think is mis-scored (the docstrings there show the style: name the real-world builder
   it stands for).
2. Add an invariant to `tests/test_scoring_invariants.py` asserting what *should* be true —
   **relationally** ("X must not out-score Y"), not as a magic number ("expert == 9", which is
   exactly the construct error we're avoiding).
3. Show the current math violates it. Now we have something concrete to discuss, and your fix
   has a guard so it can't silently regress later.

That's the bait, honestly stated: the interesting open problem isn't adding sources, it's
**measurement validity** — can you design a signal that separates skill from style without
becoming gameable? If you can, you've made paxel meaningfully better. If you can't yet, a sharp
adversarial profile that *exposes* a blind spot is itself a real contribution.

---

Issues and pull requests welcome. Be honest about what your change can and can't see — that
honesty is the product.
