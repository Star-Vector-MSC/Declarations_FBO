import unittest

import planner as P
from test_planner import PK, T, snap


def plan(trows, prows, **kw):
    return P.build_plan(snap(trows, prows), {}, "2026-11-05", "WH", None, **kw)


class MinimumLineTest(unittest.TestCase):
    def test_tiny_need_that_cannot_reach_the_minimum_is_skipped(self):
        # needs ~43 pcs (5.7 -> 10 days at 10/day); lifting to 80 would give 13.7 days > max 13
        r = plan([T("w1", "A1", s=(10, 10, 10, 10), fc=5.7)], [PK("w1", box=20, pal=600)])
        self.assertEqual(r["lines"], [])
        self.assertTrue(any("below 50" in w for w in r["warnings"]))

    def test_unliftable_line_of_at_least_50_is_kept(self):
        # needs ~60 pcs (4 -> 10 days at 10/day); lifting to 80 would give 12 days... use a case that cannot lift
        r = plan([T("w1", "A1", s=(10, 10, 10, 10), fc=4.0)], [PK("w1", box=20, pal=600)], min_line_qty=200)
        self.assertEqual([ln["quantity"] for ln in r["lines"]], [60])

    def test_small_line_is_lifted_to_the_minimum_when_it_fits(self):
        r = plan([T("w1", "A1", s=(12, 12, 12, 12), fc=6.1)], [PK("w1", box=20, pal=600)])
        self.assertEqual([ln["quantity"] for ln in r["lines"]], [80])
        self.assertLessEqual(r["lines"][0]["forecast_after"], 13)
        self.assertIn("lifted from", r["lines"][0]["reason"])

    def test_small_pallet_wild_gets_half_a_pallet_not_one_piece(self):
        r = plan([T("w1", "A1", s=(29, 29, 29, 29), fc=9.5)], [PK("w1", box=1, pal=72)])
        self.assertEqual([ln["quantity"] for ln in r["lines"]], [36])

    def test_lines_at_or_above_the_minimum_are_untouched(self):
        a = plan([T("w1", "A1", s=(20, 20, 20, 20), fc=2)], [PK("w1", box=10, pal=400)])
        b = plan([T("w1", "A1", s=(20, 20, 20, 20), fc=2)], [PK("w1", box=10, pal=400)], min_line_qty=0)
        self.assertEqual(a["lines"], b["lines"])
        self.assertGreaterEqual(a["lines"][0]["quantity"], 80)

    def test_fixed_lines_are_not_subject_to_the_minimum(self):
        s = snap([T("w1", "A1", s=(12, 12, 12, 12), fc=6.1)], [PK("w1", box=20, pal=600)])
        r = P.build_plan(s, {"fixed": [{"article": "A1", "cabinet": "C1", "quantity": 20, "reason": "promo"}]},
                         "2026-11-05", "WH", None)
        self.assertEqual([ln["quantity"] for ln in r["lines"]], [20])


if __name__ == "__main__":
    unittest.main()
