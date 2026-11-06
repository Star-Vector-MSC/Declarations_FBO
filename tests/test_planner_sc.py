import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import planner as P
import planner_sc as sc
import test_planner as tp
from test_planner_integration import OWNER, SNAP, UiBase, buttons, cb, said

T0 = datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc)
HIST = [("wild1", "Коледино", "2026-10-20", 3), ("wild1", "Казань", "2026-10-05", 1), ("wild2", "коледино ", "2026-10-25", 2)]


class AllowedWildsTest(unittest.TestCase):
    def test_allowed_wilds_narrow_the_pick_and_report_the_pool(self):
        sn = tp.snap([tp.T("wild1", "A1"), tp.T("wild2", "A2")], [tp.PK("wild1"), tp.PK("wild2")])
        kw = dict(min_line_qty=0, min_cover=0, floor_qty=0)
        free = P.build_plan(sn, {}, "2026-10-28", "WH", None, **kw)
        self.assertEqual({ln["wild"] for ln in free["lines"]}, {"wild1", "wild2"})
        self.assertNotIn("allowed_wilds", free)
        one = P.build_plan(sn, {}, "2026-10-28", "WH", None, allowed_wilds=["WILD1"], **kw)
        self.assertEqual({ln["wild"] for ln in one["lines"]}, {"wild1"})
        self.assertEqual((one["allowed_wilds"], sorted(one["pool_wilds"])), (["wild1"], ["wild1", "wild2"]))
        none = P.build_plan(sn, {}, "2026-10-28", "WH", None, allowed_wilds=[], **kw)
        self.assertEqual(none["lines"], [])


class ScStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.st = sc.ScStore(os.path.join(self.tmp.name, "sc.sqlite3"))

    def test_base_wild(self):
        self.assertEqual([sc.base_wild(x) for x in ("wild797d2", " WILD797 ", "wild5d", "W1", "wild", None)],
                         ["wild797", "wild797", "wild5", None, None, None])

    def test_refresh_every_seven_days_and_empty_fetch_keeps_snapshot(self):
        calls = []

        def fetch(now):
            calls.append(now)
            return HIST
        self.assertTrue(self.st.stale(T0))
        self.assertTrue(self.st.ensure_fresh(fetch, T0))
        self.assertFalse(self.st.ensure_fresh(fetch, T0 + timedelta(days=6)))  # fresh: no database read
        self.assertTrue(self.st.ensure_fresh(fetch, T0 + timedelta(days=8)))
        self.assertTrue(self.st.ensure_fresh(fetch, T0 + timedelta(days=8), force=True))
        self.assertEqual(len(calls), 3)
        with self.assertRaises(sc.ScError):
            self.st.ensure_fresh(lambda now: [], T0 + timedelta(days=20))
        a = self.st.analyze(["wild1"], ["Казань", "Коледино"], T0 + timedelta(days=20))
        self.assertEqual(a["full"], ["Казань", "Коледино"])  # the old snapshot survived

    def test_analyze_full_partial_nodata_and_manual_ttl(self):
        self.st.replace_history(HIST, T0)
        whs = ["Казань", "Коледино", "СЦ Никольское"]
        a = self.st.analyze(["wild1", "wild2d3", "wild3"], whs, T0)
        self.assertEqual((a["nodata"], a["full"], a["full_known"]), (["wild3"], [], ["Коледино"]))
        self.assertEqual(a["rank"][0], ("Коледино", 2, ["wild3"]))  # names match case/space-insensitively
        a = self.st.analyze(["wild1", "wild2"], whs, T0)
        self.assertEqual((a["nodata"], a["full"]), ([], ["Коледино"]))
        self.st.add_manual([("wild3", "Коледино")], T0)
        self.assertEqual(self.st.analyze(["wild1", "wild2", "wild3"], whs, T0 + timedelta(days=9))["full"], ["Коледино"])
        late = self.st.analyze(["wild1", "wild2", "wild3"], whs, T0 + timedelta(days=11))  # the answer expired
        self.assertEqual((late["full"], late["nodata"]), ([], ["wild3"]))

    def test_sheet_and_wb_spellings_of_one_warehouse_are_matched(self):
        pairs = [("Обухово -> Волгоград", "Волгоград"), ("Обухово -> Краснодар (Тихорецкая)", "Краснодар (Тихорецкая)"),
                 ("Рязань", "Рязань (Тюшевское)"), ("СЦ Щеглово(Холмогоры)", "СЦ Щеглово (Холмогоры)"),
                 ("Владимир", "Владимир Воршинское"), ("Чехов 1 -> Новосемейкино", "Новосемейкино")]
        self.assertTrue(all(sc.wkey(a) == sc.wkey(b) for a, b in pairs), [p for p in pairs if sc.wkey(p[0]) != sc.wkey(p[1])])
        self.assertNotEqual(sc.wkey("СЦ Ногинск Парк"), sc.wkey("СЦА Ногинск Парк"))  # not guessed
        self.st.replace_history([("wild1", "Волгоград", "2026-10-01", 1), ("wild1", "Новосемейкино", "2026-10-02", 1)], T0)
        a = self.st.analyze(["wild1"], ["Обухово -> Волгоград", "Новосемейкино", "Чехов 1 -> Новосемейкино", "Тула"], T0)
        self.assertEqual(a["full"], ["Новосемейкино", "Обухово -> Волгоград", "Чехов 1 -> Новосемейкино"])

    def test_shipments_before_october_are_not_evidence(self):
        self.st.replace_history([("wild1", "Казань", "2026-09-30", 4), ("wild1", "Коледино", "2026-10-01", 1)], T0)
        self.assertEqual(self.st.analyze(["wild1"], ["Казань", "Коледино"], T0)["full"], ["Коледино"])

    def test_unknown_history_warehouses_are_reported_not_offered(self):
        self.st.replace_history(HIST + [("wild1", "Склад вне списка", "2026-10-01", 1)], T0)
        a = self.st.analyze(["wild1"], ["Казань"], T0)
        self.assertEqual(a["full"], ["Казань"])
        self.assertIn("Склад вне списка", a["outside"])
        self.assertNotIn("Склад вне списка", a["full"])


class ScUiTest(UiBase):
    def make(self):
        super().make()
        self.fetches = []
        self.fail = False
        self.hist = list(HIST)

        def fetch(now):
            self.fetches.append(now)
            if self.fail:
                raise sc.ScError("База недоступна.")
            return self.hist
        self.ctl._sc = sc.ScStore(os.path.join(self.tmp.name, "sc.sqlite3"))
        self.ctl._sc_fetch = fetch
        self.sheets.snap = json.loads(json.dumps(SNAP).replace('"W1"', '"wild1"').replace('"W2"', '"wild2"'))

    async def open_check(self):
        m, b = await self.plan()
        self.assertIn("Проверить склады", b)
        c, m1 = cb(b["Проверить склады"])
        await self.ctl.handle_callback(c)
        return m1

    async def test_whole_pool_warehouse_from_history(self):
        m = await self.open_check()
        t = said(m)
        self.assertIn("Весь пул можно отправить на: Коледино.", t)
        self.assertIn("не гарантия", t)
        self.assertEqual(len(self.fetches), 1)
        c, m2 = cb(buttons(m)["Обновить историю"])
        await self.ctl.handle_callback(c)
        self.assertEqual(len(self.fetches), 2)
        c, m3 = cb(f"pl:sc:{self.ctl.store.list(OWNER, 1)[0]['id']}:1")
        await self.ctl.handle_callback(c)
        self.assertEqual(len(self.fetches), 2)  # a fresh snapshot is reused

    async def test_wilds_without_data_are_listed_and_owner_answer_closes_the_gap(self):
        self.hist = [r for r in HIST if r[0] == "wild1"]
        m = await self.open_check()
        self.assertIn("данных нет: wild2", said(m))
        self.assertNotIn("Весь пул можно", said(m))
        c, m2 = cb(buttons(m)["Ввести данные из кабинета"])
        await self.ctl.handle_callback(c)
        m2.text = "это не то"
        self.assertFalse(await self.ctl.handle_message(m2))  # not in the agreed form: not consumed
        m2.text = "wild2: Нет такого склада"
        self.assertTrue(await self.ctl.handle_message(m2))
        self.assertIn("Ничего не сохранил", said(m2))
        self.assertNotIn("Весь пул", said(m2))
        c, m3 = cb(buttons(m)["Ввести данные из кабинета"])
        await self.ctl.handle_callback(c)
        m3.text = "wild2d2: коледино"
        self.assertTrue(await self.ctl.handle_message(m3))
        self.assertIn("Сохранил:\nwild2: Коледино", said(m3))
        self.assertIn("Весь пул можно отправить на: Коледино.", said(m3))
        self.assertIsNone(self.ctl._st.get(OWNER))

    async def test_database_down_uses_old_snapshot_or_says_so(self):
        self.fail = True
        m = await self.open_check()
        self.assertIn("База недоступна.", said(m))
        self.assertNotIn("Весь пул", said(m))
        self.fail = False
        m = await self.open_check()
        self.fail = True
        c, m2 = cb(buttons(m)["Обновить историю"])
        await self.ctl.handle_callback(c)
        self.assertIn("Использую прежний снимок", said(m2))
        self.assertIn("Весь пул можно отправить на: Коледино.", said(m2))

    async def partial(self):
        self.hist = [("wild1", "Коледино", "2026-10-20", 3)]  # wild2 was never shipped anywhere
        m, b = await self.plan()
        d0 = self.ctl.store.list(OWNER, 1)[0]
        c, m1 = cb(b["Проверить склады"])
        await self.ctl.handle_callback(c)
        return d0, m1

    async def test_partial_warehouse_offers_replacement_dates_and_data_request(self):
        d0, m1 = await self.partial()
        t, b = said(m1), buttons(m1)
        self.assertIn("Если заменить под «Коледино»: строк 2 → 1", t)
        self.assertIn("Срочные wild без подтверждения", t)
        self.assertIn("wild2", t.split("Срочные wild без подтверждения")[1])
        for name in ("Заменить под Коледино", "Раньше, 20 паллет", "Позже, добить машину", "Ввести данные из кабинета"):
            self.assertIn(name, b)
        self.assertTrue(all(len(v.encode()) <= 64 for v in b.values()))
        self.assertEqual(self.ctl.store.get(OWNER, d0["id"])["version"], 1)  # looking stores nothing

    async def test_replacement_keeps_only_confirmed_wilds_and_survives_recalculation(self):
        d0, m1 = await self.partial()
        c, m2 = cb(buttons(m1)["Заменить под Коледино"])
        await self.ctl.handle_callback(c)
        d1 = self.ctl.store.get(OWNER, d0["id"])
        self.assertEqual((d1["version"], d1["plan"]["warehouse"], d1["plan"]["allowed_wilds"]), (2, "Коледино", ["wild1"]))
        self.assertEqual({ln["wild"] for ln in d1["plan"]["lines"]}, {"wild1"})
        self.assertEqual(len(self.ctl.store.get(OWNER, d0["id"], 1)["plan"]["lines"]), 2)  # history kept
        self.assertIn("Подбор ограничен wild", said(m2))
        c, m3 = cb(buttons(m2)["Пересчитать"])
        await self.ctl.handle_callback(c)
        d2 = self.ctl.store.get(OWNER, d0["id"])
        self.assertEqual((d2["version"], d2["plan"]["allowed_wilds"]), (3, ["wild1"]))
        c, m4 = cb(buttons(m1)["Заменить под Коледино"])  # the old button: its session is gone
        await self.ctl.handle_callback(c)
        self.assertIn("устарела", said(m4))
        self.assertEqual(self.ctl.store.get(OWNER, d0["id"])["version"], 3)

    async def test_earlier_dates_with_20_pallets(self):
        d0, m1 = await self.partial()
        c, m2 = cb(buttons(m1)["Раньше, 20 паллет"])
        await self.ctl.handle_callback(c)
        t, b = said(m2), buttons(m2)
        self.assertIn("машина 20 паллет", t)
        self.assertIn("(текущая дата)", t)
        self.assertEqual(len([k for k in b if k.endswith("/20")]), 2)  # today and the current date (tomorrow)
        today = datetime.now(timezone(timedelta(hours=3))).date()
        pick = [v for v in b.values() if v.split(":")[-1] == today.strftime("%Y%m%d")][0]
        c, m3 = cb(pick)
        await self.ctl.handle_callback(c)
        d1 = self.ctl.store.get(OWNER, d0["id"])
        self.assertEqual((d1["version"], d1["plan"]["date"], d1["plan"]["rules"]["capacity"], d1["plan"]["warehouse"]),
                         (2, today.isoformat(), 20, "Коледино"))
        self.assertEqual(d1["plan"]["allowed_wilds"], ["wild1"])

    async def test_later_dates_keep_current_lines(self):
        d0, m1 = await self.partial()
        c, m2 = cb(buttons(m1)["Позже, добить машину"])
        await self.ctl.handle_callback(c)
        t, b = said(m2), buttons(m2)
        self.assertIn("текущие строки остаются", t)
        picks = [v for k, v in b.items() if "/33" in k]
        self.assertEqual(len(picks), 7)
        c, m3 = cb(picks[0])
        await self.ctl.handle_callback(c)
        d1 = self.ctl.store.get(OWNER, d0["id"])
        cur = d0["plan"]["date"]
        self.assertEqual(d1["version"], 2)
        self.assertGreater(d1["plan"]["date"], cur)
        kept = [ln for ln in d0["plan"]["lines"] if ln["wild"] == "wild1"]
        self.assertTrue(all(any(x["article"] == k["article"] and x["quantity"] == k["quantity"] for x in d1["plan"]["lines"])
                            for k in kept))
        self.assertEqual({ln["wild"] for ln in d1["plan"]["lines"]}, {"wild1"})

    async def exported_check(self):
        self.hist = [("wild1", "Коледино", "2026-10-20", 3)]
        m, b = await self.plan()
        d0 = self.ctl.store.list(OWNER, 1)[0]
        c, m1 = cb(b["Записать в тестовый лист"])
        await self.ctl.handle_callback(c)
        c, m2 = cb(buttons(m1)["Подтвердить запись"])
        await self.ctl.handle_callback(c)
        c, m3 = cb(f"pl:sc:{d0['id']}:1")
        await self.ctl.handle_callback(c)
        return d0, m3

    async def test_exported_plan_limit_then_rows_to_review(self):
        d0, m3 = await self.exported_check()
        t, b = said(m3), buttons(m3)
        self.assertIn("План уже в листе", t)
        self.assertNotIn("Раньше, 20 паллет", b)  # rows already in the sheet cannot be shrunk to a smaller machine
        self.assertNotIn("Заменить под Коледино", b)
        c, m4 = cb(b["Ограничить подбор: Коледино"])
        await self.ctl.handle_callback(c)
        d1 = self.ctl.store.get(OWNER, d0["id"])
        self.assertEqual((d1["version"], d1["plan"]["allowed_wilds"], d1["plan"]["allowed_for"]), (1, ["wild1"], "Коледино"))
        self.assertEqual(d1["exported_version"], 1)
        self.assertIn("Пересмотреть", said(m4))
        self.assertIn("wild2", said(m4))  # the row without confirmation is named
        self.assertEqual(self.ctl.store.get(OWNER, d0["id"], 1)["plan"]["lines"], d0["plan"]["lines"])
        c, m5 = cb(b["Ограничить подбор: Коледино"])  # a second press of the same button: session is gone
        await self.ctl.handle_callback(c)
        self.assertIn("устарела", said(m5))

    async def test_exported_plan_later_date_goes_through_the_sheet_confirmation(self):
        self.sheets.date_calls = []
        d0, m3 = await self.exported_check()
        c, m4 = cb(buttons(m3)["Позже, добить машину"])
        await self.ctl.handle_callback(c)
        picks = [v for k, v in buttons(m4).items() if "/33" in k]
        self.assertEqual(len(picks), 7)
        c, m5 = cb(picks[0])
        await self.ctl.handle_callback(c)
        self.assertIn("Сменить дату отгрузки плана", said(m5))
        self.assertEqual(self.sheets.date_calls, [])  # nothing is written before the confirmation
        c, m6 = cb(buttons(m5)["Подтвердить"])
        await self.ctl.handle_callback(c)
        self.assertEqual(len(self.sheets.date_calls), 1)
        d1 = self.ctl.store.get(OWNER, d0["id"])
        self.assertGreater(d1["plan"]["date"], d0["plan"]["date"])
        self.assertIn("Обновить по листу", said(m6))

    async def test_store_limit_overlay_is_append_only_and_exported_only(self):
        d0, m3 = await self.exported_check()
        st = self.ctl.store
        n = st.set_limit(OWNER, d0["id"], 1, "Коледино", ["wild1"])
        self.assertEqual(st.set_limit(OWNER, d0["id"], 1, "Коледино", ["wild1"])["plan"]["allowed_wilds"], n["plan"]["allowed_wilds"])
        st.set_limit(OWNER, d0["id"], 1, "Казань", ["wild1", "wild2"])
        self.assertEqual(st.get(OWNER, d0["id"])["plan"]["allowed_for"], "Казань")
        m, b = await self.plan()
        d_draft = [x for x in st.list(OWNER, 10) if x["exported_version"] is None][0]
        from planner_storage import DraftConflict
        with self.assertRaises(DraftConflict):
            st.set_limit(OWNER, d_draft["id"], 1, "Коледино", ["wild1"])

    async def test_non_owner_gets_nothing(self):
        m, b = await self.plan()
        c, m1 = cb(b["Проверить склады"], uid=99)
        await self.ctl.handle_callback(c)
        self.assertEqual(m1.answer.await_count, 0)
        self.assertEqual(self.fetches, [])


if __name__ == "__main__":
    unittest.main()
