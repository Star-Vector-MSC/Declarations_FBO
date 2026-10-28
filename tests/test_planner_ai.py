import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import planner
import planner_ai as ai

TH = (["wild", "Артикул", "Название магазина", "Название товара", "Свободный остаток сервис", "Остаток на (дней)"]
      + ["средние продажи " + x for x in ("3 дня", "7 дней", "14 дней", "30 дней")]
      + ["Прогнозные остатки в днях"] * 2 + ["Остаток на дней по вилду"])
ROW = [1000, 5, 10, 10, 10, 10, 4, 4, 60]
SNAP = {"test": [TH, ["W1", "A1", "Каб", "Свечи"] + ROW, ["W2", "A2", "Каб2", "Мыло"] + ROW,
                 ["W2", "A3", "Каб", "Мыло"] + ROW, ["W9", "A9", "Каб", "Парфюм для дома"] + ROW]}


def act(type_, **kw):
    base = dict(type=type_, key=None, value=None, wild=None, article=None, cabinet=None, quantity=None, reason=None)
    return dict(base, **kw)


def raw(actions=(), scope="this_plan", questions=(), understood="Понял"):
    return dict(understood=understood, scope=scope, actions=list(actions), questions=list(questions))


def prop(r, rules=None):
    return ai.validate_proposal(r, rules or planner.default_rules(), ai.catalog(SNAP))


class ValidateTests(unittest.TestCase):
    def test_valid_actions_build_validated_rules_and_diff(self):
        p = prop(raw([act("set_capacity", value=20), act("exclude_wild", wild="w1"),
                      act("exclude_cabinet", cabinet="каб2"), act("set_number", key="target_days", value=9),
                      act("add_fixed", article="a3", cabinet="КАБ", quantity=12, reason="акция")]))
        r = p.rules
        self.assertEqual((r["capacity"], r["excluded_wild"], r["excluded_cabinets"], r["target_days"]),
                         (20, ["W1"], ["Каб2"], 9))  # canonical spelling from the table, not the model's
        self.assertEqual(r["fixed"], [dict(article="A3", cabinet="Каб", quantity=12, reason="акция")])
        self.assertEqual((r["fixed"][0]["article"], r["fixed"][0]["cabinet"]), ("A3", "Каб"))
        self.assertEqual(p.questions, [])
        self.assertTrue(any("Машина" in x for x in p.summary))
        self.assertEqual(planner.validate_rules(r), r)

    def test_unknown_items_become_questions_not_guesses(self):
        p = prop(raw([act("exclude_wild", wild="W77"), act("exclude_cabinet", cabinet="Нет такого"),
                      act("add_fixed", article="A1", cabinet="Каб2", quantity=5, reason="x")]))
        self.assertEqual(len(p.questions), 3)
        self.assertEqual(p.summary, [])
        self.assertEqual(p.rules, planner.default_rules())

    def test_missing_quantity_or_reason_asks(self):
        p = prop(raw([act("add_fixed", article="A1", cabinet="Каб", quantity=None, reason="x"),
                      act("add_fixed", article="A2", cabinet="Каб2", quantity=6, reason=None)]))
        self.assertEqual(len(p.questions), 2)
        self.assertEqual(p.rules["fixed"], [])

    def test_banned_group_never_added_even_if_requested(self):
        with self.assertRaises(ai.AIRejected):
            prop(raw([act("add_fixed", article="A9", cabinet="Каб", quantity=10, reason="надо")]))

    def test_unclear_scope_asks_about_scope(self):
        p = prop(raw([act("exclude_wild", wild="W1")], scope="unclear"))
        self.assertTrue(any("распространить" in q for q in p.questions))

    def test_include_and_remove_revert_existing_entries(self):
        rules = planner.validate_rules({"excluded_wild": ["W1"], "excluded_cabinets": ["Каб2"],
                                        "fixed": [dict(article="A1", cabinet="Каб", quantity=6, reason="r")]})
        p = prop(raw([act("include_wild", wild="W1"), act("include_cabinet", cabinet="Каб2"),
                      act("remove_fixed", article="A1", cabinet="Каб")]), rules)
        self.assertEqual((p.rules["excluded_wild"], p.rules["excluded_cabinets"], p.rules["fixed"]), ([], [], []))

    def test_malformed_model_output_is_rejected_whole(self):
        bad = [None, [], "x", raw()["understood"], dict(raw(), extra=1), dict(raw(), scope="forever"),
               dict(raw(), understood=""), dict(raw(), actions="x"), dict(raw(), questions=["q"] * 6),
               raw([act("delete_everything")]), raw([dict(act("set_capacity", value=20), extra=1)]),
               raw([act("set_capacity", value=25)]), raw([act("set_capacity", value=True)]),
               raw([act("set_number", key="capacity", value=5)]), raw([act("set_number", key="min_sales", value=-1)]),
               raw([act("set_number", key="min_sales", value=1001)]), raw([act("set_number", key="min_sales", value=True)]),
               raw([act("set_number", key="target_days", value=20)]),  # target > max_days: rules validation refuses
               raw([act("set_capacity", value=20)] * 13)]
        for r in bad:
            with self.subTest(r=r), self.assertRaises(ai.AIRejected):
                prop(r)

    def test_nothing_is_applied_by_validation_alone(self):
        rules = planner.default_rules()
        before = json.dumps(rules, sort_keys=True)
        prop(raw([act("exclude_wild", wild="W1")]), rules)
        self.assertEqual(json.dumps(rules, sort_keys=True), before)


class PromptTests(unittest.TestCase):
    def test_prompt_has_identities_but_no_numbers_and_frames_data(self):
        cat = ai.catalog(SNAP)
        p = ai.build_prompt("исключи мыло", planner.default_rules(), cat, [])
        self.assertIn("Мыло", p)
        self.assertNotIn("1000", p)  # stock is not sent
        self.assertIn("DATA", p)
        self.assertIn("исключи мыло", p)

    def test_plan_lines_are_always_in_focus(self):
        cat = ai.catalog(SNAP)
        p = ai.build_prompt("что-то", planner.default_rules(), cat, [dict(article="A1", cabinet="Каб", quantity=42)])
        self.assertIn('"in_plan_quantity": 42', p)

    def test_interpret_limits_and_roundtrip(self):
        with self.assertRaises(ai.AIRejected):
            ai.interpret("", planner.default_rules(), SNAP, call=lambda p: raw())
        seen = []
        out = ai.interpret("  Машина   20 ", planner.default_rules(), SNAP,
                           call=lambda p: seen.append(p) or raw([act("set_capacity", value=20)]))
        self.assertEqual(out.rules["capacity"], 20)
        self.assertIn("Машина 20", seen[0])


class RunnerTests(unittest.TestCase):
    def fake(self, rc=0, stderr="", answer=None, record=None, exc=None):
        def runner(cmd, **kw):
            if record is not None:
                record.update(cmd=cmd, kw=kw)
            if exc:
                raise exc
            out = cmd[cmd.index("--output-last-message") + 1]
            if answer is not None:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(answer)
            return SimpleNamespace(returncode=rc, stdout="", stderr=stderr)
        return runner

    def test_success_uses_subscription_cli_read_only_without_api_keys(self):
        rec = {}
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "CODEX_API_KEY": "secret2", "KEEP": "1"}):
            res = ai.run_codex("hi", runner=self.fake(answer=json.dumps(raw()), record=rec), finder=lambda: ["codex"])
        self.assertEqual(res["scope"], "this_plan")
        cmd, env = rec["cmd"], rec["kw"]["env"]
        self.assertEqual(rec["kw"]["input"], "hi")
        for needed in ("--sandbox", "read-only", "--ephemeral", "--output-schema", "gpt-6.1-sol"):
            self.assertIn(needed, cmd)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", cmd)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertEqual(env["KEEP"], "1")

    def test_exact_unavailable_reasons(self):
        cases = [(dict(rc=1, stderr="You've hit your usage limit"), "лимит подписки"),
                 (dict(rc=1, stderr="Not logged in"), "codex login"),
                 (dict(rc=1, stderr="network unreachable"), "нет связи"),
                 (dict(rc=2, stderr="boom"), "ошибкой"),
                 (dict(exc=subprocess.TimeoutExpired("codex", 1)), "не ответил"),
                 (dict(exc=FileNotFoundError()), "не запустился")]
        for kw, word in cases:
            with self.subTest(word), self.assertRaises(ai.AIUnavailable) as cm:
                ai.run_codex("x", runner=self.fake(**kw), finder=lambda: ["codex"])
            self.assertIn(word, str(cm.exception))
        with self.assertRaises(ai.AIUnavailable) as cm:
            ai.run_codex("x", finder=lambda: None)
        self.assertIn("не найден", str(cm.exception))

    def test_unreadable_answer_is_rejected(self):
        for ans in (None, "not json"):
            with self.assertRaises(ai.AIRejected):
                ai.run_codex("x", runner=self.fake(answer=ans), finder=lambda: ["codex"])


if __name__ == "__main__":
    unittest.main()
