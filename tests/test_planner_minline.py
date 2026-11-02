import unittest

import planner as P
from test_planner import PK, T, snap


def plan(trows, prows, **kw):
    return P.build_plan(snap(trows, prows), {}, "2026-11-05", "WH", None, **kw)


class MinimumLineTest(unittest.TestCase):
    def test_tiny_need_that_cannot_reach_the_minimum_is_skipped(self):
        # needs ~43 pcs (5.7 -> 10 days at 10/day); lifting to 80 would give 13.7 days > max 13
        r = plan([T("w1", "A1", s=(10, 10, 10, 10), fc=5.7)], [PK("w1", box=20, pal=600)], min_line_qty=80)
        self.assertEqual(r["lines"], [])
        self.assertTrue(any("below 50" in w for w in r["warnings"]))

    def test_unliftable_line_of_at_least_50_is_kept(self):
        # needs ~60 pcs (4 -> 10 days at 10/day); lifting to 80 would give 12 days... use a case that cannot lift
        r = plan([T("w1", "A1", s=(10, 10, 10, 10), fc=4.0)], [PK("w1", box=20, pal=600)], min_line_qty=200)
        self.assertEqual([ln["quantity"] for ln in r["lines"]], [60])

    def test_small_line_is_lifted_to_the_minimum_when_it_fits(self):
        r = plan([T("w1", "A1", s=(12, 12, 12, 12), fc=6.1)], [PK("w1", box=20, pal=600)], min_line_qty=80)
        self.assertEqual([ln["quantity"] for ln in r["lines"]], [80])
        self.assertLessEqual(r["lines"][0]["forecast_after"], 13)
        self.assertIn("lifted from", r["lines"][0]["reason"])

    def test_small_pallet_wild_gets_half_a_pallet_not_one_piece(self):
        r = plan([T("w1", "A1", s=(29, 29, 29, 29), fc=9.5)], [PK("w1", box=1, pal=72)], min_line_qty=80)
        self.assertEqual([ln["quantity"] for ln in r["lines"]], [36])

    def test_lines_at_or_above_the_minimum_are_untouched(self):
        a = plan([T("w1", "A1", s=(20, 20, 20, 20), fc=2)], [PK("w1", box=10, pal=400)], min_line_qty=80)
        b = plan([T("w1", "A1", s=(20, 20, 20, 20), fc=2)], [PK("w1", box=10, pal=400)], min_line_qty=0)
        self.assertEqual(a["lines"], b["lines"])
        self.assertGreaterEqual(a["lines"][0]["quantity"], 80)

    def test_fixed_lines_are_not_subject_to_the_minimum(self):
        s = snap([T("w1", "A1", s=(12, 12, 12, 12), fc=6.1)], [PK("w1", box=20, pal=600)])
        r = P.build_plan(s, {"fixed": [{"article": "A1", "cabinet": "C1", "quantity": 20, "reason": "promo"}]},
                         "2026-11-05", "WH", None, min_line_qty=80)
        self.assertEqual([ln["quantity"] for ln in r["lines"]], [20])


    def test_lift_is_off_by_default_but_the_floor_is_on(self):
        # 40 pcs of a 600-pcs-pallet item: not lifted (off), and dropped by the 50-pcs floor
        r = plan([T("w1", "A1", s=(10, 10, 10, 10), fc=5.7)], [PK("w1", box=20, pal=600)])
        self.assertEqual(r["lines"], [])
        self.assertTrue(any("below 50 and below half a pallet" in w for w in r["warnings"]))

    def test_floor_keeps_50_plus_and_half_pallets_and_can_be_switched_off(self):
        ok = plan([T("w1", "A1", s=(10, 10, 10, 10), fc=4.0)], [PK("w1", box=10, pal=600)])  # needs 60 pcs
        self.assertEqual([ln["quantity"] for ln in ok["lines"]], [60])
        half = plan([T("w1", "A1", s=(29, 29, 29, 29), fc=9.5)], [PK("w1", box=1, pal=72)])  # 18 pcs = 1/4 pallet
        self.assertEqual(half["lines"], [])  # a quarter of a small pallet is still < 50 and < half
        off = plan([T("w1", "A1", s=(10, 10, 10, 10), fc=5.7)], [PK("w1", box=20, pal=600)], floor_qty=0)
        self.assertEqual([ln["quantity"] for ln in off["lines"]], [40])


class RatingTest(unittest.TestCase):
    def build(self, ratings, **kw):
        s = snap([T("w1", "A1", fc=2), T("w2", "A2", fc=2)], [PK("w1"), PK("w2")])
        if ratings is not None:
            s["ratings"] = ratings
        return P.build_plan(s, {}, "2026-11-05", "WH", None, **kw)

    def test_low_rating_is_not_shipped_but_unknown_or_good_is(self):
        r = self.build({"A1": 3.54, "A2": 4.6})
        self.assertEqual([ln["article"] for ln in r["lines"]], ["A2"])
        self.assertTrue(any("rating 3.54 is below 4.0" in w for w in r["warnings"]))
        self.assertEqual(len(self.build({"A1": 4.0})["lines"]), 2)  # exactly the threshold passes
        self.assertEqual(len(self.build({})["lines"]), 2)  # unknown rating = no data: not a reason to refuse
        self.assertEqual(len(self.build(None)["lines"]), 2)  # ratings unreadable: no filtering
        self.assertEqual(len(self.build({"A1": 3.54}, min_rating=3)["lines"]), 2)


if __name__ == "__main__":
    unittest.main()


class CrumbTest(unittest.TestCase):
    def build(self, **kw):
        # a fixed line takes 19 of 20 pallets; w2 needs ~5000 pcs but only 1 pallet (1000 pcs) of space is left
        trows = [T("w1", "A1", stock=100000, s=(10, 10, 10, 10), fc=0), T("w2", "A2", stock=100000, s=(1000, 1000, 1000, 1000), fc=5)]
        prows = [PK("w1", box=10, pal=100), PK("w2", box=10, pal=1000)]
        rules = {"capacity": 20, "fixed": [{"article": "A1", "cabinet": "C1", "quantity": 1900, "reason": "x"}]}
        return P.build_plan(snap(trows, prows), rules, "2026-11-05", "WH", None, min_line_qty=0, **kw)

    def test_leftover_sliver_is_not_shipped_as_a_crumb(self):
        r = self.build()
        self.assertEqual([ln["article"] for ln in r["lines"]], ["A1"])
        self.assertTrue(any("A2" in w and "only" in w for w in r["warnings"]))

    def test_threshold_can_be_switched_off(self):
        self.assertEqual([ln["article"] for ln in self.build(min_cover=0)["lines"]], ["A1", "A2"])
