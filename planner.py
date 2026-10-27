"""Pure FBO shipment plan calculation. Stdlib only; no IO, no global mutable state."""
import math
import re
from datetime import date
from fractions import Fraction

_DEFAULTS = (("min_sales", 8), ("max_current_days", 10), ("min_wild_days", 30), ("target_days", 10),
             ("max_days", 13), ("capacity", 33), ("excluded_wild", ()), ("excluded_cabinets", ()), ("fixed", ()))
_NUMS = ("min_sales", "max_current_days", "min_wild_days", "target_days", "max_days")
_POSITIVE = ("min_sales", "target_days", "max_days")
_FIXED_KEYS = frozenset(("article", "cabinet", "quantity", "reason"))
_S = "средние продажи "
# key -> (normalized header, occurrence used, exact occurrence count required)
_TEST = {"wild": ("wild", 0, 1), "article": ("артикул", 0, 1), "cabinet": ("название магазина", 0, 1),
         "product": ("название товара", 0, 1), "stock": ("свободный остаток сервис", 0, 1),
         "cur": ("остаток на (дней)", 0, 1), "s3": (_S + "3 дня", 0, 1), "s7": (_S + "7 дней", 0, 1),
         "s14": (_S + "14 дней", 0, 1), "s30": (_S + "30 дней", 0, 1),
         "forecast": ("прогнозные остатки в днях", 0, 2), "wild_days": ("остаток на дней по вилду", 0, 1)}
_PACK = {"wild": ("wild", 0, 1), "box": ("шт в коробе", 1, 2), "pallet": ("штук на палете", 1, 2)}
_METRICS = ("cur", "s3", "s7", "s14", "s30", "forecast", "wild_days")
_NUM_RE = re.compile(r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)")
# Permanent FBO safeguards (not configurable, not overridable by thresholds or fixed entries).
_BAN_EXACT = "парфюм для дома"
_BAN_PREFIX = "газовые горелки"


def default_rules():
    return {k: list(v) if isinstance(v, tuple) else v for k, v in _DEFAULTS}


def _norm(v):
    return re.sub(r"\s+", " ", str(v).casefold().replace("ё", "е")).strip()


def _blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def _num(v):
    """Exact Fraction for finite numbers / plain localized numeric strings; None otherwise (never 0)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return Fraction(v)
    if isinstance(v, float):
        return Fraction(repr(v)) if math.isfinite(v) else None
    if isinstance(v, str):
        s = re.sub(r"\s", "", v)
        if _NUM_RE.fullmatch(s):
            return Fraction(s.replace(",", "."))
    return None


def _ident(v):
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, int) and not isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return str(int(v))
    return None


def _banned(product):
    """Return the permanently FBO-banned group matched by a product group label, else None."""
    if not isinstance(product, str):
        return None
    p = _norm(product)
    if p == _BAN_EXACT:
        return _BAN_EXACT
    if p == _BAN_PREFIX or p.startswith(_BAN_PREFIX + " "):
        return _BAN_PREFIX
    return None


def _date(v, name):
    if not isinstance(v, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError(f"{name} must be an ISO date YYYY-MM-DD")
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"{name} is not a valid date: {v}") from None


def _f(x):
    return None if x is None else round(float(x), 6)


def validate_rules(rules=None):
    """Return a normalized independent copy of rules merged over defaults; raise ValueError if invalid."""
    if rules is None:
        rules = {}
    if not isinstance(rules, dict):
        raise ValueError("rules must be a dict")
    unknown = sorted(str(k) for k in rules if k not in dict(_DEFAULTS))
    if unknown:
        raise ValueError(f"unknown rule keys: {unknown}")
    out = default_rules()
    out.update(rules)
    for k in _NUMS:
        v = out[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{k} must be a finite number")
        if v < 0 or (k in _POSITIVE and v <= 0):
            raise ValueError(f"{k} must be {'positive' if k in _POSITIVE else 'non-negative'}")
    cap = out["capacity"]
    if isinstance(cap, bool) or not isinstance(cap, int) or cap not in (20, 33):
        raise ValueError("capacity must be integer 20 or 33")
    if out["target_days"] > out["max_days"]:
        raise ValueError("target_days must not exceed max_days")
    for k in ("excluded_wild", "excluded_cabinets"):
        v = out[k]
        if not isinstance(v, (list, tuple)) or not all(isinstance(x, str) and x.strip() for x in v):
            raise ValueError(f"{k} must be a list of non-empty strings")
        out[k] = [x.strip() for x in v]
    if not isinstance(out["fixed"], (list, tuple)):
        raise ValueError("fixed must be a list")
    fixed, seen = [], set()
    for f in out["fixed"]:
        if not isinstance(f, dict) or set(f) != _FIXED_KEYS:
            raise ValueError("fixed entry must be a dict with exactly article, cabinet, quantity, reason")
        a, c, q, why = f["article"], f["cabinet"], f["quantity"], f["reason"]
        if not all(isinstance(x, str) and x.strip() for x in (a, c, why)):
            raise ValueError("fixed article, cabinet and reason must be non-empty strings")
        if isinstance(q, bool) or not isinstance(q, int) or q <= 0:
            raise ValueError("fixed quantity must be a positive integer")
        key = (a.strip(), c.strip())
        if key in seen:
            raise ValueError(f"duplicate fixed entry {key[0]}/{key[1]}")
        seen.add(key)
        fixed.append({"article": key[0], "cabinet": key[1], "quantity": q, "reason": why.strip()})
    out["fixed"] = fixed
    return out


def _table(rows, spec, name):
    if not isinstance(rows, (list, tuple)) or not rows or not isinstance(rows[0], (list, tuple)):
        raise ValueError(f"{name}: expected a list of rows with a header row first")
    pos = {}
    for i, h in enumerate(rows[0]):
        if not _blank(h):
            pos.setdefault(_norm(h), []).append(i)
    idx = {}
    for key, (h, occ, cnt) in spec.items():
        found = pos.get(h, [])
        if len(found) != cnt:
            raise ValueError(f"{name}: header '{h}' must occur exactly {cnt} time(s), found {len(found)}")
        idx[key] = found[occ]
    out = []
    for n, row in enumerate(rows[1:], 2):
        if not isinstance(row, (list, tuple)):
            raise ValueError(f"{name}: row {n} is not a list")
        if not all(_blank(c) for c in row):
            out.append((n, {k: row[i] if i < len(row) else None for k, i in idx.items()}))
    return out


def build_plan(snapshot, rules, shipment_date, warehouse, arrival_date=None):
    r = validate_rules(rules)
    sd = _date(shipment_date, "shipment_date")
    ad = None if arrival_date is None else _date(arrival_date, "arrival_date")
    if ad is not None and ad < sd:
        raise ValueError("arrival_date must not be before shipment_date")
    if not isinstance(warehouse, str) or not warehouse.strip():
        raise ValueError("warehouse must be a non-empty string")
    if not isinstance(snapshot, dict):
        raise ValueError("snapshot must be a dict")
    meta = {}
    for k in ("source_hash", "captured_at"):
        if snapshot.get(k) is not None and not isinstance(snapshot[k], str):
            raise ValueError(f"snapshot {k} must be a string or None")
        meta[k] = snapshot.get(k)

    warn, src, dup = [], {}, set()
    # Per physical wild, from EVERY source row carrying that wild (incl. skipped/duplicate rows): conservative.
    stocks, badstock, banned = {}, set(), {}
    for n, c in _table(snapshot.get("test"), _TEST, "test"):
        w, a, cab = (_ident(c[k]) for k in ("wild", "article", "cabinet"))
        if w:
            st = _num(c["stock"])
            if st is None or st < 0:
                badstock.add(w)
            else:
                stocks.setdefault(w, set()).add(st)
            g = _banned(c["product"])
            if g and w not in banned:
                banned[w] = g
        if not (w and a and cab):
            warn.append(f"test row {n}: missing or invalid wild/article/cabinet; skipped")
            continue
        if (a, cab) in src or (a, cab) in dup:
            dup.add((a, cab))
            src.pop((a, cab), None)
            warn.append(f"test row {n}: duplicate source identity {a}/{cab}; all its rows skipped")
            continue
        rec = {k: _num(c[k]) for k in ("stock",) + _METRICS}
        rec.update(wild=w, article=a, cabinet=cab, row=n, product=_ident(c["product"]))
        src[(a, cab)] = rec

    packs, bad, budget, pack = {}, {}, {}, {}
    for n, c in _table(snapshot.get("packing"), _PACK, "packing"):
        w = _ident(c["wild"])
        if not w:
            warn.append(f"packing row {n}: missing wild; skipped")
            continue
        if _blank(c["box"]) and _blank(c["pallet"]):
            continue  # fully blank duplicate record
        b, p = _num(c["box"]), _num(c["pallet"])
        ok = b is not None and p is not None and b > 0 and p > 0 and b.denominator == p.denominator == 1
        packs.setdefault(w, set()).add((int(b), int(p)) if ok else None)
    for w in sorted({x["wild"] for x in src.values()}):
        s, pk = stocks.get(w, set()), packs.get(w, set())
        if w in banned:
            bad[w] = f"banned product group '{banned[w]}' is never planned for FBO"
        elif w in badstock:
            bad[w] = "invalid or missing free stock value in at least one source row"
        elif len(s) != 1:
            bad[w] = "conflicting free stock budgets" if s else "missing free stock budget"
        elif len(pk) != 1 or None in pk:
            bad[w] = "invalid or conflicting mono packing" if pk else "missing mono packing"
        else:
            budget[w], pack[w] = max(0, math.floor(next(iter(s)))), next(iter(pk))
    warn.extend(f"wild {w}: {bad[w]}; blocked" for w in sorted(bad))

    ex_w = {_norm(x) for x in r["excluded_wild"]}
    ex_c = {_norm(x) for x in r["excluded_cabinets"]}
    cap, lines, used, total = r["capacity"], [], {}, Fraction(0)

    def emit(x, q, reason, fixed):
        nonlocal total
        box, pal = pack[x["wild"]]
        s7, f = x["s7"], x["forecast"]
        after = f + Fraction(q) / s7 if f is not None and s7 is not None and s7 > 0 else None
        used[x["wild"]] = used.get(x["wild"], 0) + q
        total += Fraction(q, pal)
        lines.append({"wild": x["wild"], "article": x["article"], "cabinet": x["cabinet"], "product": x["product"],
                      "quantity": q, "box_qty": box, "pallet_qty": pal, "pallets": _f(Fraction(q, pal)),
                      "forecast_before": _f(f), "forecast_after": _f(after), "sales7": _f(s7),
                      "reason": reason, "fixed": fixed})

    for fx in r["fixed"]:
        key, q = (fx["article"], fx["cabinet"]), fx["quantity"]
        lab = f"fixed {key[0]}/{key[1]}"
        if key in dup:
            raise ValueError(f"{lab}: ambiguous duplicate source rows")
        x = src.get(key)
        if x is None:
            raise ValueError(f"{lab}: no exact source row")
        w = x["wild"]
        if w in banned:
            raise ValueError(f"{lab}: wild {w} is permanently banned for FBO (group '{banned[w]}'); "
                             "fixed entries cannot override this")
        if _norm(w) in ex_w or _norm(key[1]) in ex_c:
            raise ValueError(f"{lab}: wild or cabinet is excluded by rules")
        if w in bad:
            raise ValueError(f"{lab}: wild {w} blocked: {bad[w]}")
        box, pal = pack[w]
        if q % box:
            raise ValueError(f"{lab}: quantity {q} is not a multiple of box {box}")
        if used.get(w, 0) + q > budget[w]:
            raise ValueError(f"{lab}: quantity exceeds remaining free stock budget of wild {w}")
        if total + Fraction(q, pal) > cap:
            raise ValueError(f"{lab}: quantity exceeds pallet capacity {cap}")
        emit(x, q, fx["reason"], True)

    th = {k: _num(r[k]) for k in _NUMS}
    T, M = th["target_days"], th["max_days"]
    low = T * Fraction(7, 10)  # "reasonably near target" window for pallet presets: [0.7*target, max_days]
    fixed_keys = {(fx["article"], fx["cabinet"]) for fx in r["fixed"]}
    cands, eligible = [], 0
    for key, x in src.items():
        if x["wild"] in banned:
            continue  # permanent ban: rejected before any eligibility/quantity work
        if key in fixed_keys or _norm(x["wild"]) in ex_w or _norm(key[1]) in ex_c:
            continue
        if any(x[k] is None for k in _METRICS):
            warn.append(f"test row {x['row']} {key[0]}/{key[1]}: missing or invalid numeric data; skipped")
            continue
        if (min(x[k] for k in ("s3", "s7", "s14", "s30")) < th["min_sales"]
                or x["cur"] > th["max_current_days"] or x["wild_days"] < th["min_wild_days"]):
            continue
        eligible += 1
        if x["forecast"] < T and x["wild"] not in bad:
            cands.append(x)
    cands.sort(key=lambda x: (x["forecast"], x["article"], x["cabinet"]))

    for x in cands:
        w, f, s7 = x["wild"], x["forecast"], x["s7"]
        box, pal = pack[w]
        lim = min(budget[w] - used.get(w, 0), math.floor((cap - total) * pal), math.floor((M - f) * s7))
        nb = lim // box
        if nb < 1:
            warn.append(f"{x['article']}/{x['cabinet']}: no feasible whole-box quantity (stock/capacity/max_days)")
            continue

        def after(q):
            return f + Fraction(q) / s7

        def score(q):
            return abs(after(q) - T), q

        presets = [Fraction(pal, 4), Fraction(pal, 2)] + [Fraction(k * pal) for k in range(1, lim // pal + 1)]
        opts = [q for q in presets if q.denominator == 1 and q <= lim and q % box == 0 and low <= after(q) <= M]
        if opts:
            q = int(min(opts, key=score))
            how = f"{Fraction(q, pal)} pallet"
        else:
            k = (T - f) * s7 / box
            q = min({box * max(1, min(nb, n)) for n in (math.floor(k), math.ceil(k))}, key=score)
            how = "whole boxes"
        emit(x, q, f"regular: {how} toward {_f(T)} days", False)

    return {"rules": r, "date": sd.isoformat(), "warehouse": warehouse.strip(),
            "arrival_date": ad.isoformat() if ad else None, "lines": lines, "warnings": warn,
            "eligible_count": eligible, "total_pallets": _f(total), "remaining_pallets": _f(cap - total),
            "source_hash": meta["source_hash"], "captured_at": meta["captured_at"]}
