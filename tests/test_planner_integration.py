import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatType

import planner_ai
import planner_sheets as ps
import planner_ui as ui
import telegram_bot as app
from planner_memory import MemoryStore
from planner_storage import DraftStore

OWNER = 7
TH = (["wild", "Артикул", "Название магазина", "Название товара", "Свободный остаток сервис", "Остаток на (дней)"]
      + ["средние продажи " + x for x in ("3 дня", "7 дней", "14 дней", "30 дней")]
      + ["Прогнозные остатки в днях"] * 2 + ["Остаток на дней по вилду"])
SNAP = {"test": [TH, ["W1", "A1", "Каб", "Товар", 1000, 5, 10, 10, 10, 10, 4, 4, 60],
                 ["W2", "A2", "Каб", "Товар2", 500, 5, 10, 10, 10, 10, 6, 6, 60]],
        "packing": [["wild", "Шт в коробе", "Штук на палете", "Шт в коробе", "Штук на палете"],
                    ["W1", 1, 1, 6, 48], ["W2", 1, 1, 10, 40]],
        "warehouses": ["Казань", "Коледино"], "source_hash": "h1", "captured_at": "2026-01-01T00:00:00+03:00"}


class FakeSheets:
    def __init__(self):
        self.published, self.snap = [], dict(SNAP)
        self.hook = None

    def snapshot(self):
        if self.hook:
            self.hook()
        return dict(self.snap)

    def publish(self, draft):
        self.published.append((draft["id"], draft["version"]))
        return "https://docs.google.com/spreadsheets/d/x/edit#gid=1&range=A3"


def msg(text, uid=OWNER):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), chat=SimpleNamespace(type=ChatType.PRIVATE), text=text,
                           answer=AsyncMock())


def cb(data, m=None, uid=OWNER):
    m = m or msg("")
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), message=m, data=data, answer=AsyncMock()), m


def buttons(m):
    out = {}
    for c in m.answer.call_args_list:
        mk = c.kwargs.get("reply_markup")
        for row in getattr(mk, "inline_keyboard", []) or []:
            out.update({b.text: b.callback_data for b in row})
    return out


def said(m):
    return "\n".join(str(c.args[0]) for c in m.answer.call_args_list if c.args)


class UiBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.owner_ok = True
        self.sheets = FakeSheets()
        self.make()

    def ai(self, text, rules, snapshot, lines=()):  # never the real Codex in tests
        raise planner_ai.AIUnavailable("Codex недоступен (тест)")

    def make(self):
        d = self.tmp.name
        self.ctl = ui.PlannerController(store=DraftStore(os.path.join(d, "drafts.sqlite3")), sheets=self.sheets,
                                        memory=MemoryStore(os.path.join(d, "mem.sqlite3")),
                                        is_owner=lambda uid, ct: self.owner_ok and uid == OWNER,
                                        ai=lambda *a, **k: self.ai(*a, **k))

    async def plan(self, cap="33"):
        m = msg("Новый план")
        self.assertTrue(await self.ctl.handle_message(m))
        m.text = "Завтра"
        self.assertTrue(await self.ctl.handle_message(m))
        c, _ = cb(buttons(m)["Коледино"], m)
        await self.ctl.handle_callback(c)
        c, _ = cb(buttons(m)[f"{cap} паллет"], m)
        await self.ctl.handle_callback(c)
        return m, buttons(m)


class WizardTests(UiBase):
    async def test_wizard_persists_immutable_draft_without_writing(self):
        m, b = await self.plan()
        self.assertEqual(self.sheets.published, [])
        self.assertIn("ТЕСТОВЫЙ план", said(m))
        self.assertIn("Записать в тестовый лист", b)
        self.assertTrue(all(len(v.encode()) <= 64 for v in b.values()))
        self.make()  # "restart": a new controller reopens the stored draft
        m2 = msg("Мои планы")
        await self.ctl.handle_message(m2)
        c, m3 = cb(next(iter(buttons(m2).values())))
        await self.ctl.handle_callback(c)
        self.assertIn("ТЕСТОВЫЙ план", said(m3))

    async def test_past_and_bad_dates_and_numeric_supply_not_intercepted(self):
        m = msg("Новый план")
        await self.ctl.handle_message(m)
        for text, consumed in (("01.01.2020", True), ("31.02.2030", True), ("12345678", False), ("привет", False)):
            m.text = text
            self.assertEqual(await self.ctl.handle_message(m), consumed, text)
        self.assertEqual(self.sheets.published, [])
        m2 = msg("12345678")
        self.assertFalse(await ui.PlannerController(is_owner=lambda *_: True).handle_message(m2))

    async def test_capacity_step_accepts_labelled_text_only(self):
        m = msg("Новый план")
        await self.ctl.handle_message(m)
        m.text = "Завтра"
        await self.ctl.handle_message(m)
        c, _ = cb(buttons(m)["Казань"], m)
        await self.ctl.handle_callback(c)
        for bare in ("12345678", "20", "33"):  # bare numbers stay with the supply-number handler
            m.text = bare
            self.assertFalse(await self.ctl.handle_message(m), bare)
        m.text = "20 паллет"
        self.assertTrue(await self.ctl.handle_message(m))
        self.assertIn("20 паллет", said(m))

    async def test_menu_clears_wizard(self):
        m = msg("Новый план")
        await self.ctl.handle_message(m)
        for leave in ("Главное меню", "Декларации"):
            self.assertFalse(await self.ctl.handle_message(msg(leave)))
        m.text = "Завтра"
        self.assertFalse(await self.ctl.handle_message(m))

    async def test_unauthorized_and_revoked_owner(self):
        c, m = cb("pl:list", uid=99)
        await self.ctl.handle_callback(c)
        c.answer.assert_awaited_with("Доступ закрыт.", show_alert=True)
        self.assertEqual(m.answer.await_count, 0)
        self.assertFalse(await self.ctl.handle_message(msg("Новый план", uid=99)))
        m = msg("Новый план")
        await self.ctl.handle_message(m)
        m.text = "Завтра"
        self.sheets.hook = lambda: setattr(self, "owner_ok", False)  # revoked while the sheet is being read
        n = m.answer.await_count
        await self.ctl.handle_message(m)
        self.assertEqual(m.answer.await_count, n)  # nothing sent to a revoked owner

    async def test_memory_unavailable_blocks_creation(self):
        class Broken:
            def applicable(self, **_):
                raise RuntimeError("corrupt")
        self.ctl._memory = Broken()
        m = msg("Новый план")
        await self.ctl.handle_message(m)
        m.text = "Завтра"
        await self.ctl.handle_message(m)
        self.assertIn("Память помощника недоступна", said(m))
        self.assertNotIn("Коледино", buttons(m))


class PreliminaryPlanTests(UiBase):
    async def start(self):
        m = msg("Новый план")
        await self.ctl.handle_message(m)
        m.text = "Завтра"
        await self.ctl.handle_message(m)
        return m

    async def prelim(self, via_text=False):
        m = await self.start()
        if via_text:
            m.text = "Пока без склада, надо проверять слоты по товарам"
            self.assertTrue(await self.ctl.handle_message(m))
        else:
            c, _ = cb(buttons(m)["Пока без склада"], m)
            await self.ctl.handle_callback(c)
        c, _ = cb(buttons(m)["33 паллет"], m)
        await self.ctl.handle_callback(c)
        return m, buttons(m)

    async def test_preliminary_plan_saved_without_warehouse_and_not_publishable(self):
        for via_text in (False, True):
            m, b = await self.prelim(via_text)
            self.assertIn("Склад не выбран", said(m))
            self.assertIn("слотов", said(m))  # slots were NOT checked automatically; the note says so
            self.assertIn("Выбрать склад", b)
            self.assertNotIn("Записать в тестовый лист", b)
            d = self.ctl.store.list(OWNER, 1)[0]
            self.assertIsNone(d["plan"]["warehouse"])
            self.assertTrue(d["plan"]["lines"])
            did, ver = d["id"], d["version"]
            for act in ("pub",):  # forged callbacks cannot publish or confirm a plan without a warehouse
                c, m2 = cb(f"pl:{act}:{did}:{ver}", m)
                await self.ctl.handle_callback(c)
                self.assertIn("Склад не выбран", said(m2))
            c, m2 = cb(f"pl:ok:{did}:{ver}", m)
            await self.ctl.handle_callback(c)
            self.assertEqual(self.sheets.published, [])
            self.assertIsNone(self.ctl.store.get(OWNER, did)["exported_version"])

    async def test_warehouse_chosen_later_creates_new_version_then_publishes(self):
        m, b = await self.prelim()
        d0 = self.ctl.store.list(OWNER, 1)[0]
        c, m1 = cb(b["Выбрать склад"])
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.store.get(OWNER, d0["id"])["version"], 1)  # choosing alone stores nothing
        c, m2 = cb(buttons(m1)["Коледино"])
        await self.ctl.handle_callback(c)
        d1 = self.ctl.store.get(OWNER, d0["id"])
        self.assertEqual((d1["version"], d1["plan"]["warehouse"]), (2, "Коледино"))
        self.assertEqual(self.ctl.store.get(OWNER, d0["id"], 1)["plan"]["warehouse"], None)  # history kept
        self.assertEqual(d1["plan"]["lines"], d0["plan"]["lines"])
        b2 = buttons(m2)
        self.assertNotIn("Выбрать склад", b2)
        c, m3 = cb(b2["Записать в тестовый лист"])
        await self.ctl.handle_callback(c)
        c, _ = cb(buttons(m3)["Подтвердить запись"])
        await self.ctl.handle_callback(c)
        self.assertEqual(self.sheets.published, [(d0["id"], 2)])

    async def test_stale_or_foreign_warehouse_buttons_change_nothing(self):
        m, b = await self.prelim()
        did = self.ctl.store.list(OWNER, 1)[0]["id"]
        for data in ("pl:sp:0", "pl:sp:abc123:0", "pl:wn"):
            c, m2 = cb(data, m)
            await self.ctl.handle_callback(c)
            self.assertIn("устарела", said(m2))
        self.assertEqual(self.ctl.store.get(OWNER, did)["version"], 1)
        c, m3 = cb(b["Выбрать склад"], m)
        await self.ctl.handle_callback(c)
        c, _ = cb("pl:cn", m3)
        await self.ctl.handle_callback(c)
        c, m4 = cb(next(v for v in buttons(m3).values() if v.startswith("pl:sp:")))
        await self.ctl.handle_callback(c)
        self.assertIn("устарела", said(m4))
        self.assertEqual(self.ctl.store.get(OWNER, did)["version"], 1)

    async def test_buttons_of_an_earlier_selection_cannot_change_another_draft(self):
        m, b = await self.prelim()
        ma, _ = await self.prelim()
        da, db = self.ctl.store.list(OWNER, 2)[1], self.ctl.store.list(OWNER, 2)[0]
        self.assertNotEqual(da["id"], db["id"])
        c, m_a = cb(f"pl:sw:{da['id']}:{da['version']}")  # selection session for A ...
        await self.ctl.handle_callback(c)
        old = next(v for v in buttons(m_a).values() if v.startswith("pl:sp:"))
        c, m_b = cb(f"pl:sw:{db['id']}:{db['version']}")  # ... then for B
        await self.ctl.handle_callback(c)
        c, m_x = cb(old)  # A's old button
        await self.ctl.handle_callback(c)
        self.assertIn("устарела", said(m_x))
        for d in (da, db):
            self.assertEqual(self.ctl.store.get(OWNER, d["id"])["version"], 1)
        c, _ = cb(next(v for v in buttons(m_b).values() if v.startswith("pl:sp:")))
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.store.get(OWNER, db["id"])["version"], 2)
        self.assertEqual(self.ctl.store.get(OWNER, da["id"])["version"], 1)

    async def test_listing_and_lines_work_without_warehouse(self):
        await self.prelim()
        m = msg("Мои планы")
        await self.ctl.handle_message(m)
        self.assertIn("без склада", next(iter(buttons(m))))

    async def test_unknown_warehouse_text_is_not_swallowed(self):
        m = await self.start()
        m.text = "12345678"
        self.assertFalse(await self.ctl.handle_message(m))


def araw(actions=(), scope="this_plan", questions=(), understood="Исключить W1 и поставить машину 20"):
    base = dict(type=None, key=None, value=None, wild=None, article=None, cabinet=None, quantity=None, reason=None)
    return dict(understood=understood, scope=scope, actions=[dict(base, **a) for a in actions], questions=list(questions))


class AssistantTests(UiBase):
    def setUp(self):
        super().setUp()
        self.calls, self.answers = [], []

    def ai(self, text, rules, snapshot, lines=()):
        def call(prompt):
            self.calls.append(prompt)
            return self.answers.pop(0)
        return planner_ai.interpret(text, rules, snapshot, lines, call=call)

    async def settings(self):
        m, b = await self.plan()
        c, _ = cb(b["Настройки"], m)
        await self.ctl.handle_callback(c)
        return m

    def versions(self):
        return self.ctl.store.list(OWNER)[0]["latest_version"]

    async def test_free_text_is_proposed_then_applied_only_after_confirmation(self):
        self.answers = [araw([dict(type="set_capacity", value=20), dict(type="exclude_wild", wild="w1")])]
        m = await self.settings()
        m.text = "Пожалуйста, возьми машину поменьше и не бери первый товар"
        self.assertTrue(await self.ctl.handle_message(m))
        out = said(m)
        self.assertIn("Понял так", out)
        self.assertIn("Машина (паллет): 33 → 20", out)
        self.assertIn("Исключённые wild:", out)
        self.assertEqual((len(self.calls), self.versions()), (1, 1))  # nothing applied by the model answer
        c, m2 = cb(buttons(m)["Применить"])
        await self.ctl.handle_callback(c)
        d = self.ctl.store.list(OWNER)[0]
        self.assertEqual((d["latest_version"], d["plan"]["rules"]["capacity"], d["plan"]["rules"]["excluded_wild"]),
                         (2, 20, ["W1"]))
        self.assertNotIn("W1", [ln["wild"] for ln in d["plan"]["lines"]])  # recalculated by Python
        self.assertEqual(self.sheets.published, [])

    async def test_fixed_commands_never_call_the_model(self):
        m = await self.settings()
        m.text = "Машина 20"
        await self.ctl.handle_message(m)
        self.assertEqual(self.calls, [])
        self.assertIn("Машина (паллет): 33 → 20", said(m))

    async def test_unclear_request_asks_and_keeps_context(self):
        self.answers = [araw(questions=["Какой именно товар исключить?"], understood="Исключить какой-то товар"),
                        araw([dict(type="exclude_wild", wild="W2")])]
        m = await self.settings()
        m.text = "исключи мыло"
        await self.ctl.handle_message(m)
        self.assertIn("Какой именно товар", said(m))
        self.assertNotIn("Применить", buttons(m))  # nothing to confirm while questions are open
        self.assertEqual(self.versions(), 1)
        m.text = "второй"
        await self.ctl.handle_message(m)
        self.assertIn("исключи мыло", self.calls[1])  # the earlier request and the question travel with the answer
        self.assertIn("Какой именно товар", self.calls[1])
        self.assertIn("Применить", buttons(m))

    async def test_invented_item_is_asked_not_applied(self):
        self.answers = [araw([dict(type="exclude_wild", wild="W404")])]
        m = await self.settings()
        m.text = "убери товар который плохо продаётся"
        await self.ctl.handle_message(m)
        self.assertIn("Не нашёл wild", said(m))
        self.assertNotIn("Применить", buttons(m))
        self.assertEqual(self.versions(), 1)

    async def test_model_failure_reports_exact_reason_and_changes_nothing(self):
        def down(*a, **k):
            raise planner_ai.AIUnavailable("Codex недоступен: лимит подписки ChatGPT исчерпан. Платный API не используется.")
        self.ai = down
        m = await self.settings()
        m.text = "исключи мыло"
        self.assertTrue(await self.ctl.handle_message(m))
        self.assertIn("лимит подписки", said(m))
        self.assertEqual(self.versions(), 1)
        m.text = "Машина 20"  # the fixed commands still work
        await self.ctl.handle_message(m)
        self.assertIn("Применить", buttons(m))

    async def test_garbage_model_output_changes_nothing(self):
        self.answers = [{"hello": "world"}]
        m = await self.settings()
        m.text = "сделай что-нибудь"
        await self.ctl.handle_message(m)
        self.assertIn("не прошёл проверку", said(m))
        self.assertEqual(self.versions(), 1)

    async def test_no_change_and_long_text_and_non_owner(self):
        self.answers = [araw()]
        m = await self.settings()
        m.text = "ничего не меняй"
        await self.ctl.handle_message(m)
        self.assertIn("Изменений в условиях нет", said(m))
        m.text = "x" * 601
        await self.ctl.handle_message(m)
        self.assertIn("Слишком длинное", said(m))
        self.assertEqual(len(self.calls), 1)
        self.owner_ok = False
        m.text = "исключи мыло"
        self.assertFalse(await self.ctl.handle_message(m))
        self.assertEqual(len(self.calls), 1)

    async def test_other_scope_is_shown_and_not_saved_to_memory_yet(self):
        self.answers = [araw([dict(type="exclude_wild", wild="W2")], scope="permanent_rule")]
        m = await self.settings()
        m.text = "всегда не бери мыло"
        await self.ctl.handle_message(m)
        self.assertIn("постоянное правило", said(m))
        self.assertIn("в память это пока не сохраняется", said(m).lower())


class EditPublishTests(UiBase):
    async def test_settings_require_apply_and_stale_rejected(self):
        m, b = await self.plan()
        c, _ = cb(b["Настройки"], m)
        await self.ctl.handle_callback(c)
        m.text = "Цель 9"
        self.assertTrue(await self.ctl.handle_message(m))
        self.assertIn("пока не применены", said(m))
        apply_data = buttons(m)["Применить"]
        stored = self.ctl.store.list(OWNER)
        self.assertEqual((len(stored), stored[0]["latest_version"]), (1, 1))  # typing text mutates nothing
        c, _ = cb(apply_data, m)
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.store.list(OWNER)[0]["latest_version"], 2)
        self.assertEqual(self.ctl.store.get(OWNER, stored[0]["id"], 2)["plan"]["rules"]["target_days"], 9)
        c, m2 = cb(apply_data, m)  # replay of the same button
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.store.list(OWNER)[0]["latest_version"], 2)
        c, m3 = cb(b["Пересчитать"], m)  # button of the superseded version
        await self.ctl.handle_callback(c)
        self.assertIn("устарела", said(m3))
        self.assertEqual(self.ctl.store.list(OWNER)[0]["latest_version"], 2)

    async def test_bad_commands_and_menu_cancel_pending(self):
        m, b = await self.plan()
        c, _ = cb(b["Настройки"], m)
        await self.ctl.handle_callback(c)
        m.text = "сделай красиво"
        await self.ctl.handle_message(m)
        self.assertIn("Codex недоступен", said(m))  # unknown text goes to the AI path, which is down here
        m.text = "Машина 20"
        await self.ctl.handle_message(m)
        apply_data = buttons(m)["Применить"]
        self.assertFalse(await self.ctl.handle_message(msg("Главное меню")))
        c, m2 = cb(apply_data, m)
        await self.ctl.handle_callback(c)
        self.assertIn("устарело", said(m2))
        self.assertEqual(self.ctl.store.list(OWNER)[0]["latest_version"], 1)

    async def test_capacity_switch_goes_through_apply(self):
        m, b = await self.plan("33")
        c, _ = cb(b["Машина 20 паллет"], m)
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.store.list(OWNER)[0]["latest_version"], 1)
        c, _ = cb(buttons(m)["Применить"], m)
        await self.ctl.handle_callback(c)
        d = self.ctl.store.list(OWNER)[0]
        self.assertEqual((d["latest_version"], d["plan"]["rules"]["capacity"]), (2, 20))

    async def test_publish_needs_confirmation_and_never_repeats(self):
        m, b = await self.plan()
        did, ver = b["Состав"].split(":")[2:4]
        ok = f"pl:ok:{did}:{ver}"
        c, m1 = cb(ok, m)  # no preceding "Записать" step
        await self.ctl.handle_callback(c)
        self.assertEqual(self.sheets.published, [])
        c, _ = cb(b["Записать в тестовый лист"], m)
        await self.ctl.handle_callback(c)
        self.assertEqual(self.sheets.published, [])
        self.assertIn("Подтвердить запись", buttons(m))
        for _ in range(2):  # double click
            c, m2 = cb(buttons(m)["Подтвердить запись"], m)
            await self.ctl.handle_callback(c)
        self.assertEqual(self.sheets.published, [(did, int(ver))])
        self.assertIn("Уже записано", said(m2))
        self.assertTrue(self.ctl.store.get(OWNER, did)["export_url"].startswith("https://"))
        c, m3 = cb(b["Пересчитать"], m)
        await self.ctl.handle_callback(c)
        self.assertIn("устарела", said(m3))

    async def test_menu_exit_during_start_message_cancels_write(self):
        m, b = await self.plan()
        c, _ = cb(b["Записать в тестовый лист"], m)
        await self.ctl.handle_callback(c)

        async def leave(*a, **k):
            if a and "Перепроверяю" in str(a[0]):
                self.ctl.reset(OWNER)  # «Главное меню» pressed while the message is being sent
        c, m2 = cb(buttons(m)["Подтвердить запись"], m)
        m2.answer = AsyncMock(side_effect=leave)
        await self.ctl.handle_callback(c)
        self.assertEqual(self.sheets.published, [])

    async def test_cancel_or_revoke_during_snapshot_stores_nothing(self):
        m, b = await self.plan()
        did = b["Состав"].split(":")[2]
        before = self.ctl.store.get(OWNER, did)["latest_version"]
        for act in (lambda: self.ctl.reset(OWNER), lambda: setattr(self, "owner_ok", False)):
            self.sheets.hook = act
            c, _ = cb(b["Пересчитать"], m)
            await self.ctl.handle_callback(c)
            self.owner_ok, self.sheets.hook = True, None
            self.assertEqual(self.ctl.store.get(OWNER, did)["latest_version"], before)
        m2 = msg("Новый план")
        await self.ctl.handle_message(m2)
        m2.text = "Завтра"
        await self.ctl.handle_message(m2)
        c, _ = cb(buttons(m2)["Коледино"], m2)
        await self.ctl.handle_callback(c)
        self.sheets.hook = lambda: self.ctl.reset(OWNER)
        c, _ = cb(buttons(m2)["33 паллет"], m2)
        await self.ctl.handle_callback(c)
        self.assertEqual(len(self.ctl.store.list(OWNER, 10)), 1)

    async def test_reset_waits_for_inflight_store(self):
        m, b = await self.plan()
        did = b["Состав"].split(":")[2]
        real, seen = self.ctl.store.revise, {}

        def slow_revise(*a, **k):
            t = threading.Thread(target=self.ctl.reset, args=(OWNER,))
            t.start()  # menu pressed while the store is in flight
            time.sleep(0.2)
            seen["gen_during"] = self.ctl._gen.get(OWNER, 0)
            r = real(*a, **k)
            seen["thread"] = t
            return r
        gen0 = self.ctl._gen.get(OWNER, 0)
        with patch.object(self.ctl.store, "revise", side_effect=slow_revise):
            c, _ = cb(b["Пересчитать"], m)
            await self.ctl.handle_callback(c)
        seen["thread"].join()
        self.assertEqual(seen["gen_during"], gen0)  # reset could not slip in between check and store
        self.assertEqual(self.ctl.store.get(OWNER, did)["latest_version"], 2)

    async def test_changed_source_and_revoked_owner_block_write(self):
        m, b = await self.plan()
        c, _ = cb(b["Записать в тестовый лист"], m)
        await self.ctl.handle_callback(c)
        self.sheets.publish = AsyncMock()  # never reached
        with patch.object(self.ctl, "_mem_ok", side_effect=lambda: setattr(self, "owner_ok", False)):
            c, m2 = cb(buttons(m)["Подтвердить запись"], m)
            await self.ctl.handle_callback(c)
        self.sheets.publish.assert_not_called()
        self.owner_ok = True
        self.sheets.publish = FakeSheets().publish
        err = ps.SourceChangedError("Исходные данные изменились")
        c, _ = cb(b["Записать в тестовый лист"], m)
        await self.ctl.handle_callback(c)
        with patch.object(self.sheets, "publish", side_effect=err):
            c, m3 = cb(buttons(m)["Подтвердить запись"], m)
            await self.ctl.handle_callback(c)
        self.assertIn("Нажмите «Пересчитать»", said(m3))
        self.assertIsNone(self.ctl.store.get(OWNER, b["Состав"].split(":")[2])["exported_version"])

    async def test_lines_memory_and_old_version_view(self):
        m, b = await self.plan()
        c, m2 = cb(b["Состав"], m)
        await self.ctl.handle_callback(c)
        self.assertIn("W1", said(m2))
        self.ctl.memory.propose("Не отгружать зимой", scope="global", kind="rule")
        mid = self.ctl.memory.entries()[0]["id"]
        self.ctl.memory.confirm(mid)
        self.ctl.memory.propose("Вентиляторы по поручению", scope="global", kind="case")
        c, m3 = cb(b["Память"], m)
        await self.ctl.handle_callback(c)
        self.assertIn("Не отгружать зимой", said(m3))
        self.assertNotIn("Вентиляторы", said(m3))  # pending is never effective


class ParserTests(unittest.TestCase):
    R = None

    def setUp(self):
        import planner
        self.R = planner.default_rules()

    def test_commands(self):
        for text, key, value in (("Продажи от 9", "min_sales", 9), ("Запас до 12,5", "max_current_days", 12.5),
                                 ("остаток wild от 40", "min_wild_days", 40), ("Цель 8", "target_days", 8),
                                 ("Максимум 14", "max_days", 14), ("Машина 20", "capacity", 20),
                                 ("Исключить wild wild123", "excluded_wild", ["wild123"]),
                                 ("Исключить кабинет Мой  Кабинет", "excluded_cabinets", ["Мой Кабинет"])):
            new, summary = ui.parse_command(text, self.R)
            self.assertEqual(new[key], value, text)
            self.assertTrue(summary)
        new, _ = ui.parse_command("Добавить A1 | Каб | 12 | по поручению", self.R)
        self.assertEqual(new["fixed"], [dict(article="A1", cabinet="Каб", quantity=12, reason="по поручению")])
        back, _ = ui.parse_command("Убрать A1 | Каб", new)
        self.assertEqual(back["fixed"], [])

    def test_refusals(self):
        for text in ("", "сделай красиво", "Цель -3", "Цель 99999", "Цель 20", "Машина 25", "Продажи от 8",
                     "Добавить A | B | 0 | x", "Добавить A | B | x", "Убрать A | B", "x" * 400):
            with self.assertRaises(ValueError, msg=text):
                ui.parse_command(text, self.R)

    def test_warning_summary_is_bounded(self):
        ws = [f"test row {i}: missing or invalid wild/article/cabinet; skipped" for i in range(26000)]
        cats, examples = ui.warning_summary(ws)
        self.assertEqual((len(cats), len(examples)), (1, 3))
        self.assertIn("26000", cats[0])


class TelegramWiringTests(unittest.IsolatedAsyncioTestCase):
    def test_main_menu_and_declaration_keyboards(self):
        main = [[b.text for b in r] for r in app.keyboard().keyboard]
        self.assertEqual(main, [["Новый план", "Мои планы"], ["Декларации"]])
        old = {b.text for r in app.declarations_keyboard().keyboard for b in r}
        self.assertTrue({"Ближайшая отгрузка", "Обновить базу", "База файлов", "Сегодня", "Завтра", "Выбрать дату",
                         "Проверить отметки", "Файлы по кабинету", "По номеру поставки", "Главное меню"} <= old)

    def test_declaration_callback_registered_before_planner(self):
        names = [h.callback.__name__ for h in app.create_dispatcher().callback_query.handlers]
        self.assertLess(names.index("completion_callback"), names.index("planner_callback"))

    async def test_text_routes(self):
        dispatcher = app.create_dispatcher()
        handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == "text_message")
        m = msg("Декларации", uid=123)
        with patch.object(app, "setting", return_value="123"), patch.object(ui.PlannerController, "_new",
                                                                           new=AsyncMock(return_value=True)) as new:
            await handler(m)
            self.assertEqual(len(m.answer.call_args.kwargs["reply_markup"].keyboard), 7)
            m.text = "Новый план"
            await handler(m)
            new.assert_awaited_once()
            m.text = "Главное меню"
            await handler(m)
            self.assertEqual([[b.text for b in r] for r in m.answer.call_args.kwargs["reply_markup"].keyboard][0],
                             ["Новый план", "Мои планы"])

    async def test_supply_number_still_delivers(self):
        dispatcher = app.create_dispatcher()
        handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == "text_message")
        m = msg("12345678", uid=123)
        with patch.object(app, "setting", return_value="123"), \
             patch.object(app, "generate_files", new_callable=AsyncMock,
                          return_value=(None, dict(total=0), None)) as gen:
            m.answer = AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock()))
            await handler(m)
            gen.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
