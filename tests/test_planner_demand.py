import json
import unittest

import planner as P
import planner_demand as D
from test_planner import PK, T, snap

REF = "2026-11-02T12:00:00+03:00"


def sheet(rows, newest="02.11", n=16):
    """Header: Артикул, wild, 'Среднее в день', then n date columns newest-left; rows give OLD->NEW values."""
    days = []
    import datetime as dt
    cur = dt.date(2026, 11, 2)
    for _ in range(n):
        days.append(cur.strftime("%d.%m"))
        cur -= dt.timedelta(days=1)
    head = ["Артикул", "wild", "Среднее в день"] + days + ["Среднее за 7 дней", "Тренд"]
    body = []
    for art, vals in rows:
        cells = [("" if v is None else v) for v in reversed(vals)]  # newest-left, includes the reading day
        body.append([art, "w1", 1] + cells + [0, ""])
    return [head] + body


def flat(v, n=15):
    return [v] * n


class ParseTest(unittest.TestCase):
    def test_today_dropped_and_blank_is_none(self):
        o = D.parse_orders(sheet([(1, [5, None] + [7] * 14)]), REF)
        self.assertTrue(o["ok"])
        self.assertEqual(o["dates"][-1], "2026-11-01")  # 02.11 is the incomplete reading day
        self.assertEqual(len(o["dates"]), 15)
        self.assertEqual(o["series"]["1"][:2], [5, None])

    def test_blank_never_zero(self):
        vals = [None] * 3 + [10] * 13
        s = D.parse_orders(sheet([(1, vals)]), REF)["series"]["1"]
        self.assertEqual(s[:2], [None, None])

    def test_stale_sheet_is_unusable(self):
        o = D.parse_orders(sheet([(1, flat(5, 16))]), "2026-11-20T12:00:00+03:00")
        self.assertFalse(o["ok"])
        self.assertIn("устарели", o["problems"][0])

    def test_non_consecutive_dates_unusable(self):
        s = sheet([(1, flat(5, 16))])
        s[0][5] = "20.09"
        self.assertFalse(D.parse_orders(s, REF)["ok"])

    def test_duplicate_article_is_uncertain(self):
        o = D.parse_orders(sheet([(1, flat(5, 16)), (1, flat(9, 16))]), REF)
        self.assertNotIn("1", o["series"])
        self.assertEqual(D.analyse(o, "1")["state"], D.UNCERTAIN)

    def test_year_rollover(self):
        d = D._header_day("30.12", __import__("datetime").date(2027, 1, 2))
        self.assertEqual(d.isoformat(), "2026-12-30")


class ClassifyTest(unittest.TestCase):
    def st(self, vals):
        return D.classify(vals)["state"]

    def test_states(self):
        self.assertEqual(self.st([10] * 7 + [10] * 7), D.STABLE)
        self.assertEqual(self.st([10] * 7 + [14] * 7), D.GROWTH)
        self.assertEqual(self.st([10] * 7 + [8] * 7), D.GENTLE)
        self.assertEqual(self.st([10] * 7 + [3] * 7), D.SHARP)

    def test_recent_drop_and_recovery(self):
        self.assertEqual(self.st([10] * 7 + [10] * 4 + [10, 10, 2]), D.SHARP)  # last day collapsed
        self.assertEqual(self.st([10] * 7 + [10] * 5 + [4, 4]), D.SHARP)  # last two days
        self.assertEqual(self.st([10] * 7 + [8, 8, 8, 8, 8, 8, 12]), D.STABLE)  # weekly dip, but already recovering

    def test_uncertain(self):
        self.assertEqual(self.st([10] * 10), D.UNCERTAIN)  # short history
        self.assertEqual(self.st([None] * 7 + [10] * 7), D.UNCERTAIN)  # a week is mostly blank, not zero
        self.assertEqual(self.st([10] * 7 + [0] * 7), D.UNCERTAIN)  # zeros may mean no stock
        self.assertEqual(self.st([10] * 7 + [1, 1, 1, 1, 1, 1, 100]), D.UNCERTAIN)  # single spike is not growth

    def test_uses_only_given_history(self):
        v = [10] * 14
        self.assertEqual(self.st(v + [1] * 7), D.classify(v + [1] * 7)["state"])
        self.assertEqual(self.st(v), D.STABLE)

    def test_backtest_uses_history_before_origin(self):
        res = D.backtest({"a": [10] * 14 + [20] * 14}, horizons=(7,))
        self.assertIn("all", res)
        self.assertGreater(res["all"][7][0], 0)


class PlanTest(unittest.TestCase):
    def build(self, state, mode="advisory", **kw):
        s = snap([T("w1", "A1", fc=2)], [PK("w1")])
        s["demand"] = {"ok": True, "problems": [], "by_article": {"A1": {"state": state, "why": "x"}}}
        return P.build_plan(s, {}, "2026-10-28", "WH", "2026-10-30", demand_mode=mode, min_line_qty=0, floor_qty=0)

    def test_advisory_never_changes_quantity(self):
        base = P.build_plan(snap([T("w1", "A1", fc=2)], [PK("w1")]), {}, "2026-10-28", "WH", "2026-10-30")
        self.assertNotIn("demand", base["lines"][0])
        for st in ("growth", "gentle_decline", "sharp_decline", "stable", "uncertain"):
            r = self.build(st)
            self.assertEqual([l["quantity"] for l in r["lines"]], [l["quantity"] for l in base["lines"]], st)
            self.assertEqual(r["lines"][0]["demand"]["state"], st)
            self.assertFalse(r["lines"][0]["demand"]["applied"])
            json.dumps(r, allow_nan=False)

    def test_apply_policy(self):
        base = self.build("stable", "apply")["lines"][0]
        self.assertEqual(self.build("sharp_decline", "apply")["lines"], [])
        self.assertTrue(any("no automatic replenishment" in w for w in self.build("sharp_decline", "apply")["warnings"]))
        g = self.build("growth", "apply")["lines"][0]
        self.assertGreaterEqual(g["quantity"], base["quantity"])
        self.assertLessEqual(g["forecast_after"], 13 + 1e-9)
        self.assertEqual(self.build("gentle_decline", "apply")["lines"], [])
        self.assertLessEqual(g["forecast_after"], 11 + 1e-9)
        self.assertLessEqual(base["forecast_after"], 8 + 1e-9)
        self.assertLessEqual(self.build("uncertain", "apply")["lines"][0]["forecast_after"], 8 + 1e-9)
        self.assertTrue(g["demand"]["applied"])

    def test_missing_article_in_demand_is_uncertain_flat_horizon(self):
        s = snap([T("w1", "A1", fc=2)], [PK("w1")])
        s["demand"] = {"ok": True, "problems": [], "by_article": {}}
        base = P.build_plan(snap([T("w1", "A1", fc=2)], [PK("w1")]), {}, "2026-10-28", "WH", "2026-10-30")
        adv = P.build_plan(s, {}, "2026-10-28", "WH", "2026-10-30", demand_mode="advisory", min_line_qty=0, floor_qty=0)
        self.assertEqual(adv["lines"][0]["quantity"], base["lines"][0]["quantity"])
        r = P.build_plan(s, {}, "2026-10-28", "WH", "2026-10-30", demand_mode="apply", min_line_qty=0, floor_qty=0)
        self.assertLessEqual(r["lines"][0]["forecast_after"], 8 + 1e-9)
        self.assertEqual(r["lines"][0]["demand"]["state"], "uncertain")

    def test_problems_become_warnings_and_bad_mode_rejected(self):
        s = snap([T("w1", "A1", fc=2)], [PK("w1")])
        s["demand"] = {"ok": False, "problems": ["данные устарели"], "by_article": {}}
        self.assertIn("demand: данные устарели", P.build_plan(s, {}, "2026-10-28", "WH")["warnings"])
        with self.assertRaises(ValueError):
            P.build_plan(s, {}, "2026-10-28", "WH", demand_mode="x")


class BlankTailTest(unittest.TestCase):
    def test_rows_without_any_identity_are_not_warnings_but_partial_ones_are(self):
        s = snap([T("w1", "A1", fc=2), [None] * 13, ["", "", "", "x"] + [None] * 9, ["w2", None, None] + [None] * 10],
                 [PK("w1")])
        r = P.build_plan(s, {}, "2026-10-28", "WH", "2026-10-30")
        w = [x for x in r["warnings"] if "missing or invalid wild/article/cabinet" in x]
        self.assertEqual(len(w), 1)
        self.assertIn("test row 5", w[0])


class UiTest(unittest.TestCase):
    def test_lines_text_shows_demand_state(self):
        import planner_ui
        s = snap([T("w1", "A1", fc=2)], [PK("w1")])
        s["demand"] = {"ok": True, "problems": [], "by_article": {"A1": {"state": "growth", "why": "x2"}}}
        plan = P.build_plan(s, {}, "2026-10-28", "WH", "2026-10-30")
        txt, _ = planner_ui.lines_text({"id": "p", "version": 1, "plan": plan}, 0)
        self.assertIn("Спрос: рост (x2), на количество не влияет", txt)


if __name__ == "__main__":
    unittest.main()
