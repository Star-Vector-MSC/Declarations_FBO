"""Owner-scoped immutable plan draft versions in a dedicated SQLite file (stdlib only, no IO at import)."""
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.path.join("data", "planner_drafts.sqlite3")
_ID = re.compile(r"[0-9a-f]{12}")
_NF = "Черновик не найден"
_INS = "INSERT INTO versions(id, version, owner_id, plan, created_at) VALUES (?,?,?,?,?)"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions(id TEXT NOT NULL, version INTEGER NOT NULL, owner_id TEXT NOT NULL,
  plan TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(id, version));
CREATE INDEX IF NOT EXISTS versions_owner ON versions(owner_id, id);
CREATE TABLE IF NOT EXISTS exports(id TEXT PRIMARY KEY, version INTEGER NOT NULL, url TEXT NOT NULL,
  exported_at TEXT NOT NULL, FOREIGN KEY(id, version) REFERENCES versions(id, version));
CREATE TRIGGER IF NOT EXISTS versions_no_u BEFORE UPDATE ON versions BEGIN SELECT RAISE(ABORT, 'immutable'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_d BEFORE DELETE ON versions BEGIN SELECT RAISE(ABORT, 'immutable'); END;
CREATE TRIGGER IF NOT EXISTS exports_no_u BEFORE UPDATE ON exports BEGIN SELECT RAISE(ABORT, 'immutable'); END;
CREATE TRIGGER IF NOT EXISTS exports_no_d BEFORE DELETE ON exports BEGIN SELECT RAISE(ABORT, 'immutable'); END;
"""


class DraftError(Exception):
    """Friendly storage error, safe to show to users."""


class DraftNotFound(DraftError):
    pass


class DraftConflict(DraftError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _owner(o):
    if isinstance(o, bool) or not isinstance(o, (int, str)) or not str(o).strip():
        raise ValueError("owner id is required")
    return str(o).strip()


def _dump(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("lines"), list):
        raise ValueError("plan must be a complete planner.build_plan result")
    return json.dumps(plan, ensure_ascii=False, sort_keys=True, allow_nan=False)


class DraftStore:
    """create/revise/get/list/mark_exported; every read and write is scoped by owner id."""

    def __init__(self, db_path):
        if not db_path:
            raise ValueError("db_path is required")
        self.db_path = os.fspath(db_path)

    @contextmanager
    def _tx(self, write=False):
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            con.execute("PRAGMA foreign_keys=ON")
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

    def _get(self, con, owner, did, version=None):
        if not isinstance(did, str) or not _ID.fullmatch(did):
            raise DraftNotFound(_NF)
        if version is not None and (type(version) is not int or version < 1):
            raise DraftNotFound(_NF)
        latest = con.execute("SELECT MAX(version) FROM versions WHERE id=? AND owner_id=?", (did, owner)).fetchone()[0]
        v = latest if version is None else version
        row = latest and con.execute("SELECT plan, created_at FROM versions WHERE id=? AND owner_id=? AND version=?",
                                     (did, owner, v)).fetchone()
        if not row:
            raise DraftNotFound(_NF)
        ex = con.execute("SELECT version, url, exported_at FROM exports WHERE id=?", (did,)).fetchone() or (None,) * 3
        mine = ex[0] == v
        return {"id": did, "version": v, "owner_id": owner, "plan": json.loads(row[0]), "created_at": row[1],
                "latest_version": latest, "exported_version": ex[0], "export_url": ex[1] if mine else None,
                "exported_at": ex[2] if mine else None}

    def create(self, owner, plan):
        owner, body = _owner(owner), _dump(plan)
        with self._tx(True) as con:
            did = secrets.token_hex(6)
            while con.execute("SELECT 1 FROM versions WHERE id=?", (did,)).fetchone():
                did = secrets.token_hex(6)
            con.execute(_INS, (did, 1, owner, body, _now()))
            return self._get(con, owner, did)

    def revise(self, owner, did, expected_version, plan):
        """Append version+1 only if expected_version is still the latest (transactional compare-and-swap)."""
        owner, body = _owner(owner), _dump(plan)
        with self._tx(True) as con:
            cur = self._get(con, owner, did)
            if cur["exported_version"] is not None:
                raise DraftConflict("Черновик уже выгружен; создайте новый черновик")
            if cur["version"] != expected_version:
                raise DraftConflict("Черновик уже изменён; откройте последнюю версию")
            con.execute(_INS, (did, cur["version"] + 1, owner, body, _now()))
            return self._get(con, owner, did)

    def get(self, owner, did, version=None):
        owner = _owner(owner)
        with self._tx() as con:
            return self._get(con, owner, did, version)

    def list(self, owner, limit=10):
        owner, limit = _owner(owner), max(1, min(int(limit), 100))
        with self._tx() as con:
            ids = [r[0] for r in con.execute("SELECT id FROM versions WHERE owner_id=? GROUP BY id "
                                             "ORDER BY MAX(rowid) DESC LIMIT ?", (owner, limit))]
            return [self._get(con, owner, i) for i in ids]

    def mark_exported(self, owner, did, version, url):
        """Record a CONFIRMED remote export; one version per draft, idempotent for the same version/url."""
        owner = _owner(owner)
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("url must be an https link")
        with self._tx(True) as con:
            d = self._get(con, owner, did, version)
            if d["exported_version"] is None:
                con.execute("INSERT INTO exports VALUES (?,?,?,?)", (did, d["version"], url, _now()))
                return self._get(con, owner, did, version)
            if d["exported_version"] == d["version"] and d["export_url"] == url:
                return d
            raise DraftConflict("Другая версия этого черновика уже выгружена")
