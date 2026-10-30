import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

import planner
import planner_ai
import planner_experience as exp
import planner_ui
from planner_memory import MemoryStore
from test_planner_integration import OWNER, SNAP, UiBase, araw, buttons, cb, msg, said


class UnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mem = MemoryStore(os.path.join(self.tmp.name, "m.sqlite3"))

    def add(self, category="permanent_rule", effect=None, confirm=True, expires=None, **kw):
        e = exp.memory_entries(category, effect or {"excluded_wild": ["W1"]}, reason="потому что", plan_id="a" * 12,
                               plan_date="2026-10-30", today=date(2026, 10, 28), expires_on=expires)
        ids = []
        for x in e:
            i = self.mem.propose(**x)
            if confirm:
                self.mem.confirm(i)
            ids.append(i)
        return ids

    def sel(self, **kw):
        args = dict(plan_id=None, on_date="2026-10-30", wilds={"W1", "W2"})
        args.update(kw)
        return exp.select(self.mem.entries(), **args)

    def test_effect_between_and_persistable(self):
        old = planner.default_rules()
        new = dict(old, excluded_wild=["W1"], capacity=20)
        eff, removed = exp.effect_between(old, new)
        self.assertEqual((eff, removed), ({"excluded_wild": ["W1"], "set": {"capacity": 20}}, False))
        self.assertFalse(exp.persistable("permanent_rule", eff, removed)[0])  # a machine is chosen per plan, never remembered
        self.assertTrue(exp.persistable("once_exception", eff, removed)[0])
        self.assertTrue(exp.persistable("permanent_rule", {"excluded_wild": ["W1"], "set": {"min_sales": 9}}, False)[0])
        self.assertFalse(exp.persistable("product_preference", eff, removed)[0])  # only «не брать wild»
        self.assertTrue(exp.persistable("product_preference", {"excluded_wild": ["W1"]}, False)[0])
        fx = [dict(article="A", cabinet="C", quantity=6, reason="r")]
        eff2, _ = exp.effect_between(old, dict(old, fixed=fx))
        self.assertFalse(exp.persistable("permanent_rule", eff2, False)[0])  # fixed lines are never permanent
        self.assertTrue(exp.persistable("once_exception", eff2, False)[0])
        e3, removed3 = exp.effect_between(dict(old, excluded_wild=["W1"]), old)
        self.assertTrue(removed3)
        self.assertFalse(exp.persistable("once_exception", e3, removed3)[0])  # lifting a rule is never remembered
        self.assertFalse(exp.persistable("example", eff, False)[0])
        self.assertFalse(exp.persistable("once_exception", {}, False)[0])

    def test_confirmed_only_pending_rejected_revoked_expired_are_ignored(self):
        self.add(confirm=False)
        pend = self.add(confirm=False)[0]
        self.mem.reject(pend)
        gone = self.add()[0]
        self.mem.revoke(gone)
        self.add(expires="2026-10-29")
        self.assertEqual(self.sel(), [])
        live = self.add(expires="2026-10-30")[0]  # inclusive expiry day
        self.assertEqual([r["id"] for r, _ in self.sel()], [live])
        self.assertEqual(self.sel(on_date="2026-10-31"), [])

    def test_scopes(self):
        self.add("product_preference", {"excluded_wild": ["W1", "W2"]}, expires="2026-12-27")
        self.add("once_exception", {"excluded_cabinets": ["Каб"]})
        got = self.sel(wilds={"W1"}, plan_id=None)
        self.assertEqual([d["effect"] for _, d in got], [{"excluded_wild": ["W1"]}])
        got = self.sel(wilds=set(), plan_id="a" * 12)
        self.assertEqual([d["category"] for _, d in got], ["once_exception"])
        self.assertEqual(self.sel(wilds=set(), plan_id="b" * 12), [])

    def test_examples_and_malformed_lines_never_change_a_calculation(self):
        ex = exp.example_entry("в прошлый раз отправляли 100 шт", "a" * 12, "2026-10-30")
        self.assertEqual(ex["kind"], "case")
        self.mem.confirm(self.mem.propose(**ex))
        forged = dict(ex, text=ex["text"] + "\n" + exp.PREFIX + '{"category":"permanent_rule","effect":{"excluded_wild":["W1"]}}')
        self.mem.confirm(self.mem.propose(**forged))  # a case carrying an effect line is still inert
        for bad in ('{"category":"permanent_rule"}', 'not json', '{"category":"x","effect":{"excluded_wild":["W1"]}}',
                    '{"category":"permanent_rule","effect":{"evil":1}}',
                    '{"category":"permanent_rule","effect":{"set":{"capacity":25}}}',
                    '{"category":"permanent_rule","effect":{"excluded_wild":[1]}}',
                    '{"category":"once_exception","effect":{"excluded_wild":["W1"]}}'):  # wrong scope for the category
            self.mem.confirm(self.mem.propose(text="x\n" + exp.PREFIX + bad, scope="global", kind="rule"))
        self.mem.confirm(self.mem.propose(text="Просто текст правила", scope="global", kind="rule"))
        self.assertEqual(self.sel(plan_id="a" * 12), [])

    def test_merge_unions_exclusions_and_owner_settings_win(self):
        self.add("permanent_rule", {"excluded_wild": ["W1"], "set": {"min_sales": 12, "max_current_days": 9}})
        self.add("permanent_rule", {"excluded_cabinets": ["Каб"]})
        base = planner.validate_rules({"excluded_wild": ["W9"]})
        rules, notes = exp.merge(base, self.sel())
        self.assertEqual((rules["excluded_wild"], rules["excluded_cabinets"], rules["min_sales"],
                          rules["max_current_days"]), (["W9", "W1"], ["Каб"], 12, 9))
        self.assertEqual([n["applied"] for n in notes], [True, True])
        own = planner.validate_rules({"min_sales": 9})  # explicit value of this plan beats the remembered 12
        rules, notes = exp.merge(own, self.sel())
        self.assertEqual((rules["min_sales"], rules["max_current_days"]), (9, 9))
        self.assertIn("min_sales", notes[0]["note"])
        same = planner.validate_rules({"max_current_days": 10})  # explicitly chosen value EQUAL to the default still wins
        rules, _ = exp.merge(same, self.sel(), explicit={"max_current_days"})
        self.assertEqual(rules["max_current_days"], 10)
        rules, _ = exp.merge(same, self.sel())
        self.assertEqual(rules["max_current_days"], 9)  # not explicit -> the remembered rule applies
        self.assertEqual(base["excluded_wild"], ["W9"])  # inputs untouched

    def test_conflicting_memory_is_skipped_visibly(self):
        self.add("permanent_rule", {"set": {"target_days": 12}})  # fine alone, but this plan's own max_days is 11
        self.add("permanent_rule", {"set": {"target_days": 20}})  # invalid even alone: never active at all
        base = planner.validate_rules({"max_days": 11})
        rules, notes = exp.merge(base, self.sel())
        self.assertEqual(len(notes), 1)
        self.assertEqual(rules, base)
        self.assertFalse(notes[0]["applied"])
        self.assertIn("не применено", notes[0]["note"])
        self.assertIn("не применено", "\n".join(exp.experience_lines(notes)))

    def test_product_preference_has_owner_given_expiry_and_one_entry_per_wild(self):
        es = exp.memory_entries("product_preference", {"excluded_wild": ["W1", "W2"]}, reason="", plan_id="a" * 12,
                                plan_date="2026-10-30", today=date(2026, 10, 28), expires_on="2026-12-27")
        self.assertEqual([(e["scope"], e["target"], e["expires_on"]) for e in es],
                         [("product", w, "2026-12-27") for w in ("W1", "W2")])
        self.assertTrue(all(e["kind"] == "rule" for e in es))
        perm = exp.memory_entries("permanent_rule", {"excluded_wild": ["W1"]}, reason="", plan_id="a" * 12,
                                  plan_date="2026-10-30", today=date(2026, 10, 28))
        self.assertEqual((perm[0]["scope"], perm[0]["target"], perm[0]["expires_on"]), ("global", "", None))


class ExperienceFlowTests(UiBase):
    def setUp(self):
        super().setUp()
        self.calls, self.answers = [], []

    def ai(self, text, rules, snapshot, lines=(), **kw):
        def call(prompt):
            self.calls.append(prompt)
            return self.answers.pop(0)
        return planner_ai.interpret(text, rules, snapshot, lines, call=call, **kw)

    async def propose(self, answer, text="запомни"):
        self.answers = [answer]
        m, b = await self.plan()
        c, _ = cb(b["Настройки"], m)
        await self.ctl.handle_callback(c)
        m.text = text
        await self.ctl.handle_message(m)
        return m

    async def tap(self, m, label):
        c, m2 = cb(buttons(m)[label])  # buttons of the latest proposal
        await self.ctl.handle_callback(c)
        return m2

    def ids(self):
        return [(r["scope"], r["status"]) for r in self.ctl.memory.entries()]

    def latest(self):
        return self.ctl.store.list(OWNER)[0]

    async def test_permanent_rule_needs_two_taps_then_shapes_next_plans_and_can_be_revoked(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="permanent_rule", reason="нестабильное качество"))
        self.assertIn("Применить и запомнить", buttons(m))
        self.assertEqual(self.ctl.memory.entries(), [])  # a proposal alone stores nothing
        m2 = await self.tap(m, "Применить и запомнить")
        self.assertIn("ВСЕХ будущих", said(m2))
        self.assertEqual(self.ctl.memory.entries(), [])  # still nothing after the first tap
        m3 = await self.tap(m2, "Да, сохранить навсегда")
        self.assertEqual(self.ids(), [("global", "confirmed")])
        self.assertIn("Запомнил", said(m3))
        rec = self.ctl.memory.entries()[0]
        self.assertEqual((rec["reason"], rec["kind"], rec["expires_on"]), ("нестабильное качество", "rule", None))
        self.assertEqual(self.latest()["latest_version"], 2)
        # a brand-new plan picks the remembered rule up and says so
        m4, _ = await self.plan()
        d = self.latest()
        self.assertNotIn("W1", [ln["wild"] for ln in d["plan"]["lines"]])
        self.assertEqual([e["id"] for e in d["plan"]["experience"]], [rec["id"]])
        self.assertIn("Учтён опыт", said(m4))
        self.assertIn("постоянное правило", said(m4))
        self.assertEqual(d["plan"]["base_rules"]["excluded_wild"], [])  # the owner's own rules stay separate
        # revoke from the memory view; the next calculation no longer applies it
        c, mm = cb(buttons(m4)["Память"])
        await self.ctl.handle_callback(c)
        self.assertIn("действует в расчёте", said(mm))
        c, mr = cb(next(v for v in buttons(mm).values() if v.startswith("pl:rv:")))
        await self.ctl.handle_callback(c)
        self.assertIn("отозвана", said(mr))
        self.assertEqual(self.ids(), [("global", "revoked")])
        c, m5 = cb(buttons(m4)["Пересчитать"])
        await self.ctl.handle_callback(c)
        self.assertIn("W1", [ln["wild"] for ln in self.latest()["plan"]["lines"]])
        self.assertEqual(self.latest()["plan"]["experience"], [])
        self.assertEqual(self.sheets.published, [])

    async def test_owner_settings_beat_remembered_numbers_through_recalculations(self):
        self.ctl.memory.confirm(self.ctl.memory.propose(**exp.memory_entries(
            "permanent_rule", {"set": {"max_current_days": 6}}, reason="", plan_id="a" * 12, plan_date="2026-10-30",
            today=date(2026, 10, 28))[0]))
        m, b = await self.plan()
        self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 6)  # remembered, not explicit
        self.assertEqual(self.latest()["plan"]["rules"]["capacity"], 33)
        self.answers = [araw([dict(type="set_number", key="max_current_days", value=8)])]
        c, _ = cb(b["Настройки"], m)
        await self.ctl.handle_callback(c)
        m.text = "сделай окно запаса короче"
        await self.ctl.handle_message(m)
        await self.tap(m, "Применить")
        self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 8)  # the owner's value
        c, m2 = cb(buttons(m)["Пересчитать"] if "Пересчитать" in buttons(m) else self.latest_keys(m))
        await self.ctl.handle_callback(c)
        self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 8)
        self.assertIn("max_current_days", self.latest()["plan"]["explicit_keys"])

    def latest_keys(self, m):
        d = self.latest()
        return f"pl:rc:{d['id']}:{d['latest_version']}"

    async def test_explicit_value_equal_to_default_still_beats_memory(self):
        self.ctl.memory.confirm(self.ctl.memory.propose(**exp.memory_entries(
            "permanent_rule", {"set": {"max_current_days": 6}}, reason="", plan_id="a" * 12, plan_date="2026-10-30",
            today=date(2026, 10, 28))[0]))
        for via_ai in (False, True):
            self.answers = [araw([dict(type="set_number", key="max_current_days", value=10)])]
            m, b = await self.plan()
            self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 6)  # remembered, not explicit
            c, _ = cb(b["Настройки"], m)
            await self.ctl.handle_callback(c)
            m.text = "Запас до 10" if not via_ai else "поставь запас как раньше"
            await self.ctl.handle_message(m)
            await self.tap(m, "Применить")
            plan = self.latest()["plan"]
            self.assertEqual(plan["rules"]["max_current_days"], 10)  # the owner's own 10 == default, memory says 6
            self.assertIn("max_current_days", plan["explicit_keys"])
            c, _ = cb(self.latest_keys(m))  # a plain recalculation keeps the owner's choice
            await self.ctl.handle_callback(c)
            self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 10)
            self.assertEqual([e["applied"] and e["note"] != "" for e in self.latest()["plan"]["experience"]], [True])
            self.ctl._st.clear()

    async def test_apply_without_remembering_stores_nothing(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="permanent_rule"))
        await self.tap(m, "Применить к этому плану")
        self.assertEqual(self.ctl.memory.entries(), [])
        self.assertEqual(self.latest()["plan"]["rules"]["excluded_wild"], ["W1"])

    async def test_one_time_exception_is_bound_to_its_plan_only(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W2")], scope="once_exception", reason="акция"))
        self.assertIn("действует для этого плана", said(m))
        await self.tap(m, "Применить и запомнить")
        did = self.latest()["id"]
        rec = self.ctl.memory.entries()[0]
        self.assertEqual((rec["scope"], rec["target"], rec["expires_on"]), ("plan", did, self.latest()["plan"]["date"]))
        await self.plan()  # another plan: unaffected
        self.assertIn("W2", [ln["wild"] for ln in self.latest()["plan"]["lines"]])
        self.assertEqual(self.latest()["plan"]["experience"], [])

    async def test_product_preference_term_is_a_visible_proposal_chosen_by_owner(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1"), dict(type="exclude_wild", wild="W2")],
                                    scope="product_preference"))
        self.assertIn("Срок вы не называли", said(m))
        self.assertIn("60 дней", said(m))
        self.assertNotIn("Применить и запомнить", buttons(m))
        self.assertEqual([b for b in buttons(m) if b.startswith("Запомнить на")],
                         [f"Запомнить на {n} дн." for n in exp.TERM_CHOICES])
        self.assertEqual(self.ctl.memory.entries(), [])  # nothing stored until a term is chosen
        await self.tap(m, "Запомнить на 30 дн.")
        es = self.ctl.memory.entries()
        self.assertEqual(sorted(e["target"] for e in es), ["W1", "W2"])
        want = (datetime.now(planner_ui.MSK).date() + timedelta(days=30)).isoformat()
        self.assertTrue(all(e["scope"] == "product" and e["expires_on"] == want for e in es))

    async def test_excluding_product_preference_stays_revocable_from_next_plan_memory_view(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="product_preference"))
        await self.tap(m, "Запомнить на 30 дн.")
        m2, b = await self.plan()
        self.assertNotIn("W1", [ln["wild"] for ln in self.latest()["plan"]["lines"]])
        c, mm = cb(b["Память"])
        await self.ctl.handle_callback(c)
        c, _ = cb(next(v for v in buttons(mm).values() if v.startswith("pl:rv:")))
        await self.ctl.handle_callback(c)
        self.assertEqual([e["status"] for e in self.ctl.memory.entries()], ["revoked"])

    async def test_once_exception_keeps_the_term_the_owner_saw_and_chose(self):
        kw = dict(reason="", plan_id="a" * 12, plan_date="2026-11-02", today=date(2026, 10, 28))
        e = exp.memory_entries("once_exception", {"excluded_wild": ["W1"]}, expires_on="2026-10-31", **kw)[0]
        self.assertEqual(e["expires_on"], "2026-10-31")  # not replaced by the plan date
        e = exp.memory_entries("once_exception", {"excluded_wild": ["W1"]}, **kw)[0]
        self.assertEqual(e["expires_on"], "2026-11-02")  # no named term: valid for the plan's own date
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="once_exception",
                                    expires_on="2026-12-01"))
        self.assertIn("до 2026-12-01", said(m))
        await self.tap(m, "Применить и запомнить")
        self.assertEqual(self.ctl.memory.entries()[0]["expires_on"], "2026-12-01")

    async def test_every_memory_record_is_listed_and_revocable_across_pages(self):
        m, b = await self.plan()
        for i in range(20):  # more than one screen, more than the old 5 buttons / 15 lines
            self.ctl.memory.confirm(self.ctl.memory.propose(**exp.memory_entries(
                "permanent_rule", {"excluded_wild": [f"W9{i:02d}"]}, reason="", plan_id="a" * 12,
                plan_date="2026-10-30", today=date(2026, 10, 28))[0]))
        c, mm = cb(b["Память"])
        await self.ctl.handle_callback(c)
        revoke_ids, shown = [], ""
        for _ in range(10):
            shown += said(mm)
            revoke_ids += [v for v in buttons(mm).values() if v.startswith("pl:rv:")]
            nxt = buttons(mm).get("▶")
            if not nxt:
                break
            c, mm = cb(nxt)
            await self.ctl.handle_callback(c)
        self.assertEqual(len(set(revoke_ids)), 20)
        for i in range(20):
            self.assertIn(f"W9{i:02d}", shown)
        c, _ = cb(revoke_ids[-1])  # the very last record can be revoked on its own
        await self.ctl.handle_callback(c)
        self.assertEqual(sorted(r["status"] for r in self.ctl.memory.entries()).count("revoked"), 1)

    async def test_expired_once_exception_leaves_the_recalculated_plan_entirely(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1"), dict(type="set_number", key="max_current_days", value=8)],
                                    scope="once_exception", expires_on="2020-01-01"))  # term before the plan date
        await self.tap(m, "Применить и запомнить")
        d = self.latest()
        self.assertIn("W1", [ln["wild"] for ln in d["plan"]["lines"]])  # expired: the exclusion did not survive in base_rules
        self.assertEqual(d["plan"]["base_rules"]["excluded_wild"], [])
        self.assertEqual(d["plan"]["experience"], [])
        self.assertEqual(d["plan"]["rules"]["max_current_days"], 8)  # the owner's other setting stays
        c, _ = cb(self.latest_keys(m))  # a later recalculation keeps it gone
        await self.ctl.handle_callback(c)
        self.assertIn("W1", [ln["wild"] for ln in self.latest()["plan"]["lines"]])

    async def test_live_once_exception_applies_through_memory_and_goes_when_revoked(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="once_exception",
                                    expires_on="2099-01-01"))
        await self.tap(m, "Применить и запомнить")
        d = self.latest()
        self.assertNotIn("W1", [ln["wild"] for ln in d["plan"]["lines"]])
        self.assertEqual(len(d["plan"]["experience"]), 1)
        self.assertEqual(d["plan"]["base_rules"]["excluded_wild"], [])  # carried by memory only
        self.ctl.memory.revoke(self.ctl.memory.entries()[0]["id"])
        c, _ = cb(self.latest_keys(m))
        await self.ctl.handle_callback(c)
        self.assertIn("W1", [ln["wild"] for ln in self.latest()["plan"]["lines"]])

    async def test_memory_pages_fit_telegram_with_a_large_experience_block(self):
        for r in range(15):  # 15 rules x 12 wilds with 20-char names: the plan's experience block alone is >4096 chars
            self.ctl.memory.confirm(self.ctl.memory.propose(**exp.memory_entries(
                "permanent_rule", {"excluded_wild": [f"WILD{r:02d}{w:02d}".ljust(20, "x") for w in range(12)]},
                reason="", plan_id="a" * 12, plan_date="2026-10-30", today=date(2026, 10, 28))[0]))
        m, b = await self.plan()
        self.assertEqual(len(self.latest()["plan"]["experience"]), 15)
        c, mm = cb(b["Память"])
        await self.ctl.handle_callback(c)
        pages = 0
        while True:
            pages += 1
            last = mm.answer.call_args_list[-1]
            self.assertLessEqual(len(last.args[0]), 4096)
            self.assertTrue([v for v in buttons(mm).values() if v.startswith("pl:rv:")])
            nxt = buttons(mm).get("▶")
            if not nxt:
                break
            c, mm = cb(nxt)
            await self.ctl.handle_callback(c)
        self.assertEqual(pages, 2)

    async def test_permanent_rule_first_button_cannot_double_tap_into_saving(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="permanent_rule"))
        first = buttons(m)["Применить и запомнить"]
        m2 = await self.tap(m, "Применить и запомнить")
        self.assertNotEqual(first, buttons(m2)["Да, сохранить навсегда"])
        c, _ = cb(first, m2)  # impatient second tap on the SAME first button
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries(), [])
        await self.tap(m2, "Да, сохранить навсегда")
        self.assertEqual(self.ids(), [("global", "confirmed")])

    async def test_typed_date_continues_the_open_product_preference(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="product_preference"),
                               "не бери W1 пока")
        self.answers = [araw([dict(type="exclude_wild", wild="W1")], scope="product_preference", expires_on="2026-12-01")]
        m.text = "до 01.12.2026"
        await self.ctl.handle_message(m)
        self.assertIn("не бери W1 пока", self.calls[-1])  # the original request travels with the date
        self.assertIn("до 01.12.2026", self.calls[-1])
        await self.tap(m, "Применить и запомнить")
        self.assertEqual(self.ctl.memory.entries()[0]["expires_on"], "2026-12-01")

    async def test_product_preference_without_a_term_is_refused(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="product_preference"))
        c, _ = cb(buttons(m)["Запомнить на 30 дн."].rsplit(":", 1)[0])  # a forged tap without a term
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries(), [])
        with self.assertRaises(ValueError):
            exp.memory_entries("product_preference", {"excluded_wild": ["W1"]}, reason="", plan_id="a" * 12,
                               plan_date="2026-10-30", today=date(2026, 10, 28))

    async def test_product_preference_with_owner_named_date_is_stored_as_named(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="product_preference",
                                    expires_on="2026-12-01"))
        self.assertNotIn("Срок вы не называли", said(m))
        await self.tap(m, "Применить и запомнить")
        self.assertEqual(self.ctl.memory.entries()[0]["expires_on"], "2026-12-01")

    async def test_memory_that_cannot_be_remembered_is_explained(self):
        fx = dict(type="add_fixed", article="A1", cabinet="Каб", quantity=6, reason="акция")
        m = await self.propose(araw([fx], scope="permanent_rule"))
        self.assertNotIn("Применить и запомнить", buttons(m))
        self.assertIn("В память это записать нельзя", said(m))
        self.assertIn("Применить", buttons(m))

    async def test_example_is_saved_only_as_non_policy_case(self):
        m = await self.propose(araw(scope="example", understood="В прошлый раз отправили 100 шт"),
                               "в прошлый раз отправляли 100 шт A1, ориентируйся")
        self.assertIn("Сохранить как пример", buttons(m))
        self.assertEqual(self.ctl.memory.entries(), [])
        m2 = await self.tap(m, "Сохранить как пример")
        rec = self.ctl.memory.entries()[0]
        self.assertEqual((rec["kind"], rec["status"], rec["scope"]), ("case", "confirmed", "plan"))
        self.assertIn("не правило", said(m2))
        await self.plan()
        self.assertEqual(self.latest()["plan"]["experience"], [])  # an example never shapes a calculation
        self.assertEqual(self.latest()["plan"]["rules"], planner.validate_rules({"capacity": 33}))

    async def test_unconfirmed_and_foreign_entries_do_not_apply(self):
        self.ctl.memory.propose(**exp.memory_entries("permanent_rule", {"excluded_wild": ["W1"]}, reason="", plan_id="a" * 12,
                                                     plan_date="2026-10-30", today=date(2026, 10, 28))[0])  # pending
        await self.plan()
        self.assertIn("W1", [ln["wild"] for ln in self.latest()["plan"]["lines"]])
        self.assertEqual(self.latest()["plan"]["experience"], [])

    async def test_failed_recalculation_withdraws_the_new_memory(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="permanent_rule"))
        m2 = await self.tap(m, "Применить и запомнить")
        calls = []

        def boom():
            calls.append(1)
            raise RuntimeError("sheet down")
        self.sheets.hook = boom
        m3 = await self.tap(m2, "Да, сохранить навсегда")
        self.assertTrue(calls)
        self.assertEqual(self.ids(), [("global", "revoked")])
        self.assertIn("отозвана", said(m3))
        self.assertEqual(self.latest()["latest_version"], 1)

    async def test_menu_exit_during_remember_leaves_no_active_memory(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="once_exception"))
        orig = self.ctl.memory.confirm

        def confirm_then_menu(i):
            r = orig(i)
            self.ctl._gen[OWNER] = self.ctl._gen.get(OWNER, 0) + 1  # the owner hits the menu right after the write
            return r
        self.ctl.memory.confirm = confirm_then_menu
        await self.tap(m, "Применить и запомнить")
        self.assertEqual([r["status"] for r in self.ctl.memory.entries()], ["revoked"])
        self.assertEqual(self.latest()["latest_version"], 1)

    async def test_stale_buttons_and_memory_outage_remember_nothing(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="once_exception"))
        data = buttons(m)["Применить и запомнить"]
        await self.tap(m, "Отмена")
        c, m2 = cb(data)
        await self.ctl.handle_callback(c)
        self.assertIn("устарело", said(m2))
        for bad in ("pl:ar:" + "0" * 12 + ":1:abc123", "pl:ex:" + "0" * 12 + ":1:abc123", "pl:rv:not-a-uuid"):
            c, m3 = cb(bad)
            await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries(), [])

    async def test_memory_rule_cannot_be_lifted_by_free_text_only_by_revoking(self):
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="permanent_rule"))
        m = await self.tap(await self.tap(m, "Применить и запомнить"), "Да, сохранить навсегда")
        m4, b4 = await self.plan()
        c, _ = cb(b4["Настройки"], m4)
        await self.ctl.handle_callback(c)
        self.answers = [araw([dict(type="include_wild", wild="W1")])]
        m4.text = "верни W1"
        await self.ctl.handle_message(m4)
        self.assertIn("отзовите его в разделе «Память»", said(m4))
        self.assertNotIn("Применить", buttons(m4))  # nothing to confirm: the request was refused with an explanation


if __name__ == "__main__":
    unittest.main()
