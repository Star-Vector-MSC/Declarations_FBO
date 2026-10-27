import json
import unittest
from fractions import Fraction

import planner as P

TH = ["wild", "Артикул", "Название магазина", "Название товара", "Свободный остаток сервис", "Остаток на (дней)",
      "Средние продажи 3 дня", "Средние продажи 7 дней", "Средние продажи 14 дней", "Средние продажи 30 дней",
      "Прогнозные остатки в днях", "Прогнозные остатки в днях", "Остаток на дней по вилду"]
PH = ["wild", "шт в коробе", "Штук на палете", "шт в коробе", "Штук на палете"]
PI, AI = TH.index("Название товара"), TH.index("Артикул")


def T(w, a, cab="C1", stock=1000, cur=5, s=(10, 10, 10, 10), fc=2, wd=40, fc2=99):
    return [w, a, cab, "Item " + a, stock, cur, *s, fc, fc2, wd]


def TP(w, a, product, cab="C1", **kw):
    row = T(w, a, cab, **kw)
    row[PI] = product  # header lookup, not hardcoded offset
    return row


def PK(w, box=10, pal=400):
    return [w, 1, 1, box, pal]  # first box/pallet pair is junk; second (mono I/K) is used


def snap(trows, prows, th=TH):
    return {"test": [th] + trows, "packing": [PH] + prows, "source_hash": "h1", "captured_at": "2026-10-27T08:00"}


def fx(a, q, reason="promo", cab="C1"):
    return {"article": a, "cabinet": cab, "quantity": q, "reason": reason}


INVALID_GROUPS = (None, "", "   ", "\t\n", 0, 5, 5.0, -1.5, True, False, [], ["Item"], {}, {"g": "Item"},
                  float("nan"), float("inf"), float("-inf"))


class PlannerTest(unittest.TestCase):
    def plan(self, trows, prows, snapshot=None, **rules):
        res = P.build_plan(snapshot or snap(trows, prows), rules, "2026-10-28", "WH", "2026-10-30")
        json.dumps(res, allow_nan=False)
        total = Fraction(0)
        for ln in res["lines"]:
            self.assertGreater(ln["quantity"], 0)
            self.assertEqual(ln["quantity"] % ln["box_qty"], 0)
            total += Fraction(ln["quantity"], ln["pallet_qty"])
            if not ln["fixed"]:
                self.assertLessEqual(ln["forecast_after"], res["rules"]["max_days"] + 1e-9)
        self.assertLessEqual(total, res["rules"]["capacity"])
        return res

    @staticmethod
    def q(res):
        return {(ln["article"], ln["cabinet"]): ln["quantity"] for ln in res["lines"]}

    def test_header_reorder_and_duplicate_columns(self):
        base = self.plan([T("W1", "A")], [PK("W1")])
        tp, pp = list(range(5, 13)) + list(range(5)), [1, 2, 3, 4, 0]
        th = [TH[i] for i in tp] + ["Extra"]
        th[0], th[th.index("wild")] = "ОСТАТОК  НА (ДНЕЙ)", "  WILD "
        s = {"test": [th, [T("W1", "A")[i] for i in tp] + ["junk"]],
             "packing": [[PH[i] for i in pp], [PK("W1")[i] for i in pp]]}
        res = self.plan(None, None, snapshot=s)
        self.assertEqual(self.q(res), self.q(base))
        self.assertEqual(self.q(res), {("A", "C1"): 100})
        ln = res["lines"][0]
        self.assertEqual((ln["box_qty"], ln["pallet_qty"], ln["forecast_before"]), (10, 400, 2.0))
        with self.assertRaisesRegex(ValueError, "прогнозные"):
            self.plan(None, None, snapshot=snap([T("W1", "A")], [PK("W1")], th=TH[:11] + TH[12:]))
        with self.assertRaisesRegex(ValueError, "артикул"):
            self.plan(None, None, snapshot=snap([], [], th=[h for h in TH if h != "Артикул"]))
        self.assertEqual(P._norm(" Ёлка  Тест "), "елка тест")

    def test_inclusive_thresholds_and_just_outside(self):
        rows = [T("WA", "A", s=(8, 9, 10, 11), cur=10, wd=30), T("WB", "B", s=(7.99, 10, 10, 10)),
                T("WC", "C", cur=10.01), T("WD", "D", wd=29.99)]
        pk = [PK(w) for w in ("WA", "WB", "WC", "WD")]
        res = self.plan(rows, pk)
        self.assertEqual(set(self.q(res)), {("A", "C1")})
        self.assertEqual(res["eligible_count"], 1)
        self.assertIn(("B", "C1"), self.q(self.plan(rows, pk, min_sales=7)))

    def test_first_forecast_sorting_and_pending_once(self):
        rows = [T("W1", "A", fc=5, fc2=0), T("W2", "B", fc=1, fc2=50), T("W3", "C", fc=10, fc2=0)]
        res = self.plan(rows, [PK("W1"), PK("W2"), PK("W3")])
        self.assertEqual([ln["article"] for ln in res["lines"]], ["B", "A"])
        self.assertEqual(res["eligible_count"], 3)
        b, a = res["lines"]
        self.assertEqual((a["quantity"], a["forecast_before"], a["forecast_after"]), (50, 5.0, 10.0))
        self.assertEqual((b["quantity"], b["forecast_after"]), (100, 11.0))

    def test_cap13_tiny_boxes(self):
        rows = [T("W1", "A", fc=12.9), T("W2", "B", fc=12.9), T("W3", "C")]
        res = self.plan(rows, [PK("W1", 1, 1000), PK("W2", 2, 1000), PK("W3", 1, 1000)], target_days=13)
        self.assertEqual(self.q(res), {("A", "C1"): 1, ("C", "C1"): 110})
        self.assertEqual(res["lines"][1]["forecast_after"], 13.0)
        self.assertTrue(any("no feasible" in w for w in res["warnings"]))

    def test_pallet_preferences_and_below_quarter(self):
        rows = [T("WQ", "Q"), T("WH", "H", s=(20,) * 4), T("WI", "I", stock=5000, s=(100,) * 4, fc=1),
                T("WB", "B", fc=5)]
        res = self.plan(rows, [PK("WQ"), PK("WH"), PK("WI", 20, 400), PK("WB")])
        by = {ln["article"]: ln for ln in res["lines"]}
        self.assertEqual({k: v["quantity"] for k, v in by.items()}, {"Q": 100, "H": 200, "I": 800, "B": 50})
        self.assertIn("1/4 pallet", by["Q"]["reason"])
        self.assertIn("1/2 pallet", by["H"]["reason"])
        self.assertIn("2 pallet", by["I"]["reason"])
        self.assertIn("whole boxes", by["B"]["reason"])
        self.assertEqual(by["B"]["pallets"], 0.125)

    def test_shared_stock_and_conflicting_budgets(self):
        rows = [T("W1", "A", "C1", 100, s=(8,) * 4, fc=0), T("W1", "A", "C2", 100, s=(8,) * 4, fc=0),
                T("W2", "B", "C1", 100), T("W2", "B", "C2", 90)]
        res = self.plan(rows, [PK("W1", 10, 1000), PK("W2")])
        self.assertEqual(self.q(res), {("A", "C1"): 80, ("A", "C2"): 20})
        self.assertTrue(any("W2" in w and "conflicting" in w for w in res["warnings"]))

    def test_exact_fraction_capacity(self):
        rows = [T(f"W{i}", f"A{i}", stock=10 ** 6, fc=0) for i in range(5)]
        pk = [PK(f"W{i}", 1, 7) for i in range(5)]
        for cap, exp in ((20, [98, 42]), (33, [98, 98, 35])):
            res = self.plan(rows, pk, capacity=cap)
            self.assertEqual([ln["quantity"] for ln in res["lines"]], exp)
            self.assertEqual(sum(Fraction(ln["quantity"], 7) for ln in res["lines"]), cap)
            self.assertEqual((res["total_pallets"], res["remaining_pallets"]), (float(cap), 0.0))

    def test_fixed_rows(self):
        rows = [T("WL", "L", s=(1, 1, 1, 1)), T("WM", "M", s=(None, "", "#N/A", None), fc=None)]
        res = self.plan(rows, [PK("WL"), PK("WM")], fixed=[fx("L", 20), fx("M", 10, "launch")])
        a, b = res["lines"]
        self.assertEqual((a["article"], a["quantity"], a["fixed"], a["reason"]), ("L", 20, True, "promo"))
        self.assertEqual(a["forecast_after"], 22.0)
        self.assertEqual((b["sales7"], b["forecast_before"], b["forecast_after"], b["fixed"]),
                         (None, None, None, True))
        small = snap([T("WL", "L", stock=100, s=(1, 1, 1, 1)), T("WB", "B", stock=10 ** 6)], [PK("WL"), PK("WB")])
        cases = [([fx("L", 200)], {}, "stock"), ([fx("Z", 10)], {}, "no exact"), ([fx("L", 15)], {}, "box"),
                 ([fx("L", 20)], {"excluded_cabinets": ["c1"]}, "excluded"),
                 ([fx("L", 20)], {"excluded_wild": ["WL"]}, "excluded"),
                 ([fx("B", 400 * 21)], {"capacity": 20}, "capacity"), ([fx("L", 10, cab="C9")], {}, "no exact")]
        for fixed, extra, msg in cases:
            with self.assertRaisesRegex(ValueError, msg):
                P.build_plan(small, dict(fixed=fixed, **extra), "2026-10-28", "WH")

    def test_packing_conflicts_identical_and_blank(self):
        rows = [T("W1", "A"), T("W2", "B"), T("W3", "C"), T("W4", "D")]
        pk = [PK("W1"), PK("W1"), ["W1", None, None, "", None], PK("W2", 10), PK("W2", 20), PK("W4", 0)]
        res = self.plan(rows, pk)
        self.assertEqual(self.q(res), {("A", "C1"): 100})
        for w in ("W2", "W3", "W4"):
            self.assertTrue(any(f"wild {w}:" in m for m in res["warnings"]), w)
        with self.assertRaisesRegex(ValueError, "blocked"):
            self.plan(rows, pk, fixed=[fx("B", 10)])

    def test_malformed_numbers_and_duplicate_identities(self):
        rows = [T("W1", "A", s=("#N/A", 10, 10, 10)), T("W2", "B", cur=float("nan")), T("W3", "C", wd=True),
                T("W4", "D", stock="1 000", s=("8,5",) * 4), T("W5", "E"), T("W5", "E")]
        pk = [PK(f"W{i}") for i in range(1, 6)]
        res = self.plan(rows, pk)
        self.assertEqual(self.q(res), {("D", "C1"): 70})
        self.assertEqual(sum("invalid numeric" in w for w in res["warnings"]), 3)
        self.assertTrue(any("duplicate" in w for w in res["warnings"]))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.plan(rows, pk, fixed=[fx("E", 10)])
        self.assertIsNone(P._num("error"))
        self.assertEqual(P._num("0"), 0)

    def test_rule_validation(self):
        bad = [{"nope": 1}, {"capacity": True}, {"capacity": 25}, {"capacity": 33.0}, {"min_sales": 0},
               {"min_sales": True}, {"target_days": 14}, {"max_days": float("inf")}, {"min_wild_days": "30"},
               {"max_current_days": float("nan")}, {"excluded_wild": "W1"}, {"excluded_cabinets": [1]},
               {"fixed": [{"article": "A", "cabinet": "C1", "quantity": 5}]}, {"fixed": [fx("A", 1.5)]},
               {"fixed": [fx("A", True)]}, {"fixed": [fx("A", 0)]}, {"fixed": [fx("A", 5, " ")]},
               {"fixed": [fx("A", 5), fx("A", 10)]}, "rules"]
        for b in bad:
            with self.assertRaises(ValueError, msg=repr(b)):
                P.validate_rules(b)
        d = P.default_rules()
        d["excluded_wild"].append("X")
        self.assertEqual(P.default_rules()["excluded_wild"], [])
        src = {"excluded_wild": ["W1"], "capacity": 20}
        out = P.validate_rules(src)
        src["excluded_wild"].append("W2")
        self.assertEqual((out["excluded_wild"], out["capacity"], out["min_sales"]), (["W1"], 20, 8))

    def test_bad_call_arguments(self):
        s = snap([T("W1", "A")], [PK("W1")])
        for args in [("2026-13-01", "WH"), ("28.10.2026", "WH"), ("2026-10-28", ""), ("2026-10-28", None),
                     ("2026-10-28", "WH", "2026-10-01")]:
            with self.assertRaises(ValueError):
                P.build_plan(s, None, *args)
        res = P.build_plan(s, None, "2026-10-28", "WH")
        self.assertEqual((res["source_hash"], res["arrival_date"], res["date"]), ("h1", None, "2026-10-28"))

    # --- regressions: invalid free stock blocks the whole wild ---

    def test_invalid_stock_in_any_row_blocks_whole_wild(self):
        for badv in (None, "", "  ", "#N/A", float("nan"), float("inf"), float("-inf"), True, False, -5, "-10"):
            rows = [T("W1", "A", "C1", 1000), T("W1", "A", "C2", badv), T("W2", "B")]
            pk = [PK("W1"), PK("W2")]
            res = self.plan(rows, pk)
            self.assertEqual(self.q(res), {("B", "C1"): 100}, repr(badv))
            self.assertTrue(any("wild W1:" in w and "free stock" in w for w in res["warnings"]), repr(badv))
            for cab in ("C1", "C2"):
                with self.assertRaisesRegex(ValueError, "free stock"):
                    self.plan(rows, pk, fixed=[fx("A", 10, "override", cab=cab)])

    def test_invalid_stock_in_skipped_row_still_blocks(self):
        bad_row = T("W1", "X", "C2", "#N/A")
        bad_row[AI] = None  # row skipped for identity, but its wild stock is still invalid
        res = self.plan([T("W1", "A"), bad_row], [PK("W1")])
        self.assertEqual(res["lines"], [])
        self.assertTrue(any("wild W1:" in w and "free stock" in w for w in res["warnings"]))

    def test_all_invalid_stock_fails_cleanly(self):
        rows = [T("W1", "A", "C1", None), T("W1", "A", "C2", "#N/A")]
        res = self.plan(rows, [PK("W1")])
        self.assertEqual((res["lines"], res["total_pallets"]), ([], 0.0))
        self.assertTrue(any("wild W1:" in w and "blocked" in w for w in res["warnings"]))
        with self.assertRaisesRegex(ValueError, "free stock"):
            self.plan(rows, [PK("W1")], fixed=[fx("A", 10)])

    def test_zero_and_consistent_localized_stock(self):
        res = self.plan([T("W1", "A", "C1", 0), T("W1", "A", "C2", "0")], [PK("W1")])
        self.assertEqual(res["lines"], [])
        self.assertFalse(any("wild W1:" in w for w in res["warnings"]))
        self.assertTrue(any("no feasible" in w for w in res["warnings"]))
        res = self.plan([T("W1", "A", "C1", "1 000"), T("W1", "A", "C2", 1000.0)], [PK("W1")])
        self.assertEqual(self.q(res), {("A", "C1"): 100, ("A", "C2"): 100})

    # --- regressions: permanent FBO bans ---

    def test_banned_groups_never_planned(self):
        labels = ["Газовые горелки туристические", "Парфюм для дома", "  ГАЗОВЫЕ   горелки  ",
                  "парфюм  ДЛЯ дома ", "Газовые горелки", "газовые горёлки туристические"]
        for lab in labels:
            rows = [TP("W1", "A", lab, fc=0, s=(50,) * 4), T("W2", "B")]
            pk = [PK("W1"), PK("W2")]
            res = self.plan(rows, pk, min_sales=1, min_wild_days=0, max_current_days=100)
            self.assertEqual(self.q(res), {("B", "C1"): 100}, lab)
            self.assertEqual(res["eligible_count"], 1, lab)
            self.assertTrue(any("wild W1:" in w and "banned" in w and "FBO" in w for w in res["warnings"]), lab)
            with self.assertRaisesRegex(ValueError, "banned"):
                self.plan(rows, pk, fixed=[fx("A", 10, "manager override")])

    def test_banned_rejected_before_capacity(self):
        rows = [TP("W1", "A", "Парфюм для дома", stock=10 ** 6, s=(100,) * 4, fc=0), T("W2", "B")]
        res = self.plan(rows, [PK("W1", 1, 7), PK("W2")], capacity=20)
        self.assertEqual(self.q(res), {("B", "C1"): 100})
        self.assertEqual(res["total_pallets"], 0.25)

    def test_banned_any_row_blocks_wild_in_every_cabinet(self):
        skipped = TP("W3", "X", "Газовые горелки туристические", cab="C2")
        skipped[AI] = None
        rows = [T("W1", "A", "C1"), TP("W1", "A", "Газовые горелки туристические", cab="C2"),
                T("W1", "Z", "C3"), T("W2", "B"), T("W3", "C"), skipped]
        pk = [PK("W1"), PK("W2"), PK("W3")]
        res = self.plan(rows, pk)
        self.assertEqual(self.q(res), {("B", "C1"): 100})
        self.assertEqual(res["eligible_count"], 1)
        for a, cab in (("A", "C1"), ("A", "C2"), ("Z", "C3"), ("C", "C1")):
            with self.assertRaisesRegex(ValueError, "banned"):
                self.plan(rows, pk, fixed=[fx(a, 10, "must ship", cab=cab)])

    def test_unrelated_categories_remain_eligible(self):
        for lab in ("Газовые баллоны", "Парфюм для тела", "Горелка для фондю", "Item A"):
            res = self.plan([TP("W1", "A", lab), T("W2", "B")], [PK("W1"), PK("W2")])
            self.assertEqual(self.q(res), {("A", "C1"): 100, ("B", "C1"): 100}, lab)
            self.assertFalse(any("banned" in w for w in res["warnings"]), lab)
        self.assertIsNone(P._banned("Горелки газовые"))
        self.assertIsNone(P._banned("Газовые горелкими"))

    # --- regressions: missing/invalid product group blocks the whole wild ---

    def test_invalid_group_blocks_whole_wild_regular_and_fixed(self):
        for badv in INVALID_GROUPS:
            rows = [T("W1", "A", "C1", s=(50,) * 4, fc=0), TP("W1", "A", badv, cab="C2"),
                    T("W1", "Z", "C3"), T("W2", "B")]
            pk = [PK("W1"), PK("W2")]
            res = self.plan(rows, pk)
            self.assertEqual(self.q(res), {("B", "C1"): 100}, repr(badv))
            self.assertEqual((res["eligible_count"], res["total_pallets"]), (1, 0.25), repr(badv))
            self.assertTrue(any("wild W1:" in w and "missing/invalid product group" in w and "blocked" in w
                                for w in res["warnings"]), repr(badv))
            self.assertFalse(any("wild W2:" in w for w in res["warnings"]), repr(badv))
            for a, cab in (("A", "C1"), ("A", "C2"), ("Z", "C3")):
                with self.assertRaisesRegex(ValueError, "product group"):
                    self.plan(rows, pk, fixed=[fx(a, 10, "manager override", cab=cab)])
            res = self.plan(rows, pk, fixed=[fx("B", 10)])
            self.assertEqual(self.q(res), {("B", "C1"): 10}, repr(badv))

    def test_invalid_group_single_row_blocked(self):
        for badv in INVALID_GROUPS:
            rows = [TP("W1", "A", badv, stock=10 ** 6, s=(100,) * 4, fc=0)]
            res = self.plan(rows, [PK("W1", 1, 7)])
            self.assertEqual((res["lines"], res["eligible_count"], res["total_pallets"]), ([], 0, 0.0), repr(badv))
            with self.assertRaisesRegex(ValueError, "product group"):
                self.plan(rows, [PK("W1", 1, 7)], fixed=[fx("A", 7)])

    def test_invalid_group_in_skipped_or_duplicate_row_blocks(self):
        for badv in (None, "  ", 42, float("nan")):
            skipped = TP("W1", "X", badv, cab="C2")
            skipped[AI] = None  # skipped for identity, still carries wild W1
            rows = [T("W1", "A"), skipped, T("W3", "E"), TP("W3", "E", badv), T("W3", "F", "C2"), T("W2", "B")]
            pk = [PK("W1"), PK("W2"), PK("W3")]
            res = self.plan(rows, pk)
            self.assertEqual(self.q(res), {("B", "C1"): 100}, repr(badv))
            self.assertEqual(res["eligible_count"], 1, repr(badv))
            for w in ("W1", "W3"):
                self.assertTrue(any(f"wild {w}:" in m and "product group" in m for m in res["warnings"]), w)
            for a, cab in (("A", "C1"), ("F", "C2")):
                with self.assertRaisesRegex(ValueError, "product group"):
                    self.plan(rows, pk, fixed=[fx(a, 10, "must ship", cab=cab)])

    def test_banned_label_takes_precedence_over_invalid_group(self):
        for rows in ([TP("W1", "A", None), TP("W1", "A", "Парфюм для дома", cab="C2")],
                     [TP("W1", "A", "Газовые горелки", cab="C2"), TP("W1", "A", "")]):
            res = self.plan(rows, [PK("W1")])
            self.assertEqual(res["lines"], [])
            msgs = [w for w in res["warnings"] if "wild W1:" in w]
            self.assertEqual(len(msgs), 1)
            self.assertIn("banned", msgs[0])
            with self.assertRaisesRegex(ValueError, "banned"):
                self.plan(rows, [PK("W1")], fixed=[fx("A", 10)])

    def test_valid_nonempty_string_groups_unaffected(self):
        for lab in ("12345", " Group X ", "Процент"):
            rows = [TP("W1", "A", lab), TP("W1", "A", "Item Z", cab="C2"), T("W2", "B")]
            res = self.plan(rows, [PK("W1"), PK("W2")])
            self.assertEqual(self.q(res), {("A", "C1"): 100, ("A", "C2"): 100, ("B", "C1"): 100}, lab)
            self.assertFalse(any("product group" in w for w in res["warnings"]), lab)
            res = self.plan(rows, [PK("W1"), PK("W2")], fixed=[fx("A", 10)])
            self.assertEqual(self.q(res)[("A", "C1")], 10, lab)


if __name__ == "__main__":
    unittest.main()
