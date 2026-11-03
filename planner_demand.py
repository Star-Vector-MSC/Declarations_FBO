"""Demand dynamics for the FBO planner: pure functions over the daily order series of one WB article.
Stdlib only; no IO. Blank/invalid cells are missing data, never zero orders. Thresholds are NOT calibrated
claims: they are the defaults in PARAMS and are checked by backtest() on sequential past origins."""
import re
import statistics
from datetime import date, datetime, timedelta
from fractions import Fraction

import planner

GROWTH, STABLE, GENTLE, SHARP, UNCERTAIN = "growth", "stable", "gentle_decline", "sharp_decline", "uncertain"
LABELS = {GROWTH: "рост", STABLE: "стабильно", GENTLE: "плавное снижение", SHARP: "резкое падение",
          UNCERTAIN: "неопределённо"}
PARAMS = {"min_points": 5, "growth": 1.25, "stable_low": 0.85, "sharp": 0.5, "sharp3": 0.4, "cv_max": 0.6,
          "cv_gentle": 0.5, "spike_share": 0.5, "decline_cap_days": 7, "max_stale_days": 2,
          "drop1": 0.4, "drop2": 0.6, "drop3": 0.6}
_HDR = re.compile(r"\s*(\d{1,2})\.(\d{1,2})\s*")
_EPOCH = date(1899, 12, 30)


def _header_day(h, ref):
    """Header 'ДД.ММ' (no year) or a date serial -> date. A year is never taken silently from an old export:
    ДД.ММ is placed in the latest year not after the reading date, and freshness is checked by the caller."""
    if isinstance(h, (int, float)) and not isinstance(h, bool) and 30000 < h < 70000:
        return _EPOCH + timedelta(days=int(h))
    m = _HDR.fullmatch(h) if isinstance(h, str) else None
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    for y in (ref.year, ref.year - 1):
        try:
            c = date(y, mo, d)
        except ValueError:
            continue
        if c <= ref:
            return c
    return None


def parse_orders(values, captured_at):
    """values: raw rows of the «Количество заказов» sheet. Returns
    {"ref": iso, "dates": [iso oldest..newest completed day], "series": {article: [Fraction|None]},
     "problems": [str], "ok": bool}. ok=False -> the whole sheet is unusable (every article gets 'uncertain')."""
    ref = datetime.fromisoformat(captured_at).date() if isinstance(captured_at, str) else captured_at
    out = {"ref": ref.isoformat(), "dates": [], "series": {}, "problems": [], "ok": False}
    if not values or not isinstance(values[0], (list, tuple)):
        out["problems"].append("лист «Количество заказов» пуст")
        return out
    head = list(values[0])
    art = [i for i, h in enumerate(head) if planner._norm(h) == "артикул"]
    cols = [(i, _header_day(h, ref)) for i, h in enumerate(head)]
    cols = [(i, d) for i, d in cols if d is not None]
    if len(art) != 1 or len(cols) < 14:
        out["problems"].append("в листе «Количество заказов» не найден один столбец «Артикул» или меньше 14 дат")
        return out
    days = [d for _, d in cols]
    if any(a - b != timedelta(days=1) for a, b in zip(days, days[1:])):
        out["problems"].append("даты в листе «Количество заказов» идут не подряд (ожидается по одному дню, новейшая слева)")
        return out
    cols.reverse()  # oldest -> newest
    if cols[-1][1] >= ref:
        cols.pop()  # the reading day is incomplete: never a full day
    if cols[-1][1] < ref - timedelta(days=PARAMS["max_stale_days"]):
        out["problems"].append(f"данные устарели: новейший полный день {cols[-1][1].isoformat()}")
        return out
    out["dates"] = [d.isoformat() for _, d in cols]
    seen, dup = {}, set()
    for n, row in enumerate(values[1:], 2):
        if not isinstance(row, (list, tuple)) or art[0] >= len(row) or planner._blank(row[art[0]]):
            continue
        a = planner._ident(row[art[0]])
        if a in seen:
            dup.add(a)
            continue
        vals = []
        for i, _ in cols:
            v = planner._num(row[i]) if i < len(row) else None
            vals.append(v if v is not None and v >= 0 else None)
        seen[a] = vals
    for a in dup:
        seen.pop(a, None)
    if dup:
        out["problems"].append(f"артикулов с дублирующимися строками в листе заказов: {len(dup)} (для них спрос неопределён)")
    out["series"], out["ok"] = seen, True
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def classify(vals, params=None):
    """vals: completed daily orders, oldest -> newest (None = unknown). -> {"state", "why", "ratio7", ...}."""
    P = {**PARAMS, **(params or {})}
    if vals is None or len(vals) < 14:
        return {"state": UNCERTAIN, "why": "истории меньше 14 дней"}
    a, b = vals[-7:], vals[-14:-7]
    if sum(x is not None for x in a) < P["min_points"] or sum(x is not None for x in b) < P["min_points"]:
        return {"state": UNCERTAIN, "why": "в неделе меньше пяти заполненных дней"}
    ma, mb = _mean(a), _mean(b)
    if mb == 0 or ma == 0:
        return {"state": UNCERTAIN, "why": "нулевые заказы за неделю: отсутствие товара не доказывает спрос"}
    r = float(ma / mb)
    m3, p3 = _mean(vals[-3:]), _mean(vals[-10:-7])
    r3 = float(m3 / p3) if m3 is not None and p3 else None
    xs = [float(x) for x in a if x is not None]
    cv = statistics.pstdev(xs) / float(ma)
    spike = max(xs) / sum(xs)
    info = {"ratio7": round(r, 2), "ratio3": None if r3 is None else round(r3, 2), "cv": round(cv, 2)}
    # sharp drop of the last 1-3 days against the 7 days before them: do not ship regardless of the weekly ratio
    for n, key in ((1, "drop1"), (2, "drop2"), (3, "drop3")):
        recent, prior = _mean(vals[-n:]), _mean(vals[-n - 7:-n])
        if recent is not None and prior and recent <= prior * P[key] and (n > 1 or sum(x is not None for x in vals[-n - 7:-n]) >= 5):
            return {"state": SHARP, "why": f"последние {n} дн. упали до ×{float(recent / prior):.2f} к предыдущим 7", **info}
    if r <= P["sharp"] or (r3 is not None and r3 <= P["sharp3"] and r < P["stable_low"]):
        return {"state": SHARP, "why": f"последние 7 дн. к предыдущим ×{r:.2f}", **info}
    if r >= P["growth"]:
        if spike > P["spike_share"] or cv > P["cv_max"] or (r3 is not None and r3 < 1):
            return {"state": UNCERTAIN, "why": f"рост ×{r:.2f} не подтверждён: всплеск/колебания", **info}
        return {"state": GROWTH, "why": f"последние 7 дн. к предыдущим ×{r:.2f}", **info}
    if r < P["stable_low"]:
        if r3 is not None and r3 >= 1:
            return {"state": STABLE, "why": f"неделя ×{r:.2f}, но последние 3 дня уже не ниже прошлой недели", **info}
        if cv > P["cv_gentle"]:
            return {"state": UNCERTAIN, "why": f"снижение ×{r:.2f} при сильных колебаниях", **info}
        return {"state": GENTLE, "why": f"последние 7 дн. к предыдущим ×{r:.2f}", **info}
    return {"state": STABLE, "why": f"последние 7 дн. к предыдущим ×{r:.2f}", **info}


def horizon(state, target, maxd, params=None):
    """Same policy as planner._horizon: (target, max) days or None = do not ship."""
    r = planner._horizon({"state": state}, target, maxd)
    return None if r[0] is None else r


def analyse(orders, article):
    """orders: parse_orders() result or None -> classification dict (never raises)."""
    if not orders:
        return None
    if not orders.get("ok"):
        return {"state": UNCERTAIN, "why": "; ".join(orders.get("problems") or ["лист заказов недоступен"])}
    s = orders["series"].get(article)
    if s is None:
        return {"state": UNCERTAIN, "why": "артикула нет в листе «Количество заказов» или он дублируется"}
    return classify(s)


def backtest(series, params=None, horizons=(7,), min_hist=14):
    """Sequential origins: for each origin t use only vals[:t]; compare the state with the realised ratio of the
    next h days' mean to the last-7 mean. series: {article: [..]}. Returns {state: {h: [count, median ratio,
    share_below_0.85, share_above_1.25]}} plus baseline 'all'. Uses only information available at the origin."""
    res = {}
    for vals in series.values():
        for t in range(min_hist, len(vals)):
            st = classify(vals[:t], params)["state"]
            base = _mean(vals[t - 7:t])
            if not base:
                continue
            for h in horizons:
                fut = vals[t:t + h]
                if len(fut) < h or sum(x is not None for x in fut) < max(3, h // 2):
                    continue
                res.setdefault(st, {}).setdefault(h, []).append(float(_mean(fut) / base))
                res.setdefault("all", {}).setdefault(h, []).append(float(_mean(fut) / base))
    out = {}
    for st, hs in res.items():
        out[st] = {h: [len(v), round(statistics.median(v), 2), round(sum(x < 0.85 for x in v) / len(v), 2),
                       round(sum(x > 1.25 for x in v) / len(v), 2)] for h, v in hs.items()}
    return out
