import copy
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing

import planner
import planner_sheets as ps
from planner_storage import DraftConflict, DraftNotFound, DraftStore

IDS = {ps.T_TEST: 1, ps.T_PACK: 2, ps.T_ORIG: 100, ps.T_TARGET: 777}
TH = (["wild", "Артикул", "Название магазина", "Название товара", "Свободный остаток сервис", "Остаток на (дней)"]
      + ["средние продажи " + x for x in ("3 дня", "7 дней", "14 дней", "30 дней")]
      + ["Прогнозные остатки в днях"] * 2 + ["Остаток на дней по вилду"])
COL = list(ps.ORIGINAL).index


def orow(wh, actual=""):
    r = [""] * 38
    r[0], r[8] = actual, wh
    return r


class Resp:
    def __init__(self, data):
        self.status_code, self.data = 200, data

    def json(self):
        return self.data


class FakeSession:
    def __init__(self):
        self.tabs = {
            ps.T_TEST: [TH, ["W1", "A1", "Каб", "Товар", 1000, 5, 10, 10, 10, 10, 4, 4, 60], [],
                        ["W2", 12345, "Каб", "Товар2", 500, 5, 10, 10, 10, 10, 6, 6, 60]],
            ps.T_PACK: [["wild", "Шт в коробе", "Штук на палете", "Шт в коробе", "Штук на палете"],
                        ["W1", 1, 1, 6, 48], ["W2", 1, 1, 10, 40]],
            ps.T_ORIG: [list(ps.ORIGINAL), orow("Коледино", 46000), orow("Казань")],
            ps.T_TARGET: [list(ps.ORIGINAL + ps.META), ["ручная", "", "=1+1"]]}

        def f(s):
            return {"userEnteredValue": {"formulaValue": s}}
        self.tpl = [{}] * 38
        self.tpl[13], self.tpl[19], self.tpl[25], self.tpl[36] = f("=M2"), f("=S2/X2"), f("=ARRAYFORMULA(A2:A)"), f("=T2")
        self.posts, self.fail, self.grid, self.hook = [], False, [], None
        self.rc, self.ids = 3, dict(IDS)

    def request(self, method, url, params=None, json=None, timeout=None):
        assert url.startswith(ps._API + "fake-base")
        if method == "POST":
            self.posts.append(copy.deepcopy(json))
            if self.hook:  # a human types a row at the bottom between our GET and POST
                self.hook()
            self.apply(json["requests"])
            if self.fail:
                raise ConnectionError("response lost")
            return Resp({})
        if url.endswith("/values:batchGet"):
            return Resp({"valueRanges": [{"values": copy.deepcopy(self.tabs[r[1:-1]])} for r in params["ranges"]]})
        if "rowData" in params["fields"]:
            self.grid.append(params["ranges"])
            return Resp({"sheets": [{"data": [{"rowData": [{"values": self.tpl}]}]}]})
        return Resp({"sheets": [{"properties": {"sheetId": i, "title": t, "gridProperties": {
            "rowCount": self.rc if t == ps.T_TARGET else 99}}} for t, i in self.ids.items()]})

    def apply(self, reqs):
        rows = self.tabs[ps.T_TARGET]
        for r in reqs:
            if "appendDimension" in r:
                self.rc += r["appendDimension"]["length"]
            d = r.get("insertDimension")
            if d:
                a, b = d["range"]["startIndex"], d["range"]["endIndex"]
                assert a < self.rc, "insert beyond grid"
                rows.extend([] for _ in range(a - len(rows)))
                rows[a:a] = [[] for _ in range(b - a)]
                self.rc += b - a
            u = r.get("updateCells")
            if u:
                g, c = u["range"], u["range"]["startColumnIndex"]
                for k, row in enumerate(u["rows"]):
                    while len(rows) <= g["startRowIndex"] + k:
                        rows.append([])
                    rr = rows[g["startRowIndex"] + k]
                    rr.extend([""] * (c + 1 - len(rr)))
                    v = row["values"][0].get("userEnteredValue", {})
                    rr[c] = v.get("stringValue", v.get("numberValue", v.get("formulaValue", "")))


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.old = sys.modules.get("wb_declarations")
        sys.modules["wb_declarations"] = types.SimpleNamespace(KEY="unused", SHIPMENTS=("fake-base", 100))
        self.s = FakeSession()
        self.gw = ps.PlannerSheets(session=self.s)
        plan = planner.build_plan(self.gw.snapshot(), {}, "2026-11-02", "Коледино")
        self.assertEqual([ln["quantity"] for ln in plan["lines"]], [48, 40])
        self.draft = {"id": "abcdef012345", "version": 1, "plan": json.loads(json.dumps(plan))}

    def tearDown(self):
        if self.old is None:
            sys.modules.pop("wb_declarations", None)
        else:
            sys.modules["wb_declarations"] = self.old

    def test_snapshot_hash_and_arrayformula_guard(self):
        a, b = self.gw.snapshot(), self.gw.snapshot()
        self.assertEqual(a["warehouses"], ["Казань", "Коледино"])
        self.assertEqual(len(a["test"]), 3)
        self.assertEqual(a["source_hash"], b["source_hash"])
        self.s.tabs[ps.T_ORIG][2][5] = "x"
        self.assertEqual(self.gw.snapshot()["source_hash"], a["source_hash"])
        self.s.tabs[ps.T_TARGET][1].append("=ARRAYFORMULA(B2:B)")
        with self.assertRaises(ps.PlannerSheetsError):
            self.gw.publish(self.draft)
        self.assertEqual(self.s.posts, [])

    def test_publish_writes_only_test_sheet(self):
        url = self.gw.publish(self.draft)
        self.assertEqual(len(self.s.posts), 1)
        self.assertIn("gid=777", url)
        self.assertTrue(url.endswith("range=A3"))
        reqs = self.s.posts[0]["requests"]
        self.assertNotIn("USER_ENTERED", json.dumps(reqs))
        self.assertEqual(reqs[0], {"insertDimension": {"range": {"sheetId": 777, "dimension": "ROWS", "startIndex": 2,
                                                                 "endIndex": 4}, "inheritFromBefore": True}})
        for r in reqs:
            (_, b), = r.items()
            self.assertEqual((b.get("destination") or b.get("range") or b)["sheetId"], 777)
        self.assertEqual([r["copyPaste"]["source"]["sheetId"] for r in reqs if "copyPaste" in r], [777, 777])
        self.assertEqual(self.s.grid, ["'Отгрузка FBO'!2:2"])
        t = self.s.tabs[ps.T_TARGET]
        self.assertEqual([t[2][13], t[3][13], t[2][36], t[3][36]], ["=M3", "=M4", "=T3", "=T4"])  # verified text, shifted
        self.assertEqual(t[1][:3], ["ручная", "", "=1+1"])
        self.assertEqual([t[2][COL("Артикул")], t[3][COL("Артикул")]], ["A1", "12345"])
        self.assertEqual(t[2][COL("Тип поставки")], "Монопаллета")
        self.assertEqual(t[2][COL("Статус отгрузки")], "")

    def test_lost_response_replay_after_source_moved(self):
        self.s.fail = True
        with self.assertRaises(ps.PublishUncertainError):
            self.gw.publish(self.draft)
        self.s.fail = False
        self.s.tabs[ps.T_TEST][1][4] = 999
        url = self.gw.publish(self.draft)
        self.assertEqual(self.gw.publish(self.draft), url)
        self.assertEqual(len(self.s.posts), 1)

    def test_edited_markers_and_other_version_refused(self):
        self.gw.publish(self.draft)
        q = COL("Количество")
        self.s.tabs[ps.T_TARGET][2][q] = 60
        with self.assertRaises(ps.PublishConflictError):
            self.gw.publish(self.draft)
        self.s.tabs[ps.T_TARGET][2][q] = 48
        self.assertTrue(self.gw.publish(self.draft))
        with self.assertRaises(ps.PublishConflictError):
            self.gw.publish(dict(self.draft, version=2))
        self.assertEqual(len(self.s.posts), 1)

    def test_source_mismatch_forged_and_capacity(self):
        self.s.tabs[ps.T_TEST][1][4] = 999
        with self.assertRaises(ps.SourceChangedError):
            self.gw.publish(self.draft)
        self.s.tabs[ps.T_TEST][1][4] = 1000
        forged = copy.deepcopy(self.draft)
        forged["plan"]["lines"][0]["quantity"] = 96
        with self.assertRaises(ps.SourceChangedError):
            self.gw.publish(forged)
        for k, v in (("quantity", 47), ("pallet_qty", 1)):
            bad = copy.deepcopy(self.draft)
            bad["plan"]["lines"][0][k] = v
            with self.assertRaises(ps.PlannerSheetsError):
                self.gw.publish(bad)
        forged["plan"]["lines"] = []
        with self.assertRaises(ps.PlannerSheetsError):
            self.gw.publish(forged)
        self.assertEqual(self.s.posts, [])

    def test_config_gid_and_swapped_titles_refused(self):
        for bad in (["same", "same"], ["fake-base", "100"], ["fake-base", True], ["fake-base"]):
            sys.modules["wb_declarations"] = types.SimpleNamespace(KEY="unused", SHIPMENTS=bad)
            with self.assertRaises(ps.PlannerSheetsError):
                ps.PlannerSheets(session=self.s).snapshot()
        sys.modules["wb_declarations"] = types.SimpleNamespace(KEY="unused", SHIPMENTS=("fake-base", 100))
        for swap in ({ps.T_ORIG: 777, ps.T_TARGET: 100}, {ps.T_ORIG: 100, ps.T_TARGET: 100}):
            self.s.ids.update(swap)
            with self.assertRaises(ps.PlannerSheetsError):
                ps.PlannerSheets(session=self.s).snapshot()
            with self.assertRaises(ps.PlannerSheetsError):
                ps.PlannerSheets(session=self.s).publish(self.draft)
        self.assertEqual(self.s.posts, [])

    def test_insert_rows_keep_concurrent_human_row(self):
        t = self.s.tabs[ps.T_TARGET]
        self.s.hook = lambda: t.append(["человек", "x"])
        self.assertTrue(self.gw.publish(self.draft).endswith("range=A3"))
        self.assertEqual((t[1][:3], t[4], len(t)), (["ручная", "", "=1+1"], ["человек", "x"], 5))
        self.assertEqual([t[2][COL("Артикул")], t[3][COL("Артикул")]], ["A1", "12345"])
        kinds = {next(iter(r)) for r in self.s.posts[0]["requests"]}
        self.assertEqual(kinds, {"insertDimension", "copyPaste", "updateCells"})
        out = [{"updateCells": {"range": {"sheetId": 777, "startRowIndex": 1, "endRowIndex": 2}}}]
        with self.assertRaises(ps.PlannerSheetsError):
            ps._guard(out, 777, 2, 4)

    def test_grid_extended_before_insert(self):
        self.s.rc = 2
        self.gw.publish(self.draft)
        self.assertEqual(self.s.posts[0]["requests"][:2], [
            {"appendDimension": {"sheetId": 777, "dimension": "ROWS", "length": 1}},
            {"insertDimension": {"range": {"sheetId": 777, "dimension": "ROWS", "startIndex": 2, "endIndex": 4},
                                 "inheritFromBefore": True}}])
        self.assertEqual(self.s.tabs[ps.T_TARGET][3][COL("Артикул")], "12345")

    def test_wiped_ids_after_lost_response_blocked(self):
        self.s.fail = True
        with self.assertRaises(ps.PublishUncertainError):
            self.gw.publish(self.draft)
        self.s.fail = False
        t = self.s.tabs[ps.T_TARGET]
        for r in t[2:4]:
            r[COL("Комментарии")] = r[len(ps.ORIGINAL)] = ""
        n = len(t)
        with self.assertRaises(ps.PublishConflictError):
            self.gw.publish(self.draft)
        self.assertEqual((len(self.s.posts), len(t)), (1, n))

    def test_replay_compares_all_owned_cells(self):
        self.gw.publish(self.draft)
        t, hdr = self.s.tabs[ps.T_TARGET], list(ps.ORIGINAL + ps.META)
        for name, v in (("Артикул", "012345"), ("Название магазина", "Каб2"), ("wild", "W9"), ("Плановая дата прибытия авто на вб", 46001), ("Правила плана", "{}"),
                        ("Шт в коробе", ""), ("Прогноз после", 12345.5), ("Склад Wildberries", "Казань")):
            c = hdr.index(name)
            old, t[3][c] = t[3][c], v
            with self.assertRaises(ps.PublishConflictError, msg=name):
                self.gw.publish(self.draft)
            t[3][c] = old
        t[2][COL("Количество")] = 48.0
        t[2], t[3] = t[3], t[2]
        self.assertTrue(self.gw.publish(self.draft).endswith("range=A3"))
        del t[3]
        with self.assertRaises(ps.PublishConflictError):
            self.gw.publish(self.draft)
        self.assertEqual(len(self.s.posts), 1)

    def test_scalar_formula_predicate_and_refusals(self):
        ok = ("=1+1", "=M2", "=S2/X2", "=T2", "=$A$1*(2+B$3)^2%", "= -A1 - 3.5")
        bad = ("=SEQUENCE(9)", "=FILTER(A2:A,B2:B)", "=SORT(A2:A9)", "=UNIQUE(A2:A)", "=TRANSPOSE(A1:C1)",
               '=SPLIT(A1,",")', "=SUM(A1)", "=LOG10(5)", "=A1:A3", "={1,2}", "=Sheet1!A1", "=", "=(1", "=1+", "=A1 B1")
        self.assertTrue(all(map(ps._scalar, ok)))
        self.assertFalse(any(map(ps._scalar, bad)))
        row = self.s.tabs[ps.T_TARGET][1]
        for f in bad[:6]:
            row.append(f)
            with self.assertRaises(ps.PlannerSheetsError, msg=f):
                self.gw.publish(self.draft)
            row.pop()
        self.assertEqual(self.s.posts, [])
        self.s.tpl[13] = {"userEnteredValue": {"formulaValue": "=SEQUENCE(9)"}}
        self.gw.publish(self.draft)
        t = self.s.tabs[ps.T_TARGET]
        self.assertEqual((t[2][13], t[2][36]), ("", "=T3"))  # unverifiable formula skipped, not copied
        self.s.tabs[ps.T_TARGET].clear()
        for bad_src in ({"sheetId": 100}, {"sheetId": 100, "startColumnIndex": 3}):
            req = [{"copyPaste": {"source": bad_src, "destination": {"sheetId": 777, "startRowIndex": 2, "endRowIndex": 3},
                                  "pasteType": "PASTE_FORMULA"}}]
            with self.assertRaises(ps.PlannerSheetsError):
                ps._guard(req, 777, 2, 4)

    def test_shift_relative_refs_only(self):
        self.assertEqual(ps._shift("=S2/X2", 3, 1), "=T5/Y5")
        self.assertEqual(ps._shift("=$A$1*B$2+$C3", 2, 1), "=$A$1*C$2+$C5")
        self.assertEqual(ps._shift("=1E5+A2", 1, 0), "=1E5+A3")
        self.assertIsNone(ps._shift("=A1", -1, 0))
        self.assertIsNone(ps._shift("=B2", 0, -2))

    def test_formula_changed_after_check_cannot_be_copied(self):
        """Source formula is read once; only that verified text is written, never a server-side copy of the source."""
        self.gw.publish(self.draft)
        dump = json.dumps(self.s.posts[0]["requests"])
        self.assertNotIn('"sheetId": 100', dump)
        self.assertNotIn("PASTE_FORMULA", dump)


class StoreTest(unittest.TestCase):
    def test_versions_owner_cas_export_reopen(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "data", "planner_drafts.sqlite3")
            s = DraftStore(p)
            a = s.create("u1", {"lines": [], "n": 1})
            self.assertRegex(a["id"], r"^[0-9a-f]{12}$")
            self.assertEqual(s.revise("u1", a["id"], 1, {"lines": [], "n": 2})["version"], 2)
            with self.assertRaises(DraftConflict):
                s.revise("u1", a["id"], 1, {"lines": [], "n": 3})
            for f in (lambda: s.get("u2", a["id"]), lambda: s.revise("u2", a["id"], 2, {"lines": []}),
                      lambda: s.mark_exported("u2", a["id"], 2, "https://example.invalid/x")):
                self.assertRaises(DraftNotFound, f)
            s2 = DraftStore(p)
            self.assertEqual(s2.get("u1", a["id"], 1)["plan"]["n"], 1)
            self.assertEqual(s2.get("u1", a["id"])["plan"]["n"], 2)
            self.assertEqual([x["id"] for x in s2.list("u1")], [a["id"]])
            self.assertEqual(s2.list("u2"), [])
            url = "https://example.invalid/x"
            self.assertEqual(s2.mark_exported("u1", a["id"], 2, url)["export_url"], url)
            self.assertEqual(s2.mark_exported("u1", a["id"], 2, url)["exported_version"], 2)
            with self.assertRaises(DraftConflict):
                s2.mark_exported("u1", a["id"], 1, "https://example.invalid/y")
            with self.assertRaises(DraftConflict):
                s2.revise("u1", a["id"], 2, {"lines": []})
            with closing(sqlite3.connect(p)) as con, self.assertRaises(sqlite3.DatabaseError):
                con.execute("DELETE FROM versions")


if __name__ == "__main__":
    unittest.main()
