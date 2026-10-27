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
        self.posts, self.fail, self.grid = [], False, []

    def request(self, method, url, params=None, json=None, timeout=None):
        assert url.startswith(ps._API + "fake-base")
        if method == "POST":
            self.posts.append(copy.deepcopy(json))
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
            "rowCount": 3 if i == 777 else 99}}} for t, i in IDS.items()]})

    def apply(self, reqs):
        rows = self.tabs[ps.T_TARGET]
        for r in reqs:
            u = r.get("updateCells")
            if u:
                g, c = u["range"], u["range"]["startColumnIndex"]
                for k, row in enumerate(u["rows"]):
                    while len(rows) <= g["startRowIndex"] + k:
                        rows.append([])
                    rr = rows[g["startRowIndex"] + k]
                    rr.extend([""] * (c + 1 - len(rr)))
                    v = row["values"][0].get("userEnteredValue", {})
                    rr[c] = v.get("stringValue", v.get("numberValue", ""))


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.old = sys.modules.get("wb_declarations")
        sys.modules["wb_declarations"] = types.SimpleNamespace(KEY="unused", SHIPMENTS=["fake-base", "fake-prod"])
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
        self.assertEqual(reqs[0], {"appendDimension": {"sheetId": 777, "dimension": "ROWS", "length": 1}})
        for r in reqs:
            (_, b), = r.items()
            self.assertEqual((b.get("destination") or b.get("range") or b)["sheetId"], 777)
        src = sorted(r["copyPaste"]["source"]["startColumnIndex"] for r in reqs
                     if "copyPaste" in r and r["copyPaste"]["source"]["sheetId"] == 100)
        self.assertEqual(src, [13, 36])
        self.assertEqual(self.s.grid, ["'Отгрузка FBO'!2:2"])
        t = self.s.tabs[ps.T_TARGET]
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

    def test_production_spreadsheet_refused(self):
        sys.modules["wb_declarations"] = types.SimpleNamespace(KEY="unused", SHIPMENTS=["same", "same"])
        with self.assertRaises(ps.PlannerSheetsError):
            ps.PlannerSheets(session=self.s).snapshot()


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
