"""Scoring invariants — the contract any change to the scoring math must satisfy.

Each test freezes a real scoring bug we already shipped and fixed (see git history + the
profile docstrings). The assertions are RELATIONAL and anti-gaming, never "expert == 9":
paxel measures usage STYLE and VOLUME, not skill, so the ground truth is about what must
NOT happen (inflation by volume, a spammer out-scoring a shipper, autonomy costing points),
not about hitting a magic number.

If you're changing `compute_scores` and a test here goes red: that test is the lesson. Read
its docstring before you "fix the test." If you genuinely believe the invariant is wrong,
change it in its own commit with the reasoning — don't loosen it to make a coefficient pass.

Run: python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)   # for `import paxel`
sys.path.insert(0, HERE)   # for `import adversarial_profiles` (sibling helper)
import paxel  # noqa: E402
import adversarial_profiles as P  # noqa: E402


def score(overrides):
    return paxel.compute_scores(P.make_stats(**overrides))


AXES = ("Execution", "Planning", "Engineering")


class TestScoringInvariants(unittest.TestCase):

    def test_all_profiles_score_in_range(self):
        """I8 — bounded. Every axis stays in [0, 10] for every profile (no overflow/NaN
        from a future term that forgets to clamp)."""
        profiles = {
            "EXPERT": P.EXPERT, "SPAMMER": P.SPAMMER, "VERBOSE_RAMBLER": P.VERBOSE_RAMBLER,
            "ERRORING_FACTORY": P.ERRORING_FACTORY, "HANDS_OFF": P.HANDS_OFF_DELEGATOR,
            "HANDS_ON": P.HANDS_ON_TWIN, "THIN": P.THIN, "EMPTY": P.EMPTY,
        }
        for name, prof in profiles.items():
            s = score(prof)
            for axis in AXES:
                self.assertGreaterEqual(s[axis], 0.0, f"{name}.{axis} below 0")
                self.assertLessEqual(s[axis], 10.0, f"{name}.{axis} above 10")

    def test_volume_alone_does_not_inflate_score(self):
        """I1 — anti-gaming. A 3x-busier copy of the SAME builder (every count tripled, all
        rates identical) must NOT score higher. 'Send more to score more' is the single most
        important thing a usage profiler must refuse to reward."""
        base = score(P.EXPERT)
        busier = score(P.scale_volume(P.EXPERT, 3))
        for axis in AXES:
            self.assertAlmostEqual(
                busier[axis], base[axis], delta=0.15,
                msg=f"{axis} rose with raw volume ({base[axis]} -> {busier[axis]}): gameable")

    def test_spammer_never_outscores_expert(self):
        """I2 — a high-volume builder who ships nothing and is sloppy must not out-score a
        real shipper on Execution or Engineering. The '9.4 spammer' a 4-agent review caught."""
        expert, spammer = score(P.EXPERT), score(P.SPAMMER)
        self.assertLess(spammer["Execution"], expert["Execution"])
        self.assertLess(spammer["Engineering"], expert["Engineering"])

    def test_verbosity_does_not_buy_planning(self):
        """I3a — talking/'thinking' a lot without actually exploring-before-building must not
        beat the terse EXPERT on Planning. Freezes the dropped avg_prompt_length term."""
        self.assertLess(score(P.VERBOSE_RAMBLER)["Planning"], score(P.EXPERT)["Planning"])

    def test_prompt_length_is_ignored_by_scorer(self):
        """I3b — focused regression guard for the SAME lesson: the scorer must read no
        prompt-length signal at all. Two identical builders, one tagged with a long
        avg_prompt_length, must score identically. If someone re-introduces a verbosity term,
        this breaks first and loudest. (avg_prompt_length is not a key make_stats knows, so we
        inject it straight onto the stats dict — the scorer must simply never look at it.)"""
        s1 = P.make_stats(**P.EXPERT)
        s2 = P.make_stats(**P.EXPERT)
        s2["behavior"]["avg_prompt_length"] = 4000   # verbose; scorer must ignore it
        s1["behavior"]["avg_prompt_length"] = 40      # terse
        self.assertEqual(paxel.compute_scores(s1), paxel.compute_scores(s2))

    def test_prolific_but_sloppy_is_not_craft(self):
        """I4 — the erroring factory may EXECUTE well (it ships), but high rework + errors must
        keep Engineering (craft) low. Output volume is not craftsmanship."""
        factory, expert = score(P.ERRORING_FACTORY), score(P.EXPERT)
        self.assertLess(factory["Engineering"], expert["Engineering"])
        self.assertLess(factory["Engineering"], 4.0)

    def test_being_hands_off_is_never_penalized(self):
        """I5 — the Steering demotion, frozen. Two builders with IDENTICAL output, differing
        ONLY in how hands-on they are, must score IDENTICALLY on all graded axes (autonomy is
        described, not graded). Grading it once INVERTED the axis — autonomy scored LOWER."""
        self.assertEqual(score(P.HANDS_OFF_DELEGATOR), score(P.HANDS_ON_TWIN))

    def test_steering_is_described_not_scored(self):
        """I5b — the other half: steering_reading must still TELL them apart (long vs short
        leash) even though the scores don't. Described, not erased."""
        off = paxel.steering_reading(P.make_stats(**P.HANDS_OFF_DELEGATOR))
        on = paxel.steering_reading(P.make_stats(**P.HANDS_ON_TWIN))
        self.assertNotEqual(off["label"], on["label"])

    def test_empty_corpus_scores_zero_not_flattering(self):
        """I6 — no activity must yield a flat 0/0/0, never a manufactured 'Quality Guardian'
        from absence-of-bad-signal."""
        self.assertEqual(score(P.EMPTY), {"Execution": 0.0, "Planning": 0.0, "Engineering": 0.0})

    def test_thin_corpus_reads_humble_not_flawless(self):
        """I7 — a barely-used corpus with 'perfect' (absent) bad-metrics must score BELOW the
        well-evidenced EXPERT: the _ev gate pulls inverse terms toward neutral so 'no data'
        never reads as 'did it perfectly'."""
        self.assertLess(score(P.THIN)["Engineering"], score(P.EXPERT)["Engineering"])


if __name__ == "__main__":
    unittest.main()
