"""Adversarial scoring profiles — the ground truth the scoring algorithm is held to.

These are NOT real transcripts. Each is a hand-built `stats` dict (the same shape
`compute_scores` consumes) that encodes a specific way the score could be WRONG. They
exist so that any change to the scoring math has to survive a battery of "...but a
<bad actor> must not out-score a <real builder>" checks. See test_scoring_invariants.py.

Why synthetic stats instead of transcripts? Because the interesting failures live in the
SCORING, not the parsing. A synthetic stats dict lets us dial exactly one trait (volume,
verbosity, sloppiness, autonomy) and prove the score does — or doesn't — move with it.

The honest framing (read CONTRIBUTING.md): paxel measures AI-usage STYLE and VOLUME, not
skill. So the ground truth here is never "an expert must score 9." It's RELATIONAL and
anti-gaming: "raising volume alone must not raise a score," "a spammer must not out-score a
shipper," "being hands-off must not cost you points." Each profile below is the frozen
memory of a real scoring bug we already shipped and fixed — kept as a test so it can't come
back. If you think a score is wrong, don't just reweight a coefficient: add a profile here
that encodes what SHOULD be true, and show the current math violates it.
"""

# Every key `compute_scores` / `steering_reading` / `_evidence` actually reads. Defaults are
# a deliberately NEUTRAL mid-corpus; each named profile overrides only its load-bearing traits
# so the contrast under test is obvious. `_ev`-gated terms need tool_calls_total >= ~2000 to
# reach full evidence (below that the inverse terms are pulled toward 0.5 — see THIN).
_DEFAULTS = dict(
    # volume
    total_sessions=30, tool_calls_total=5000, total_prompts=300, thinking_blocks=240,
    # behavior
    planning_ratio_explore_to_doing=0.4, iteration_depth_mean=3.0, iteration_depth_p90=5.0,
    files_hammered_over_15x=3, error_rate_per_100_tools=3.0, delegate_actions=30,
    background_tasks=10, actions_per_prompt=8.0, questions_asked=30, shell_test_runs=5,
    # velocity
    active_hours=60.0, git_repos_with_commits=6, git_repos_seen=8, git_churn_total=40000,
    tool_churn_edit_write=60000, shell_authored_lines_est=2000,
    # stack
    top_skills=(("plan", 6), ("code-review", 6), ("test", 6)),
)


def make_stats(**overrides):
    """Build a full `stats` dict for the scorer from NEUTRAL defaults + overrides.
    Unknown keys raise — so a typo in a profile fails loudly instead of silently
    scoring against a default."""
    bad = set(overrides) - set(_DEFAULTS)
    if bad:
        raise KeyError(f"unknown stats field(s): {sorted(bad)}")
    f = {**_DEFAULTS, **overrides}
    return {
        "volume": {
            "total_sessions": f["total_sessions"],
            "tool_calls_total": f["tool_calls_total"],
            "total_prompts": f["total_prompts"],
            "thinking_blocks": f["thinking_blocks"],
        },
        "behavior": {
            "planning_ratio_explore_to_doing": f["planning_ratio_explore_to_doing"],
            "iteration_depth_mean": f["iteration_depth_mean"],
            "iteration_depth_p90": f["iteration_depth_p90"],
            "files_hammered_over_15x": f["files_hammered_over_15x"],
            "error_rate_per_100_tools": f["error_rate_per_100_tools"],
            "delegate_actions": f["delegate_actions"],
            "background_tasks": f["background_tasks"],
            "actions_per_prompt": f["actions_per_prompt"],
            "questions_asked": f["questions_asked"],
            "shell_test_runs": f["shell_test_runs"],
        },
        "velocity": {
            "active_hours": f["active_hours"],
            "git_repos_with_commits": f["git_repos_with_commits"],
            "git_repos_seen": f["git_repos_seen"],
            "git_churn_total": f["git_churn_total"],
            "tool_churn_edit_write": f["tool_churn_edit_write"],
            "shell_authored_lines_est": f["shell_authored_lines_est"],
        },
        "stack": {"top_skills": list(f["top_skills"])},
    }


# Counts that grow with sheer volume — scaled together by scale_volume() to prove that
# "do more of the same" does NOT inflate a score (everything here is a rate under the hood).
# RATES and DEPTHS (planning_ratio, iteration_depth_*, error_rate, actions_per_prompt) are
# deliberately NOT in this list: they're intensive, not extensive.
_VOLUME_FIELDS = ("total_sessions", "tool_calls_total", "total_prompts", "thinking_blocks",
                  "delegate_actions", "background_tasks", "questions_asked", "shell_test_runs",
                  "active_hours", "git_churn_total", "tool_churn_edit_write",
                  "shell_authored_lines_est", "files_hammered_over_15x")


def scale_volume(profile_overrides, k):
    """Return a copy of a profile's overrides with every extensive (count-like) field
    multiplied by k, leaving rates/depths untouched. Used by the anti-gaming invariant:
    a 3x-busier copy of the SAME builder must score the same, not higher."""
    out = dict(profile_overrides)
    for key in _VOLUME_FIELDS:
        if key in out:
            out[key] = type(out[key])(out[key] * k)
    if "top_skills" in out:
        out["top_skills"] = tuple((name, int(n * k)) for name, n in out["top_skills"])
    return out


# ---------------------------------------------------------------------------------------
# The profiles. Each docstring names the real bug it freezes. Stored as override-dicts (not
# built stats) so scale_volume() can transform them before make_stats().
# ---------------------------------------------------------------------------------------

# The honest baseline: a senior builder who writes TERSE prompts, ships most of what the
# agent generates (high git fidelity), rarely re-hammers a file (low rework), runs tests,
# and lets the agent run (hands-off). The "30-year engineer / Chris Sells" stand-in — the
# one the old inverted Steering axis scored 1/10. Everyone below is measured against this.
EXPERT = dict(
    total_sessions=40, tool_calls_total=6000, total_prompts=400, thinking_blocks=320,
    planning_ratio_explore_to_doing=0.5, iteration_depth_mean=2.2, iteration_depth_p90=3.0,
    files_hammered_over_15x=2, error_rate_per_100_tools=2.0, delegate_actions=80,
    background_tasks=20, actions_per_prompt=15.0, questions_asked=40, shell_test_runs=20,
    active_hours=80.0, git_repos_with_commits=8, git_repos_seen=10, git_churn_total=80000,
    tool_churn_edit_write=120000, shell_authored_lines_est=2000,
    top_skills=(("code-review", 10), ("test", 15), ("plan", 8), ("brainstorm", 4)),
)

# High VOLUME, ships nothing. Thousands of prompts and tool calls, near-zero committed git
# churn, sloppy and shallow. The "9.4 spammer" a 4-agent review caught gaming the old
# autonomous-command term. Must not out-score a real shipper on anything.
SPAMMER = dict(
    total_sessions=60, tool_calls_total=12000, total_prompts=2000, thinking_blocks=60,
    planning_ratio_explore_to_doing=0.05, iteration_depth_mean=8.0, iteration_depth_p90=14.0,
    files_hammered_over_15x=20, error_rate_per_100_tools=12.0, delegate_actions=0,
    background_tasks=0, actions_per_prompt=6.0, questions_asked=5, shell_test_runs=0,
    active_hours=100.0, git_repos_with_commits=0, git_repos_seen=3, git_churn_total=200,
    tool_churn_edit_write=5000, shell_authored_lines_est=1000,
    top_skills=(),
)

# Talks and "thinks" constantly but never actually explores-before-building (low planning
# ratio) and ships little. Freezes the avg_prompt_length lesson: verbosity is NOT planning.
# An expert-elicitation validity review found prompt length is experience-INVERTING (experts
# prompt terser), so the term was dropped. This must not beat the terse EXPERT on Planning.
VERBOSE_RAMBLER = dict(
    total_sessions=50, tool_calls_total=7000, total_prompts=1500, thinking_blocks=1200,
    planning_ratio_explore_to_doing=0.10, iteration_depth_mean=4.0, iteration_depth_p90=7.0,
    files_hammered_over_15x=8, error_rate_per_100_tools=5.0, delegate_actions=10,
    background_tasks=5, actions_per_prompt=4.0, questions_asked=200, shell_test_runs=0,
    active_hours=90.0, git_repos_with_commits=2, git_repos_seen=6, git_churn_total=8000,
    tool_churn_edit_write=50000, shell_authored_lines_est=1500,
    top_skills=(),
)

# Ships a LOT but sloppily: high committed churn (real Execution) yet deep rework, hammered
# files, and a high error rate. Honest outcome: Execution can be high, but Engineering (craft
# / low rework) must NOT be — output volume is not craft.
ERRORING_FACTORY = dict(
    total_sessions=50, tool_calls_total=8000, total_prompts=500, thinking_blocks=200,
    planning_ratio_explore_to_doing=0.2, iteration_depth_mean=9.0, iteration_depth_p90=15.0,
    files_hammered_over_15x=25, error_rate_per_100_tools=14.0, delegate_actions=20,
    background_tasks=10, actions_per_prompt=10.0, questions_asked=15, shell_test_runs=0,
    active_hours=80.0, git_repos_with_commits=6, git_repos_seen=7, git_churn_total=120000,
    tool_churn_edit_write=150000, shell_authored_lines_est=3000,
    top_skills=(),
)

# Two builders with IDENTICAL output, differing ONLY in how hands-on they are: the delegator
# points the agent and lets it run (actions_per_prompt=20); the twin course-corrects every
# few actions (actions_per_prompt=3). Steering was demoted from a scored axis to a DESCRIBED
# reading precisely because grading it INVERTED the axis (autonomy scored LOWER). So these two
# must score IDENTICALLY on all three graded axes — autonomy is neither rewarded nor punished —
# while steering_reading still DESCRIBES them differently (long vs short leash).
HANDS_OFF_DELEGATOR = dict(
    actions_per_prompt=20.0, delegate_actions=90, background_tasks=30,
    git_churn_total=70000, tool_churn_edit_write=100000, iteration_depth_mean=2.5,
    error_rate_per_100_tools=2.0, top_skills=(("code-review", 8), ("test", 10)),
)
HANDS_ON_TWIN = {**HANDS_OFF_DELEGATOR, "actions_per_prompt": 3.0}

# No activity at all. Must score a flat 0/0/0 — never a flattering default (e.g. the old
# "Quality Guardian 9.0" that absence-of-bad-signal used to manufacture).
EMPTY = dict(total_sessions=0, tool_calls_total=0)

# A barely-used corpus with "perfect" (i.e. absent) bad-metrics: zero rework, zero errors,
# zero hammered files — but almost no evidence. The `_ev` gate must pull the inverse terms
# toward neutral so this reads as HUMBLE (mid), not flawless. Must score below EXPERT.
THIN = dict(
    total_sessions=2, tool_calls_total=200, total_prompts=15, thinking_blocks=8,
    iteration_depth_mean=2.0, iteration_depth_p90=3.0, files_hammered_over_15x=0,
    error_rate_per_100_tools=0.0, git_churn_total=1500, tool_churn_edit_write=2000,
    active_hours=2.0, top_skills=(),
)
