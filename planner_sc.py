"""Where each wild may be shipped: history of ACCEPTED FBW supplies (PostgreSQL, read-only) plus the owner's answers read in the
WB cabinet. Separate SQLite file; no IO at import. The history is a snapshot refreshed when older than REFRESH_DAYS; an owner's
answer is trusted for MANUAL_DAYS. «Shipped there before» is evidence, not a live guarantee of acceptance."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import planner

DEFAULT_DB_PATH = os.path.join("data", "planner_sc.sqlite3")
SINCE = date(2026, 10, 1)  # earlier shipments are not evidence: the owner counts only what was shipped since October
REFRESH_DAYS = 7
MANUAL_DAYS = 10
_WILD = re.compile(r"(wild\d+)(?:d\d*)?")
# The sheet and WB name the same warehouse differently ("Рязань" / "Рязань (Тюшевское)", "Обухово -> Волгоград" / "Волгоград");
# names are compared by destination without brackets. Only spellings that cannot be derived by that rule are listed here.
_ALIAS = {"владимир воршинское": "владимир"}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS history(wild TEXT NOT NULL, warehouse TEXT NOT NULL, last_date TEXT NOT NULL,
  supplies INTEGER NOT NULL, PRIMARY KEY(wild, warehouse));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS manual(wild TEXT NOT NULL, warehouse TEXT NOT NULL, checked_at TEXT NOT NULL);
"""
_SQL = """SELECT a.local_vendor_code, s.warehouse_name, max(s.create_date), count(DISTINCT s.id)
          FROM wb_supplies s
          JOIN wb_supplies_goods g ON g.id = s.id
          JOIN article a ON a.nm_id = g.nm_id
          WHERE s.status_id = 6 AND s.create_date >= %s AND COALESCE(g.accepted_quantity, 0) > 0
                AND COALESCE(s.warehouse_name, '') <> '' AND a.local_vendor_code IS NOT NULL
          GROUP BY 1, 2"""


class ScError(Exception):
    """Friendly error, safe to show to the user."""


def base_wild(value):
    """'wild797d2' / ' WILD797 ' -> 'wild797'; anything else -> None."""
    m = _WILD.fullmatch(planner._norm(value or ""))
    return m.group(1) if m else None


def wkey(name):
    """Comparable warehouse key: the destination of a «A -> B» route, no bracketed part, normalised spelling."""
    n = planner._norm(name or "").split("->")[-1]
    n = planner._norm(re.sub(r"\(.*?\)", " ", n))
    return _ALIAS.get(n, n)


def _now():
    return datetime.now(timezone.utc)


def _iso(t):
    return t.isoformat(timespec="seconds")


def fetch_history(now=None, since=SINCE):
    """[(wild, warehouse, last_date_iso, supplies)] from PostgreSQL, read-only session. Same connection settings as the
    declarations module (its import loads .env)."""
    import wb_declarations  # noqa: F401  (loads .env into os.environ)
    import psycopg
    need = ("PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE")
    if not all(os.environ.get(n) for n in need):
        raise ScError("База данных не настроена: история отгрузок по складам недоступна.")
    try:
        with psycopg.connect(host=os.environ["PGHOST"], port=int(os.environ.get("PGPORT", "5432")),
                             user=os.environ["PGUSER"], password=os.environ["PGPASSWORD"], dbname=os.environ["PGDATABASE"],
                             connect_timeout=15, sslmode=os.environ.get("PGSSLMODE", "prefer"),
                             options="-c default_transaction_read_only=on -c statement_timeout=60000") as con:
            raw = con.execute(_SQL, (datetime(since.year, since.month, since.day),)).fetchall()
    except Exception:
        raise ScError("Не удалось прочитать историю отгрузок из базы. Повторите позже.") from None
    merged = {}
    for vendor, wh, last, n in raw:
        w = base_wild(vendor)
        if w is None or not str(wh).strip():
            continue
        key = (w, str(wh).strip())
        old = merged.get(key)
        merged[key] = (max(old[0], last), old[1] + n) if old else (last, n)
    return [(w, wh, last.date().isoformat(), n) for (w, wh), (last, n) in sorted(merged.items())]


class ScStore:
    def __init__(self, db_path=DEFAULT_DB_PATH):
        self.db_path = os.fspath(db_path)

    @contextmanager
    def _tx(self, write=False):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            con.executescript(_SCHEMA)
            con.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                yield con
            except BaseException:
                con.execute("ROLLBACK")
                raise
            con.execute("COMMIT")
        finally:
            con.close()

    def refreshed_at(self):
        with self._tx() as con:
            row = con.execute("SELECT value FROM meta WHERE key='refreshed_at'").fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def stale(self, now=None):
        at = self.refreshed_at()
        return at is None or (now or _now()) - at > timedelta(days=REFRESH_DAYS)

    def replace_history(self, rows, now=None):
        """The new snapshot replaces the old one atomically; an empty fetch never wipes a good snapshot."""
        if not rows:
            raise ScError("База вернула пустую историю отгрузок; прежние данные оставлены.")
        with self._tx(True) as con:
            con.execute("DELETE FROM history")
            con.executemany("INSERT INTO history VALUES (?,?,?,?)", rows)
            con.execute("INSERT OR REPLACE INTO meta VALUES ('refreshed_at', ?)", (_iso(now or _now()),))

    def ensure_fresh(self, fetch=fetch_history, now=None, force=False):
        if force or self.stale(now):
            self.replace_history(fetch(now), now)
            return True
        return False

    def add_manual(self, pairs, now=None):
        """pairs: [(wild, warehouse)] the owner confirmed in the cabinet today."""
        at = _iso(now or _now())
        with self._tx(True) as con:
            con.executemany("INSERT INTO manual VALUES (?,?,?)", [(w, wh, at) for w, wh in pairs])

    def confirmed(self, warehouse, now=None):
        """Every wild with evidence for this warehouse (history since SINCE + the owner's answers still valid)."""
        now, key, out = now or _now(), wkey(warehouse), set()
        with self._tx() as con:
            rows = con.execute("SELECT wild, warehouse FROM history WHERE last_date >= ?", (SINCE.isoformat(),)).fetchall()
            rows += con.execute("SELECT wild, warehouse FROM manual WHERE checked_at >= ?",
                                (_iso(now - timedelta(days=MANUAL_DAYS)),)).fetchall()
        return {w for w, wh in rows if wkey(wh) == key}

    def analyze(self, wilds, warehouses, now=None):
        """wilds: the plan's wilds; warehouses: the names the planner can pick. Evidence per wild = history + the owner's
        answers younger than MANUAL_DAYS, matched to `warehouses` by normalised name."""
        now = now or _now()
        names = {}
        for w in warehouses:
            names.setdefault(wkey(w), []).append(w)
        wilds = sorted({w for w in (base_wild(x) for x in wilds) if w})
        ev, outside = {w: {} for w in wilds}, set()
        with self._tx() as con:
            hist = con.execute("SELECT wild, warehouse, last_date, supplies FROM history WHERE last_date >= ?",
                               (SINCE.isoformat(),)).fetchall()
            cutoff = _iso(now - timedelta(days=MANUAL_DAYS))
            man = con.execute("SELECT wild, warehouse, max(checked_at) FROM manual WHERE checked_at >= ? GROUP BY 1, 2",
                              (cutoff,)).fetchall()
        for w, wh, last, n in hist:
            if w in ev:
                if wkey(wh) in names:
                    for name in names[wkey(wh)]:
                        ev[w][name] = f"отгружали, последний раз {last[8:]}.{last[5:7]}.{last[:4]}"
                else:
                    outside.add(wh)
        for w, wh, at in man:
            if w in ev:
                for name in names.get(wkey(wh), ()):
                    ev[w][name] = "подтверждено вами в кабинете"
        nodata = [w for w in wilds if not ev[w]]
        known = [w for w in wilds if ev[w]]
        rank = []
        for wh in warehouses:
            miss = [w for w in wilds if wh not in ev[w]]
            rank.append((wh, len(wilds) - len(miss), miss))
        rank.sort(key=lambda r: (-r[1], r[0]))
        return {"wilds": wilds, "nodata": nodata, "refreshed_at": self.refreshed_at(),
                "full": [r[0] for r in rank if not r[2]],
                "full_known": [wh for wh, _c, miss in rank if all(m in nodata for m in miss)],
                "rank": rank, "outside": sorted(outside), "known": len(known)}
