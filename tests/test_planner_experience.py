import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock
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

    def test_effect_between_and_without_effect_compare_line_values_not_only_keys(self):
        old = planner.default_rules()
        old["fixed"] = [dict(article="A1", cabinet="K", quantity=6, reason="r"), dict(article="A2", cabinet="K", quantity=4, reason="r")]
        new = planner.default_rules()
        new["fixed"] = [dict(article="A1", cabinet="K", quantity=12, reason="r"), dict(article="A2", cabinet="K", quantity=4, reason="r")]
        eff, removed = exp.effect_between(old, new)
        self.assertEqual(eff, {"fixed": [dict(article="A1", cabinet="K", quantity=12, reason="r")]})
        self.assertFalse(removed)
        new["fixed"][1]["reason"] = "другая причина"  # a changed reason is a changed line too
        self.assertEqual(len(exp.effect_between(old, new)[0]["fixed"]), 2)
        back = exp.without_effect(new, eff, old)
        self.assertEqual([(f["article"], f["quantity"]) for f in back["fixed"]], [("A1", 6), ("A2", 4)])
        self.assertEqual(exp.without_effect(new, eff)["fixed"][0]["article"], "A2")  # no prior: a pure addition is dropped

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
        self.add("permanent_rule", {"set": {"target_days": 20}})  # a valid record; it just does not fit THIS plan either
        base = planner.validate_rules({"max_days": 11})
        rules, notes = exp.merge(base, self.sel())
        self.assertEqual(len(notes), 2)
        self.assertEqual(rules, base)
        self.assertFalse(any(n["applied"] for n in notes))
        self.assertTrue(all("не применено" in n["note"] for n in notes))
        self.assertIn("не применено", "\n".join(exp.experience_lines(notes)))
        roomy = planner.validate_rules({"max_days": 30})  # the same record is applicable where the plan's own limits allow it
        rules, notes = exp.merge(roomy, self.sel())
        self.assertEqual((rules["target_days"], notes[-1]["applied"]), (20, True))
        for bad in ({"set": {"target_days": 0}}, {"set": {"capacity": 25}}, {"set": {"min_sales": True}},
                    {"fixed": [{"article": "A"}]}):  # a genuinely malformed record is still inert
            line = exp.PREFIX + json.dumps({"category": "permanent_rule", "effect": bad})
            self.assertIsNone(exp.decode(dict(kind="rule", scope="global", text="x\n" + line)))

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

    # --- a remembered change of an EXISTING required line (value, not only key) ------------------------------------
    async def step(self, m2, answer, label):
        """Owner opens settings on the latest plan message, sends text, taps `label` -> message of the shown new version."""
        self.answers = [answer]
        c, _ = cb(buttons(m2)["Настройки"], m2)
        await self.ctl.handle_callback(c)
        m2.text = "правка"
        await self.ctl.handle_message(m2)
        return await self.tap(m2, label)

    @staticmethod
    def fx(rules):
        return sorted((f["article"], f["quantity"]) for f in rules["fixed"])

    @staticmethod
    def qty(d, art):
        return [ln["quantity"] for ln in d["plan"]["lines"] if ln["article"] == art]

    async def mixed(self, expires_on="2099-01-01"):
        """Plan v2: the owner's own A1×6 and max_current_days=8; then ONE remembered proposal: A1 6->12 + W2 excluded."""
        m, _ = await self.plan()
        add = lambda art, q: dict(type="add_fixed", article=art, cabinet="Каб", quantity=q, reason="по поручению")  # noqa: E731
        m2 = await self.step(m, araw([add("A1", 6), dict(type="set_number", key="max_current_days", value=8)]),
                             "Применить")
        before = self.latest()
        self.assertEqual(self.fx(before["plan"]["base_rules"]), [("A1", 6)])
        m3 = await self.step(m2, araw([add("A1", 12), dict(type="exclude_wild", wild="W2")], scope="once_exception",
                                      expires_on=expires_on), "Применить и запомнить")
        return m3

    async def test_remembered_quantity_change_is_carried_by_memory_not_by_base_rules(self):
        m3 = await self.mixed()
        d = self.latest()
        self.assertEqual(self.fx(d["plan"]["base_rules"]), [("A1", 6)])  # the owner's own values, not the exception
        self.assertEqual(d["plan"]["base_rules"]["excluded_wild"], [])
        self.assertEqual(self.qty(d, "A1"), [12])  # ...yet the plan uses the remembered 12
        self.assertNotIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])
        self.assertEqual(d["plan"]["rules"]["max_current_days"], 8)
        rec = self.ctl.memory.entries()
        self.assertEqual(len(rec), 1)
        self.assertEqual(exp.decode(rec[0])["effect"]["fixed"], [dict(article="A1", cabinet="Каб", quantity=12, reason="по поручению")])
        c, _ = cb(self.latest_keys(m3))  # a later recalculation keeps the same result
        await self.ctl.handle_callback(c)
        d = self.latest()
        self.assertEqual((self.qty(d, "A1"), self.fx(d["plan"]["base_rules"])), ([12], [("A1", 6)]))
        self.assertNotIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])

    async def test_revoked_quantity_change_returns_to_the_owners_value_and_keeps_independent_settings(self):
        m3 = await self.mixed()
        self.ctl.memory.revoke(self.ctl.memory.entries()[0]["id"])
        c, _ = cb(self.latest_keys(m3))
        await self.ctl.handle_callback(c)
        d = self.latest()
        self.assertEqual(self.qty(d, "A1"), [6])
        self.assertIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])
        self.assertEqual(self.fx(d["plan"]["rules"]), [("A1", 6)])
        self.assertEqual(d["plan"]["rules"]["max_current_days"], 8)  # the owner's explicit number stays
        self.assertEqual(d["plan"]["experience"], [])

    async def test_independent_required_line_of_a_third_wild_survives_revoke_of_a_mixed_record(self):
        snap = self.ctl.sheets.snap
        snap["test"] = list(snap["test"]) + [["W3", "A3", "Каб", "Товар3", 500, 5, 10, 10, 10, 10, 6, 6, 60]]
        snap["packing"] = list(snap["packing"]) + [["W3", 1, 1, 10, 40]]
        m, _ = await self.plan()
        add = lambda art, q: dict(type="add_fixed", article=art, cabinet="Каб", quantity=q, reason="по поручению")  # noqa: E731
        m2 = await self.step(m, araw([add("A1", 6), add("A3", 10)]), "Применить")
        m3 = await self.step(m2, araw([add("A1", 12), dict(type="exclude_wild", wild="W2")], scope="once_exception",
                                      expires_on="2099-01-01"), "Применить и запомнить")
        d = self.latest()
        self.assertEqual((self.qty(d, "A1"), self.qty(d, "A3")), ([12], [10]))
        self.assertNotIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])
        self.assertEqual(self.fx(d["plan"]["base_rules"]), [("A1", 6), ("A3", 10)])
        self.ctl.memory.revoke(self.ctl.memory.entries()[0]["id"])
        c, _ = cb(self.latest_keys(m3))
        await self.ctl.handle_callback(c)
        d = self.latest()
        self.assertEqual((self.qty(d, "A1"), self.qty(d, "A3")), ([6], [10]))  # only the remembered part went away
        self.assertIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])
        self.assertEqual(self.fx(d["plan"]["rules"]), [("A1", 6), ("A3", 10)])
        self.assertEqual(d["plan"]["experience"], [])

    # --- contract fixes after the external review of 6127d90 --------------------------------------------------------
    def remember_global(self, effect):
        self.ctl.memory.confirm(self.ctl.memory.propose(**exp.memory_entries(
            "permanent_rule", effect, reason="", plan_id="a" * 12, plan_date="2026-10-30", today=date(2026, 10, 28))[0]))

    async def test_explicit_number_equal_to_the_base_is_remembered_alone_and_with_an_exception(self):
        for extra in ([], [dict(type="exclude_wild", wild="W2")]):
            for r in self.ctl.memory.entries():
                if r["status"] == "confirmed":
                    self.ctl.memory.revoke(r["id"])  # every variant starts from only the old remembered 6
            self.remember_global({"set": {"max_current_days": 6}})
            m, _ = await self.plan()
            self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 6)  # memory applies to a fresh plan
            self.answers = [araw([dict(type="set_number", key="max_current_days", value=10)] + extra, scope="permanent_rule")]
            c, _ = cb(buttons(m)["Настройки"], m)
            await self.ctl.handle_callback(c)
            m.text = "10 дней, запомни"
            await self.ctl.handle_message(m)
            self.assertIn("Применить и запомнить", buttons(m), msg=said(m))  # not "nothing to remember"
            m2 = await self.tap(m, "Применить и запомнить")
            await self.tap(m2, "Да, сохранить навсегда")
            mine = [exp.decode(r)["effect"] for r in self.ctl.memory.entries() if r["status"] == "confirmed"][-1]
            self.assertEqual(mine.get("set"), {"max_current_days": 10})  # only the touched number, no other numbers
            self.assertEqual("excluded_wild" in mine, bool(extra))
            self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 10)
            await self.plan()  # a brand-new plan now gets the newer remembered 10, not the old 6
            self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 10)

    async def test_partial_effect_with_dependent_plan_settings_is_saved_applied_listed_and_revocable(self):
        m, _ = await self.plan()
        m2 = await self.step(m, araw([dict(type="set_number", key="max_days", value=30)]), "Применить")
        m3 = await self.step(m2, araw([dict(type="set_number", key="target_days", value=20), dict(type="exclude_wild", wild="W2")],
                                      scope="once_exception", expires_on="2099-01-01"), "Применить и запомнить")
        rec = self.ctl.memory.entries()
        self.assertEqual(len(rec), 1)
        self.assertIsNotNone(exp.decode(rec[0]))  # not thrown away for failing on someone else's defaults
        d = self.latest()
        self.assertNotIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])
        self.assertEqual((d["plan"]["rules"]["target_days"], d["plan"]["rules"]["max_days"]), (20, 30))
        self.assertTrue(d["plan"]["experience"][0]["applied"])
        c, mm = cb(buttons(m3)["Память"], m3)
        await self.ctl.handle_callback(c)
        rv = [v for v in buttons(mm).values() if v.startswith("pl:rv:")]
        self.assertEqual(len(rv), 1)
        c, rm = cb(rv[0])
        await self.ctl.handle_callback(c)
        self.assertEqual([r["status"] for r in self.ctl.memory.entries()], ["revoked"])
        self.assertIn("Запись отозвана", said(rm))
        c, _ = cb(self.latest_keys(m3))
        await self.ctl.handle_callback(c)
        d = self.latest()
        self.assertIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])
        self.assertEqual(d["plan"]["rules"]["max_days"], 30)  # the owner's own setting of this plan stays

    async def remember_with_failures(self, recalc_fails, revoke_fails):
        from contextlib import ExitStack
        from unittest.mock import patch
        m, _ = await self.plan()
        self.answers = [araw([dict(type="exclude_wild", wild="W2")], scope="once_exception", expires_on="2099-01-01")]
        c, _ = cb(buttons(m)["Настройки"], m)
        await self.ctl.handle_callback(c)
        m.text = "запомни"
        await self.ctl.handle_message(m)
        c, m2 = cb(buttons(m)["Применить и запомнить"])
        with ExitStack() as st:
            if recalc_fails:
                st.enter_context(patch.object(self.ctl, "_revise", AsyncMock(return_value=None)))
            if revoke_fails:
                st.enter_context(patch.object(self.ctl.memory, "revoke", side_effect=RuntimeError("db down")))
            await self.ctl.handle_callback(c)
        return said(m2), [r["status"] for r in self.ctl.memory.entries()]

    async def test_recalc_failure_withdraws_the_record_and_says_so(self):
        text, status = await self.remember_with_failures(recalc_fails=True, revoke_fails=False)
        self.assertEqual(status, ["revoked"])
        self.assertIn("запись отозвана", text.lower())

    async def test_recalc_failure_with_failed_revoke_never_claims_the_record_is_revoked(self):
        text, status = await self.remember_with_failures(recalc_fails=True, revoke_fails=True)
        self.assertEqual(status, ["confirmed"])  # the record really is still active...
        self.assertIn("НЕ удался", text)  # ...and the owner is told so, and where to fix it
        self.assertIn("«Память»", text)
        self.assertNotIn("Запись отозвана", text)

    async def failing_write(self, revoke_fails):
        """Two product records; the second confirm fails inside remember(), before any recalculation."""
        from contextlib import ExitStack
        from unittest.mock import patch
        m = await self.propose(araw([dict(type="exclude_wild", wild="W1"), dict(type="exclude_wild", wild="W2")],
                                    scope="product_preference"))
        versions = len(self.ctl.store.list(OWNER))
        c, m2 = cb(buttons(m)["Запомнить на 30 дн."])
        real, calls = self.ctl.memory.confirm, []

        def confirm(i):
            calls.append(i)
            if len(calls) == 2:
                raise RuntimeError("db down")
            return real(i)
        with ExitStack() as st:
            st.enter_context(patch.object(self.ctl.memory, "confirm", side_effect=confirm))
            if revoke_fails:
                st.enter_context(patch.object(self.ctl.memory, "revoke", side_effect=RuntimeError("db down")))
            await self.ctl.handle_callback(c)
        by = {r["target"]: r["status"] for r in self.ctl.memory.entries()}
        return said(m2), by, versions

    async def test_write_failure_before_recalc_withdraws_both_records_and_keeps_the_plan(self):
        text, by, versions = await self.failing_write(revoke_fails=False)
        self.assertNotIn("confirmed", by.values())  # the first (confirmed) one is revoked, the second never confirmed
        self.assertEqual(by["W1"], "revoked")
        self.assertEqual(len(self.ctl.store.list(OWNER)), versions)  # no recalculation, plan untouched
        self.assertNotIn("Запомнил", text)
        self.assertNotIn("НЕ удался", text)

    async def test_write_failure_before_recalc_with_failed_revoke_tells_the_owner_what_is_still_active(self):
        text, by, versions = await self.failing_write(revoke_fails=True)
        self.assertEqual(by["W1"], "confirmed")  # really still active...
        self.assertNotEqual(by["W2"], "confirmed")
        self.assertIn("НЕ удался", text)  # ...and the owner is told so
        self.assertIn("«Память»", text)
        self.assertNotIn("Запомнил", text)
        self.assertNotIn("Запись отозвана", text)
        self.assertEqual(len(self.ctl.store.list(OWNER)), versions)

    # --- the four MAJOR of the review of 039795d ---------------------------------------------------------------------
    async def test_remember_a_number_the_owner_already_applied_in_this_plan(self):
        self.remember_global({"set": {"max_current_days": 6}})
        m, _ = await self.plan()
        self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 6)
        m2 = await self.step(m, araw([dict(type="set_number", key="max_current_days", value=10)]), "Применить")
        self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 10)
        self.assertIn("max_current_days", self.latest()["plan"]["explicit_keys"])
        self.answers = [araw([dict(type="set_number", key="max_current_days", value=10)], scope="permanent_rule")]
        c, _ = cb(buttons(m2)["Настройки"], m2)
        await self.ctl.handle_callback(c)
        m2.text = "10 дней, запомни"
        await self.ctl.handle_message(m2)
        self.assertNotIn("Изменений в условиях нет", said(m2))
        self.assertIn("Применить и запомнить", buttons(m2), msg=said(m2))
        m3 = await self.tap(m2, "Применить и запомнить")
        await self.tap(m3, "Да, сохранить навсегда")
        mine = [exp.decode(r)["effect"] for r in self.ctl.memory.entries() if r["status"] == "confirmed"][-1]
        self.assertEqual(mine, {"set": {"max_current_days": 10}})
        self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 10)
        await self.plan()
        self.assertEqual(self.latest()["plan"]["rules"]["max_current_days"], 10)  # the newer record, not the old 6

    def test_a_repeat_of_the_same_value_without_remembering_is_still_no_change(self):
        rules = planner.default_rules()
        raw = araw([dict(type="set_number", key="max_current_days", value=rules["max_current_days"])])
        p = planner_ai.validate_proposal(raw, rules, planner_ai.catalog(SNAP), explicit=("max_current_days",))
        self.assertEqual(list(p.summary), [])

    async def test_target_days_is_accepted_when_max_days_comes_from_memory(self):
        self.remember_global({"set": {"max_days": 30}})
        m, _ = await self.plan()
        d = self.latest()
        self.assertEqual(d["plan"]["rules"]["max_days"], 30)
        self.assertEqual(d["plan"]["base_rules"]["max_days"], planner.default_rules()["max_days"])  # memory not absorbed
        m2 = await self.step(m, araw([dict(type="set_number", key="target_days", value=20)]), "Применить")
        self.assertNotIn("Недопустимые", said(m2))
        d = self.latest()
        self.assertEqual((d["plan"]["rules"]["target_days"], d["plan"]["rules"]["max_days"]), (20, 30))
        self.assertEqual(d["plan"]["base_rules"]["max_days"], planner.default_rules()["max_days"])  # still not absorbed
        self.assertTrue(d["plan"]["experience"][0]["applied"])

    def test_a_typed_command_is_judged_in_the_context_of_the_effective_rules(self):
        base = planner.default_rules()
        ctx = dict(base, max_days=30)
        with self.assertRaises(ValueError):
            planner_ui.parse_command("цель 20", base)
        new, _ = planner_ui.parse_command("цель 20", base, context=ctx)
        self.assertEqual((new["target_days"], new["max_days"]), (20, base["max_days"]))
        with self.assertRaises(ValueError):  # the context does not switch the limits off
            planner_ui.parse_command("цель 40", base, context=ctx)
        with self.assertRaises(ValueError):  # ...and the owner's own explicit maximum is never replaced by memory
            planner_ui.parse_command("цель 20", base, explicit=("max_days",), context=ctx)

    async def test_leftover_confirmed_records_stay_revocable_even_when_the_wild_is_not_in_the_plan(self):
        m, _ = await self.plan()
        i = self.ctl.memory.propose(**exp.memory_entries("product_preference", {"excluded_wild": ["W9"]}, reason="",
                                                         plan_id="a" * 12, plan_date="2026-10-30",
                                                         today=date(2026, 10, 28), expires_on="2099-01-01")[0])
        self.ctl.memory.confirm(i)
        c, mm = cb(buttons(m)["Память"], m)
        await self.ctl.handle_callback(c)
        self.assertIn(f"pl:rv:{i}", buttons(mm).values())
        c, rm = cb(f"pl:rv:{i}")
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries()[0]["status"], "revoked")

    async def test_after_failed_recalc_and_failed_revoke_the_record_can_be_revoked_from_memory(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        m, _ = await self.plan()
        self.answers = [araw([dict(type="exclude_wild", wild="W1")], scope="product_preference", expires_on="2099-01-01")]
        c, _ = cb(buttons(m)["Настройки"], m)
        await self.ctl.handle_callback(c)
        m.text = "запомни"
        await self.ctl.handle_message(m)
        c, m2 = cb(buttons(m)["Применить и запомнить"])
        with ExitStack() as st:
            st.enter_context(patch.object(self.ctl, "_revise", AsyncMock(return_value=None)))
            st.enter_context(patch.object(self.ctl.memory, "revoke", side_effect=RuntimeError("db down")))
            await self.ctl.handle_callback(c)
        rec = self.ctl.memory.entries()[0]
        self.assertEqual(rec["status"], "confirmed")
        self.assertIn("НЕ удался", said(m2))
        c, mm = cb(buttons(m)["Память"], m)
        await self.ctl.handle_callback(c)
        self.assertIn(f"pl:rv:{rec['id']}", buttons(mm).values())
        c, rm = cb(f"pl:rv:{rec['id']}")
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries()[0]["status"], "revoked")

    async def test_wild_in_catalog_but_not_in_plan_lines_stays_revocable_after_failed_recalc_and_failed_revoke(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        w3 = ["W3", "A3", "Каб", "Товар3", 5000, 200, 1, 1, 1, 1, 200, 200, 200]  # plenty of stock: no line in the plan
        self.sheets.snap["test"] = list(SNAP["test"]) + [w3]
        self.sheets.snap["packing"] = list(SNAP["packing"]) + [["W3", 1, 1, 6, 48]]
        m, _ = await self.plan()
        d0 = self.latest()
        self.assertIn("W3", planner_ai.catalog(self.sheets.snap)["wilds"].values())
        self.assertNotIn("W3", [ln["wild"] for ln in d0["plan"]["lines"]])
        self.assertNotIn("W3", {e["target"] for e in d0["plan"].get("experience") or ()})
        self.answers = [araw([dict(type="exclude_wild", wild="W3")], scope="product_preference", expires_on="2099-01-01")]
        c, _ = cb(buttons(m)["Настройки"], m)
        await self.ctl.handle_callback(c)
        m.text = "запомни"
        await self.ctl.handle_message(m)
        c, m2 = cb(buttons(m)["Применить и запомнить"])
        with ExitStack() as st:
            st.enter_context(patch.object(self.ctl, "_revise", AsyncMock(return_value=None)))  # the recalculation fails
            st.enter_context(patch.object(self.ctl.memory, "revoke", side_effect=RuntimeError("db down")))  # so does the revoke
            await self.ctl.handle_callback(c)
        rec = self.ctl.memory.entries()[0]
        self.assertEqual((rec["status"], rec["target"]), ("confirmed", "W3"))  # still in force
        text = said(m2)
        self.assertIn("НЕ удался", text)  # the owner is told the truth, not "cancelled"
        self.assertNotIn("Запомнил", text)
        self.assertEqual(self.latest()["latest_version"], d0["latest_version"])  # the plan itself is untouched
        for _ in range(2):  # reopening "Память" finds it again every time
            c, mm = cb(buttons(m)["Память"], m)
            await self.ctl.handle_callback(c)
            self.assertIn(f"pl:rv:{rec['id']}", buttons(mm).values(), msg=said(mm))
        c, rm = cb(f"pl:rv:{rec['id']}")
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries()[0]["status"], "revoked")
        self.assertIn("отозван", said(rm).lower())

    # --- the four findings of the review of 33ec8c4 ------------------------------------------------------------------
    async def test_goal_that_conflicts_with_the_remembered_max_days_is_refused_up_front_and_the_record_stays_whole(self):
        self.remember_global({"set": {"max_days": 11}, "excluded_wild": ["W1"]})
        m, _ = await self.plan()
        d0 = self.latest()
        self.assertEqual(d0["plan"]["rules"]["max_days"], 11)
        self.assertNotIn("W1", [ln["wild"] for ln in d0["plan"]["lines"]])
        self.assertEqual(d0["plan"]["base_rules"]["max_days"], planner.default_rules()["max_days"])
        # the owner's typed command: base target 12 <= base max 13 is fine alone, but the plan runs with max 11
        c, _ = cb(buttons(m)["Настройки"], m)
        await self.ctl.handle_callback(c)
        m.text = "цель 12"
        before = len(m.answer.call_args_list)
        await self.ctl.handle_message(m)
        said_now = "\n".join(str(x.args[0]) for x in m.answer.call_args_list[before:] if x.args)
        self.assertIn("Недопустимые настройки", said_now)
        self.assertNotIn("Применить", [b for x in m.answer.call_args_list[before:]
                                       for row in getattr(x.kwargs.get("reply_markup"), "inline_keyboard", []) for b in [bt.text for bt in row]])
        # the same through the model proposal
        self.answers = [araw([dict(type="set_number", key="target_days", value=12)])]
        m.text = "хочу чтобы цель была двенадцать дней"
        before = len(m.answer.call_args_list)
        await self.ctl.handle_message(m)
        said_now = "\n".join(str(x.args[0]) for x in m.answer.call_args_list[before:] if x.args)
        self.assertIn("Недопустимые настройки", said_now)
        d1 = self.latest()
        self.assertEqual((d1["id"], d1["version"]), (d0["id"], d0["version"]))  # nothing saved
        self.assertTrue(d1["plan"]["experience"][0]["applied"])  # the record is still applied as a whole, W1 included
        self.assertNotIn("W1", [ln["wild"] for ln in d1["plan"]["lines"]])
        # the limit is judged both ways: an owner's own explicit maximum is still the owner's call
        self.answers = [araw([dict(type="set_number", key="target_days", value=12), dict(type="set_number", key="max_days", value=14)])]
        m.text = "хочу цель 12 и максимум 14"
        await self.ctl.handle_message(m)
        self.assertIn("Применить", buttons(m))

    async def test_confirmed_record_expiring_before_the_plan_date_stays_visible_and_revocable_after_a_double_failure(self):
        from contextlib import ExitStack
        from unittest.mock import patch
        m, _ = await self.plan()
        d0 = self.latest()
        day = date.fromisoformat(d0["plan"]["date"])
        soon = (day - timedelta(days=1)).isoformat()  # still alive today, ends before the shipment date
        self.answers = [araw([dict(type="exclude_wild", wild="W2")], scope="product_preference", expires_on=soon)]
        c, _ = cb(buttons(m)["Настройки"], m)
        await self.ctl.handle_callback(c)
        m.text = "запомни"
        await self.ctl.handle_message(m)
        c, m2 = cb(buttons(m)["Применить и запомнить"])
        with ExitStack() as st:
            st.enter_context(patch.object(self.ctl, "_revise", AsyncMock(return_value=None)))
            st.enter_context(patch.object(self.ctl.memory, "revoke", side_effect=RuntimeError("db down")))
            await self.ctl.handle_callback(c)
        rec = self.ctl.memory.entries()[0]
        self.assertEqual((rec["status"], rec["expires_on"]), ("confirmed", soon))
        self.assertIn("НЕ удался", said(m2))
        for _ in range(2):
            c, mm = cb(buttons(m)["Память"], m)
            await self.ctl.handle_callback(c)
            self.assertIn(f"pl:rv:{rec['id']}", buttons(mm).values(), msg=said(mm))
            self.assertNotIn("действует в расчёте", [x for x in said(mm).splitlines() if "W2" in x][0])  # not for this date
        c, rm = cb(f"pl:rv:{rec['id']}")
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries()[0]["status"], "revoked")

    async def test_record_missing_from_the_saved_experience_is_not_called_working_without_a_check(self):
        m, _ = await self.plan()  # a saved plan with max_days 13 and nothing remembered
        self.assertEqual(self.latest()["plan"]["experience"], [])
        self.remember_global({"set": {"target_days": 20}, "excluded_wild": ["W2"]})  # saved later, e.g. from another plan
        c, mm = cb(buttons(m)["Память"], m)
        await self.ctl.handle_callback(c)
        row = [x for x in said(mm).splitlines() if x.startswith("• ") and "W2" in x]
        self.assertEqual(len(row), 1, msg=said(mm))
        self.assertNotIn("действует в расчёте", row[0])  # the recalculation would refuse it (20 > 13)
        self.assertIn("не применится", row[0])
        self.assertIn("target_days must not exceed max_days", row[0])
        # and a record that really would apply is described as such, apart from the saved snapshot
        self.remember_global({"excluded_wild": ["W1"]})
        c, mm = cb(buttons(m)["Память"], m)
        await self.ctl.handle_callback(c)
        ok = [x for x in said(mm).splitlines() if x.startswith("• ") and "W1" in x]
        self.assertEqual(len(ok), 1, msg=said(mm))
        self.assertIn("после «Пересчитать»", ok[0])

    async def test_memory_page_stays_under_the_telegram_limit_at_the_largest_allowed_data(self):
        n = 8  # MEM_PAGE records, each with the longest wild memory allows (MAX_TARGET = 200)
        wilds = [f"W{i}" + "x" * 197 for i in range(n)]
        self.sheets.snap["test"] = list(SNAP["test"]) + [[w, f"A{i + 9}", "Каб", "Товар" + "я" * 60, 1000, 5, 10, 10, 10, 10, 4, 4, 60]
                                                         for i, w in enumerate(wilds)]
        self.sheets.snap["packing"] = list(SNAP["packing"]) + [[w, 1, 1, 6, 48] for w in wilds]
        for w in wilds:
            self.ctl.memory.confirm(self.ctl.memory.propose(**exp.memory_entries(
                "product_preference", {"excluded_wild": [w]}, reason="", plan_id="a" * 12, plan_date="2026-10-30",
                today=date(2026, 10, 28), expires_on="2099-01-01")[0]))
        m, _ = await self.plan()
        self.assertGreaterEqual(len(self.latest()["plan"]["experience"]), 5)  # the plan shows the full five experience lines
        c, mm = cb(buttons(m)["Память"], m)
        await self.ctl.handle_callback(c)
        page = mm.answer.call_args_list[-1].args[0]  # the one message Telegram would have to accept
        self.assertLessEqual(len(page), 3900, msg=len(page))  # the hard limit is 4096; keep a margin for the worst tags
        self.assertEqual(sum(1 for v in buttons(mm).values() if v.startswith("pl:rv:")), n)  # every record keeps its button

    def test_not_applied_is_shown_apart_from_the_shortened_description_and_never_marked_as_working(self):
        eff = {"set": {"target_days": 20}, "excluded_wild": [f"Длинное название товара {i:02d}" for i in range(11)]}
        note = {"id": "x", "category": "permanent_rule", "scope": "global", "target": "", "summary": exp._summary(eff),
                "applied": False, "note": "не применено: target_days must not exceed max_days", "effect": eff}
        ok = dict(note, id="y", applied=True, note="", summary="Не брать wild: W1")
        lines = exp.experience_lines([ok, note], limit=5)
        self.assertIn("не применено: 1", lines[0])  # the header alone already says it
        bad = [x for x in lines[1:] if "не применено" in x]
        self.assertEqual(len(bad), 1)
        self.assertIn("target_days must not exceed max_days", bad[0])
        self.assertTrue(all(len(x) <= 160 for x in lines))
        self.assertEqual(lines.index(bad[0]), 1)  # the not-applied entry comes first, so a cut-off list keeps it
        only = exp.experience_lines([ok, note], limit=0)
        self.assertIn("не применено: 1", only[0])

    async def test_memory_view_marks_a_record_not_applied_in_this_plan_instead_of_working(self):
        self.remember_global({"set": {"target_days": 20}, "excluded_wild": ["W1"]})
        self.remember_global({"set": {"max_days": 11}})
        m, _ = await self.plan()
        d = self.latest()
        self.assertEqual([e["applied"] for e in d["plan"]["experience"]], [False, True])
        c, mm = cb(buttons(m)["Память"], m)
        await self.ctl.handle_callback(c)
        rows = [x for x in said(mm).splitlines() if x.startswith("• ") and "Постоянное правило" in x]
        self.assertEqual(len(rows), 2, msg=said(mm))
        working = [x for x in rows if x.startswith("• действует в расчёте")]
        idle = [x for x in rows if x.startswith("• не применено")]
        self.assertEqual((len(working), len(idle)), (1, 1), msg=said(mm))
        self.assertIn("max_days", working[0].replace("Максимум", "max_days"))
        self.assertIn("pl:rv:" + [r["id"] for r in self.ctl.memory.entries()][0], buttons(mm).values())  # still revocable

    async def test_not_applied_is_visible_on_the_plan_screen_itself(self):
        self.remember_global({"set": {"target_days": 20}})
        self.remember_global({"set": {"max_days": 11}})
        m, _ = await self.plan()
        self.assertIn("не применено: 1", said(m))

    async def test_revoking_the_number_a_proposal_depended_on_fails_visibly_and_changes_nothing(self):
        self.remember_global({"set": {"max_days": 30}})
        m, _ = await self.plan()
        m2 = await self.step(m, araw([dict(type="set_number", key="target_days", value=20)]), "Применить")
        before = self.latest()
        rid = self.ctl.memory.entries()[0]["id"]
        c, _ = cb(f"pl:rv:{rid}")
        await self.ctl.handle_callback(c)
        c, m3 = cb(self.latest_keys(m2))
        await self.ctl.handle_callback(c)
        self.assertIn("Не удалось построить план", said(m3))  # the limit is not switched off; the owner is told
        self.assertEqual((self.latest()["id"], self.latest()["version"]), (before["id"], before["version"]))

    def test_equal_created_at_keeps_the_store_order_so_the_newer_record_wins(self):
        from unittest.mock import patch
        import planner_memory
        for order in ((6, 10), (10, 6)):
            mem = MemoryStore(os.path.join(tempfile.mkdtemp(), "m.db"))
            with patch.object(planner_memory, "_now", return_value="2026-10-30T10:00:00.000000Z"):
                for v in order:
                    e = exp.memory_entries("permanent_rule", {"set": {"max_current_days": v}}, reason="", plan_id="a" * 12,
                                           plan_date="2026-10-30", today=date(2026, 10, 28))[0]
                    mem.confirm(mem.propose(**e))
            recs = mem.entries()
            self.assertEqual(len({r["created_at"] for r in recs}), 1)
            sel = exp.select(recs, plan_id=None, on_date="2026-10-30", wilds=set())
            rules, _ = exp.merge(planner.validate_rules({}), sel)
            self.assertEqual(rules["max_current_days"], order[-1])  # the later-created record, every time
            rules2, _ = exp.merge(planner.validate_rules({}), exp.select(list(recs), plan_id=None, on_date="2026-10-30",
                                                                         wilds=set()))
            self.assertEqual(rules, rules2)

    async def test_revoke_button_reports_revoked_only_after_the_status_changed(self):
        from unittest.mock import patch
        self.remember_global({"excluded_wild": ["W1"]})
        rid = self.ctl.memory.entries()[0]["id"]
        with patch.object(self.ctl.memory, "revoke", side_effect=RuntimeError("db down")):
            c, m = cb(f"pl:rv:{rid}")
            await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries()[0]["status"], "confirmed")
        self.assertNotIn("Запись отозвана", said(m))
        with patch.object(self.ctl.memory, "revoke", return_value="revoked"):  # claims success, changes nothing
            c, m = cb(f"pl:rv:{rid}")
            await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries()[0]["status"], "confirmed")
        self.assertNotIn("Запись отозвана", said(m))
        c, m = cb(f"pl:rv:{rid}")
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries()[0]["status"], "revoked")
        self.assertIn("Запись отозвана", said(m))

    async def test_expired_quantity_change_returns_to_the_owners_value_and_keeps_independent_settings(self):
        await self.mixed(expires_on="2020-01-01")  # term already over: nothing of the exception may remain
        d = self.latest()
        self.assertEqual(self.qty(d, "A1"), [6])
        self.assertIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])
        self.assertEqual(self.fx(d["plan"]["rules"]), [("A1", 6)])
        self.assertEqual(d["plan"]["rules"]["max_current_days"], 8)
        self.assertEqual(d["plan"]["experience"], [])

    async def test_owners_later_change_of_the_same_line_beats_the_remembered_value(self):
        m3 = await self.mixed()
        add = dict(type="add_fixed", article="A1", cabinet="Каб", quantity=18, reason="новое поручение")
        await self.step(m3, araw([add]), "Применить")
        d = self.latest()
        self.assertEqual(self.qty(d, "A1"), [18])  # this plan's own, newer decision wins over the remembered one
        self.assertNotIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])  # the unrelated exclusion still applies
        self.ctl.memory.revoke(self.ctl.memory.entries()[0]["id"])
        c, _ = cb(self.latest_keys(m3))
        await self.ctl.handle_callback(c)
        d = self.latest()
        self.assertEqual(self.qty(d, "A1"), [18])
        self.assertIn("W2", [ln["wild"] for ln in d["plan"]["lines"]])

    def seed_long_rules(self, n=5, wilds=12, width=70):
        for r in range(n):  # n permanent rules x `wilds` long-named wilds: their summaries alone exceed 4096 chars
            self.ctl.memory.confirm(self.ctl.memory.propose(**exp.memory_entries(
                "permanent_rule", {"excluded_wild": [f"LONG{r:02d}{w:02d}".ljust(width, "x") for w in range(wilds)]},
                reason="", plan_id="a" * 12, plan_date="2026-10-30", today=date(2026, 10, 28))[0]))

    async def test_preview_with_five_long_rules_fits_telegram_and_keeps_the_plan_data(self):
        self.seed_long_rules()
        m, b = await self.plan()
        d = self.latest()
        self.assertEqual(len(d["plan"]["experience"]), 5)
        self.assertGreater(sum(len(e["summary"]) for e in d["plan"]["experience"]), 4096)  # the reproduced overflow
        text = planner_ui.preview_text(d)
        self.assertLessEqual(len(text), 4096)
        for must in ("Отгрузка", "Строк:", "Правила:", "Учтён опыт"):
            self.assertIn(must, text)
        for c in m.answer.call_args_list:
            self.assertLessEqual(len(c.args[0]), 4096)
        self.assertIn("Память", b)

    async def test_confirmation_path_fits_and_a_send_failure_keeps_a_revocable_record(self):
        self.seed_long_rules(wilds=11)
        m, b = await self.plan()
        m2 = await self.propose(araw([dict(type="exclude_wild", wild="W1")], scope="permanent_rule"))
        c, _ = cb(buttons(m2)["Применить и запомнить"], m2)
        await self.ctl.handle_callback(c)
        c, _ = cb(buttons(m2)["Да, сохранить навсегда"], m2)
        real = m2.answer

        failed = []

        async def flaky(text, *a, **k):
            if text.startswith("ТЕСТОВЫЙ план"):
                failed.append(text)
                raise RuntimeError("telegram: message is too long")
            return await real(text, *a, **k)
        m2.answer = AsyncMock(side_effect=flaky)
        await self.ctl.handle_callback(c)  # the preview send fails AFTER the record is confirmed and the plan stored
        self.assertEqual(len(failed), 1)  # the send really failed
        self.assertIn("сохранена, но показать её не удалось", said(m2))  # and the owner is told where to look
        confirmed = [r for r in self.ctl.memory.entries() if r["status"] == "confirmed"]
        self.assertEqual(len(confirmed), 6)
        d = self.latest()
        self.assertEqual(d["latest_version"], 2)
        self.assertEqual(len(d["plan"]["experience"]), 6)
        m2.answer = real
        c, mm = cb(f"pl:mem:{d['id']}:{d['latest_version']}")
        await self.ctl.handle_callback(c)
        for call in mm.answer.call_args_list:
            self.assertLessEqual(len(call.args[0]), 4096)
        new_id = max(confirmed, key=lambda r: r["created_at"])["id"]
        self.assertIn(f"pl:rv:{new_id}", buttons(mm).values())  # revocable after reopening memory
        c, _ = cb(f"pl:rv:{new_id}")
        await self.ctl.handle_callback(c)
        self.assertEqual(self.ctl.memory.entries()[-1]["status"], "revoked")

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
