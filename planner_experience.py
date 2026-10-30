"""Experience of the FBO planner: owner-confirmed corrections stored in the existing SQLite decision memory and applied
to later plans. This is a memory of decisions, not model training. Stdlib + planner only; no IO of its own.

A structured entry is a normal planner_memory entry (kind "rule") whose text ends with one machine line
`FBO-EFFECT/1 {json}`. Only confirmed, unexpired entries of the right scope are applied; "case" entries (examples of past
shipments) and entries without a valid effect line never change a calculation. The owner's explicit settings of the
current plan beat remembered numeric rules."""
import json
import re
from datetime import date, timedelta

import planner

PREFIX = "FBO-EFFECT/1 "
CATEGORIES = {  # category -> (memory scope, label)
    "once_exception": ("plan", "разовое исключение"),
    "product_preference": ("product", "предпочтение по товару"),
    "permanent_rule": ("global", "постоянное правило"),
}
PREFERENCE_DAYS = 60  # only a proposal shown to the owner, never stored unseen
TERM_CHOICES = (30, 60, 90)
_LINE = re.compile(r"\n" + re.escape(PREFIX) + r"(\{.*\})\s*$", re.S)
_NUM = NUMERIC = ("min_sales", "max_current_days", "min_wild_days", "target_days", "max_days")
_EFFECT_KEYS = {"excluded_wild", "excluded_cabinets", "set", "fixed"}


def effect_between(old, new):
    """(additive effect, has_removals) from base rules `old` to proposed rules `new`."""
    eff = {}
    add_w = [x for x in new["excluded_wild"] if x not in old["excluded_wild"]]
    add_c = [x for x in new["excluded_cabinets"] if x not in old["excluded_cabinets"]]
    sets = {k: new[k] for k in _NUM + ("capacity",) if new[k] != old[k]}
    key = lambda f: (f["article"], f["cabinet"])  # noqa: E731
    add_f = [f for f in new["fixed"] if key(f) not in {key(g) for g in old["fixed"]}]
    for name, val in (("excluded_wild", add_w), ("excluded_cabinets", add_c), ("set", sets), ("fixed", add_f)):
        if val:
            eff[name] = val
    removed = (any(x not in new["excluded_wild"] for x in old["excluded_wild"])
               or any(x not in new["excluded_cabinets"] for x in old["excluded_cabinets"])
               or any(key(f) not in {key(g) for g in new["fixed"]} for f in old["fixed"]))
    return eff, removed


def persistable(category, effect, removed):
    """Can this change be remembered as `category`? -> (ok, owner-facing reason when not)."""
    if category not in CATEGORIES:
        return False, "эту область запомнить нельзя"
    if removed:
        return False, "снятие исключений и обязательных строк в память не записывается"
    if not effect:
        return False, "нечего запоминать"
    if category == "once_exception":
        return True, ""
    if category == "product_preference":
        if set(effect) != {"excluded_wild"}:
            return False, "предпочтение по товару я запоминаю только как «не брать этот wild»"
        return True, ""
    if "fixed" in effect:
        return False, "обязательные строки не могут быть постоянным правилом"
    if "capacity" in effect.get("set", {}):
        return False, "машину вы выбираете при создании каждого плана, в память она не записывается"
    return True, ""


def _summary(effect):
    parts = []
    if effect.get("excluded_wild"):
        parts.append("не брать wild " + ", ".join(effect["excluded_wild"]))
    if effect.get("excluded_cabinets"):
        parts.append("не брать кабинет " + ", ".join(effect["excluded_cabinets"]))
    for k, v in (effect.get("set") or {}).items():
        parts.append(f"{k}={v}")
    for f in effect.get("fixed", ()):
        parts.append(f"обязательно {f['article']}/{f['cabinet']} × {f['quantity']}")
    return "; ".join(parts)


def memory_entries(category, effect, *, reason, plan_id, plan_date, today, expires_on=None):
    """Memory.propose kwargs, one dict per entry (a product preference is one entry per wild)."""
    scope = CATEGORIES[category][0]
    base = dict(reason=(reason or "").strip()[:1000], kind="rule")
    if category == "once_exception":
        return [dict(base, scope="plan", target=plan_id, expires_on=expires_on or plan_date, text=_text(category, effect))]
    if category == "product_preference":
        exp = expires_on
        if not exp:
            raise ValueError("Для предпочтения по товару нужен срок, выбранный владельцем.")
        return [dict(base, scope="product", target=w, expires_on=exp,
                     text=_text(category, {"excluded_wild": [w]})) for w in effect["excluded_wild"]]
    return [dict(base, scope="global", target="", expires_on=expires_on, text=_text(category, effect))]


def _text(category, effect):
    return f"{CATEGORIES[category][1].capitalize()}: {_summary(effect)}\n{PREFIX}" + json.dumps(
        {"category": category, "effect": effect}, ensure_ascii=False, sort_keys=True)


def example_entry(owner_text, plan_id, plan_date):
    """A past-shipment example: kind "case", never a rule, never applied to a calculation."""
    return dict(text="Пример от владельца: " + " ".join(owner_text.split())[:700], reason="", scope="plan",
                target=plan_id, expires_on=None, kind="case")


def decode(rec):
    """Structured effect of a memory record, or None (case entries and malformed lines are inert)."""
    if rec.get("kind") != "rule":
        return None
    m = _LINE.search(rec.get("text") or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
        cat, eff = data["category"], data["effect"]
        if (set(data) != {"category", "effect"} or cat not in CATEGORIES or not isinstance(eff, dict)
                or not eff or not set(eff) <= _EFFECT_KEYS or CATEGORIES[cat][0] != rec["scope"]):
            return None
        for k in ("excluded_wild", "excluded_cabinets"):
            if k in eff and not (isinstance(eff[k], list) and all(isinstance(x, str) and x.strip() for x in eff[k])):
                return None
        if "set" in eff and not (isinstance(eff["set"], dict) and set(eff["set"]) <= set(_NUM) | {"capacity"}):
            return None
        if "fixed" in eff and not isinstance(eff["fixed"], list):
            return None
        planner.validate_rules({**planner.default_rules(), **{k: eff[k] for k in eff if k != "set"}, **eff.get("set", {})})
    except (ValueError, KeyError, TypeError):
        return None
    return {"category": cat, "effect": eff}


def select(entries, *, plan_id, on_date, wilds):
    """Confirmed, unexpired, correctly scoped structured records for this plan; stable order."""
    out = []
    for r in entries:
        if r["status"] != "confirmed" or (r["expires_on"] is not None and r["expires_on"] < on_date):
            continue
        if not (r["scope"] == "global" or (r["scope"] == "product" and r["target"] in wilds)
                or (r["scope"] == "plan" and plan_id is not None and r["target"] == plan_id)):
            continue
        d = decode(r)
        if d:
            out.append((r, d))
    return sorted(out, key=lambda x: x[0]["created_at"])


def merge(base, selected, explicit=()):
    """Effective rules = owner's base rules + remembered effects. -> (rules, experience list for display/storage).
    `explicit` = keys the owner set in THIS plan (even to a value equal to the default): remembered numbers never
    override them."""
    base = planner.validate_rules(base)
    explicit = set(explicit)
    rules, notes, defaults = base, [], planner.default_rules()
    for r, d in selected:
        eff = d["effect"]
        item = {"id": r["id"], "category": d["category"], "scope": r["scope"], "target": r["target"],
                                  "summary": _summary(eff), "applied": True, "note": "", "effect": eff}
        cand = json.loads(json.dumps(rules))
        for k in ("excluded_wild", "excluded_cabinets"):
            cand[k] = cand[k] + [x for x in eff.get(k, ()) if x not in cand[k]]
        kept = {}
        for k, v in (eff.get("set") or {}).items():
            if k in explicit or base[k] != defaults[k]:  # the owner's explicit value for this plan wins
                kept[k] = base[k]
            else:
                cand[k] = v
        key = lambda f: (f["article"], f["cabinet"])  # noqa: E731
        cand["fixed"] = cand["fixed"] + [f for f in eff.get("fixed", ()) if key(f) not in {key(g) for g in cand["fixed"]}]
        try:
            rules = planner.validate_rules(cand)
        except ValueError as e:
            item.update(applied=False, note="не применено: " + str(e)[:120])
        else:
            if kept:
                item["note"] = "ваши настройки этого плана важнее: " + ", ".join(kept)
        notes.append(item)
    return rules, notes


def experience_lines(experience, limit=5):
    """Owner-facing lines for the plan preview."""
    if not experience:
        return []
    labels = {c: v[1] for c, v in CATEGORIES.items()}
    out = [f"Учтён опыт ({sum(1 for e in experience if e['applied'])} из {len(experience)}):"]
    for e in experience[:limit]:
        out.append(f"• {labels[e['category']]}: {e['summary']}" + (f" ({e['note']})" if e["note"] else ""))
    if len(experience) > limit:
        out.append(f"… ещё {len(experience) - limit} в «Память»")
    return out
