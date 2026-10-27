"""Planner memory: append-only SQLite event log <-> strict versioned Markdown archive.

Stdlib only. The caller supplies an explicit SQLite path (a data DB, separate from live
declarations) and optionally an explicit Markdown backup path. Nothing here reads app
config, secrets or the network. Restore is always explicit (restore_markdown); readable
handwritten notes are never treated as machine data. The SHA-256 digest detects
corruption only; it is not authentication. Secret detection is best-effort only.
"""
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid

SCHEMA_VERSION = 1
ARCHIVE_FORMAT = "planner-memory-archive"
MARKER = "<!-- planner-memory-archive v1 machine-data -->"
MAX_EVENTS, MAX_BYTES, MAX_TEXT, MAX_TARGET = 10000, 2_000_000, 4000, 200
KINDS, SCOPES = ("rule", "case"), ("global", "product", "plan")
_RESULT = {"confirm": "confirmed", "reject": "rejected", "revoke": "revoked"}
_FROM = {"confirm": "pending", "reject": "pending", "revoke": "confirmed"}
_MSK = _dt.timezone(_dt.timedelta(hours=3))  # Moscow, no DST since 2014
_TS_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"
_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SECRET_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"
    r"|\bsk-[A-Za-z0-9_-]{20,}|\b[0-9]{8,10}:[A-Za-z0-9_-]{30,}|\bAKIA[0-9A-Z]{16}\b"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}")
_DATA_KEYS = ("text", "reason", "scope", "target", "expires_on", "kind")
_EVENT_KEYS = {"seq", "event_id", "type", "entry_id", "at"}
_COLUMNS = ["seq", "event_id", "payload"]
_CREATE_SQL = ("CREATE TABLE events(seq INTEGER PRIMARY KEY, "
               "event_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class PlannerMemoryError(Exception):
    """Base error for planner memory."""


class StorageError(PlannerMemoryError):
    """Database unreadable/unsupported/corrupt; it is never reset or replaced."""


class ArchiveError(PlannerMemoryError, ValueError):
    """Markdown archive missing, corrupt, foreign or unsupported; nothing was restored."""


class ConflictError(PlannerMemoryError):
    """Destination DB is non-empty and differs from the archive; no merge performed."""


class TransitionError(PlannerMemoryError, ValueError):
    """Requested state change is not allowed; nothing was written."""


class BackupError(PlannerMemoryError):
    """DB commit SUCCEEDED but the Markdown mirror failed. Retry write_backup()."""

    def __init__(self, message, *, entry_id=None, event_id=None, path=None):
        super().__init__(message)
        self.saved, self.entry_id, self.event_id, self.path = True, entry_id, event_id, path


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(",", ":"))


def _pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _no_const(name):
    raise ValueError(f"non-finite number {name}")


def _loads(text):
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_const)


def _digest(events):
    body = {"format": ARCHIVE_FORMAT, "schema_version": SCHEMA_VERSION, "events": events}
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()


def _now():
    return _dt.datetime.now(_dt.timezone.utc).strftime(_TS_FMT)


def _is_uuid(value):
    if type(value) is not str:
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _check_date(value, what):
    if type(value) is not str or not _DATE_RE.fullmatch(value):
        raise ValueError(f"{what} must be an ISO YYYY-MM-DD string")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{what} is not a valid date") from None
    return value


def _check_fields(data):
    if type(data) is not dict or set(data) != set(_DATA_KEYS):
        raise ValueError("entry data must have exactly the allowed fields")
    text, reason, scope, target, expires_on, kind = (data[k] for k in _DATA_KEYS)
    if type(text) is not str or not text.strip() or len(text) > MAX_TEXT:
        raise ValueError(f"text must be a non-empty string of at most {MAX_TEXT} chars")
    if type(reason) is not str or len(reason) > MAX_TEXT:
        raise ValueError(f"reason must be a string of at most {MAX_TEXT} chars")
    if type(scope) is not str or scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}")
    if (type(target) is not str or len(target) > MAX_TARGET or target != target.strip()
            or (scope == "global") == bool(target)):
        raise ValueError("target must be empty for global scope and non-empty otherwise")
    if expires_on is not None:
        _check_date(expires_on, "expires_on")
    if type(kind) is not str or kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    for value in (text, reason, target):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("text fields must be valid UTF-8") from None
        if _SECRET_RE.search(value):
            raise ValueError("value looks like a secret and was refused (best-effort check)")


def _validate_events(events):
    """Validate full history strictly; return {entry_id: status}. Raises ValueError."""
    if type(events) is not list or len(events) > MAX_EVENTS:
        raise ValueError(f"events must be a list of at most {MAX_EVENTS} items")
    states, seen = {}, set()
    for i, ev in enumerate(events, 1):
        if type(ev) is not dict:
            raise ValueError(f"event {i} is not an object")
        if set(ev) != (_EVENT_KEYS | ({"data"} if ev.get("type") == "create" else set())):
            raise ValueError(f"event {i} has missing or unknown keys")
        if type(ev["seq"]) is not int or ev["seq"] != i:
            raise ValueError(f"event {i} has non-monotonic sequence")
        if not _is_uuid(ev["event_id"]) or not _is_uuid(ev["entry_id"]):
            raise ValueError(f"event {i} has an invalid UUID")
        if ev["event_id"] in seen:
            raise ValueError(f"event {i} duplicates an event_id")
        seen.add(ev["event_id"])
        at = ev["at"]
        try:
            if type(at) is not str or not _TS_RE.fullmatch(at):
                raise ValueError
            _dt.datetime.strptime(at, _TS_FMT)
        except ValueError:
            raise ValueError(f"event {i} has an invalid timestamp") from None
        kind, eid = ev["type"], ev["entry_id"]
        if kind == "create":
            if eid in states:
                raise ValueError(f"event {i} re-creates an existing entry")
            _check_fields(ev["data"])
            states[eid] = "pending"
        elif type(kind) is str and kind in _RESULT:
            if states.get(eid) != _FROM[kind]:
                raise ValueError(f"event {i}: {kind} not allowed from {states.get(eid)}")
            states[eid] = _RESULT[kind]
        else:
            raise ValueError(f"event {i} has unknown type")
    return states


def _records(events):
    recs = {}
    for ev in events:
        if ev["type"] == "create":
            recs[ev["entry_id"]] = dict(id=ev["entry_id"], **ev["data"], status="pending",
                                        created_at=ev["at"])
        else:
            recs[ev["entry_id"]]["status"] = _RESULT[ev["type"]]
    return list(recs.values())


def _render(events):
    q = lambda s: json.dumps(s, ensure_ascii=False)  # quoted: user text cannot forge lines
    lines = ["# Planner memory archive", "",
             "Human-readable summary below is NOT authoritative; only the machine block is restored.",
             "", f"Events: {len(events)}", ""]
    for r in _records(events):
        lines.append(f"- {r['id']} [{r['status']}] kind={r['kind']} scope={r['scope']} "
                     f"target={q(r['target'])} expires_on={r['expires_on'] or '-'}")
        lines.append(f"  text: {q(r['text'])}")
        if r["reason"]:
            lines.append(f"  reason: {q(r['reason'])}")
    body = {"format": ARCHIVE_FORMAT, "schema_version": SCHEMA_VERSION, "events": events,
            "sha256": _digest(events)}
    lines += ["", MARKER, "```json", _canon(body), "```", ""]
    return "\n".join(lines)


def parse_archive(text):
    """Strictly parse and validate an archive; return its events. Raises ArchiveError."""
    try:
        if type(text) is not str or len(text.encode("utf-8")) > MAX_BYTES:
            raise ArchiveError(f"archive must be a string of at most {MAX_BYTES} bytes")
    except UnicodeEncodeError:
        raise ArchiveError("archive is not valid UTF-8 text") from None
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    marks = [i for i, ln in enumerate(lines) if ln == MARKER]
    if len(marks) != 1:
        raise ArchiveError("expected exactly one machine marker; handwritten notes are not an archive")
    i = marks[0]
    if (sum(ln.startswith("```") for ln in lines) != 2 or lines[i + 1:i + 2] != ["```json"]
            or lines[i + 3:i + 4] != ["```"] or any(ln.strip() for ln in lines[i + 4:])):
        raise ArchiveError("expected exactly one fenced JSON block right after the marker")
    try:
        body = _loads(lines[i + 2])
    except (ValueError, RecursionError) as exc:
        raise ArchiveError(f"invalid archive JSON: {exc}") from None
    if type(body) is not dict or set(body) != {"format", "schema_version", "events", "sha256"}:
        raise ArchiveError("archive body has missing or unknown keys")
    if body["format"] != ARCHIVE_FORMAT:
        raise ArchiveError("not a planner memory archive")
    if type(body["schema_version"]) is not int or body["schema_version"] != SCHEMA_VERSION:
        raise ArchiveError("unsupported archive schema version")
    try:
        digest = _digest(body["events"])
    except (ValueError, RecursionError) as exc:  # e.g. escaped lone surrogate (UnicodeEncodeError)
        raise ArchiveError(f"archive data is not canonical UTF-8: {exc}") from None
    if type(body["sha256"]) is not str or body["sha256"] != digest:
        raise ArchiveError("archive checksum mismatch (corrupted)")
    try:
        _validate_events(body["events"])
    except ValueError as exc:
        raise ArchiveError(f"invalid archive history: {exc}") from None
    if len(_render(body["events"]).encode("utf-8")) > MAX_BYTES:
        raise ArchiveError(f"canonical export would exceed {MAX_BYTES} bytes; nothing restored")
    return body["events"]


def _atomic_write(path, data):
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".planner-memory-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)  # only our own temp file
        raise
    if os.name == "posix":
        with contextlib.suppress(OSError):
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)


class MemoryStore:
    """Durable planner memory bound to one explicit SQLite file."""

    def __init__(self, db_path, backup_path=None):
        self.db_path = os.path.abspath(os.fspath(db_path))
        self.backup_path = None if backup_path is None else self._backup_target(backup_path)
        key = os.path.normcase(os.path.realpath(self.db_path))
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())
        self._init_db()  # never writes the backup file

    def _backup_target(self, path):
        path = os.path.abspath(os.fspath(path))
        if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(self.db_path)):
            raise ValueError("backup path must differ from the database path")
        return path

    @contextlib.contextmanager
    def _txn(self):
        with self._lock:
            try:
                con = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            except sqlite3.Error as exc:
                raise StorageError(f"cannot open database: {exc}") from exc
            try:
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.execute("COMMIT")
            except sqlite3.Error as exc:
                raise StorageError(f"database error (left as is, never reset): {exc}") from exc
            finally:
                with contextlib.suppress(sqlite3.Error):
                    if con.in_transaction:
                        con.execute("ROLLBACK")
                con.close()

    def _init_db(self):
        with self._txn() as con:
            ver = con.execute("PRAGMA user_version").fetchone()[0]
            names = sorted(r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex_%'"))
            if ver == 0 and not names:
                con.execute(_CREATE_SQL)
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                return
            cols = [r[1] for r in con.execute("PRAGMA table_info(events)")]
            if ver != SCHEMA_VERSION or names != ["events"] or cols != _COLUMNS:
                raise StorageError(f"unsupported database schema (user_version={ver}); left untouched")
            self._load(con)

    def _load(self, con):
        events = []
        for seq, event_id, payload in con.execute("SELECT seq, event_id, payload FROM events ORDER BY seq"):
            try:
                ev = _loads(payload) if type(payload) is str else None
            except (ValueError, RecursionError):
                ev = None
            if type(ev) is not dict or ev.get("seq") != seq or ev.get("event_id") != event_id:
                raise StorageError(f"corrupt event row seq={seq}; database left untouched")
            events.append(ev)
        try:
            _validate_events(events)
        except ValueError as exc:
            raise StorageError(f"invalid stored history: {exc}") from None
        return events

    def _commit(self, entry_id, build):
        with self._txn() as con:
            events = self._load(con)
            ev = build(events)
            if ev is not None:
                ev["seq"] = len(events) + 1
                new = events + [ev]
                _validate_events(new)
                if len(_render(new).encode("utf-8")) > MAX_BYTES:
                    raise StorageError("archive size limit reached; nothing saved")
                con.execute("INSERT INTO events(seq, event_id, payload) VALUES (?, ?, ?)",
                            (ev["seq"], ev["event_id"], _canon(ev)))
        self._mirror(entry_id, ev["event_id"] if ev else None)
        return ev

    def _mirror(self, entry_id, event_id):
        if self.backup_path is None:
            return
        try:
            self.write_backup()
        except Exception as exc:
            raise BackupError(
                f"SAVED in database (entry={entry_id}, event={event_id}) but backup write failed: "
                f"{type(exc).__name__}: {exc}; retry write_backup()",
                entry_id=entry_id, event_id=event_id, path=self.backup_path) from exc

    def propose(self, text, *, reason="", scope="global", target="", expires_on=None, kind="rule"):
        if isinstance(expires_on, _dt.date) and not isinstance(expires_on, _dt.datetime):
            expires_on = expires_on.isoformat()
        data = dict(text=text, reason=reason, scope=scope, target=target, expires_on=expires_on, kind=kind)
        _check_fields(data)
        ev = {"event_id": str(uuid.uuid4()), "type": "create", "entry_id": str(uuid.uuid4()),
              "at": _now(), "data": data}
        self._commit(ev["entry_id"], lambda events: ev)
        return ev["entry_id"]

    def _transition(self, entry_id, action):
        if not _is_uuid(entry_id):
            raise ValueError("entry_id must be a canonical UUID string")

        def build(events):
            status = _validate_events(events).get(entry_id)
            if status is None:
                raise TransitionError(f"unknown entry {entry_id}")
            if status == _RESULT[action]:
                return None  # idempotent replay; still retries the mirror
            if status != _FROM[action]:
                raise TransitionError(f"cannot {action} an entry that is {status}")
            return {"event_id": str(uuid.uuid4()), "type": action, "entry_id": entry_id, "at": _now()}

        self._commit(entry_id, build)
        return _RESULT[action]

    def confirm(self, entry_id):
        return self._transition(entry_id, "confirm")

    def reject(self, entry_id):
        return self._transition(entry_id, "reject")

    def revoke(self, entry_id):
        return self._transition(entry_id, "revoke")

    def history(self):
        with self._txn() as con:
            return self._load(con)

    def entries(self):
        return _records(self.history())

    def applicable(self, *, product=None, plan_id=None, on_date=None):
        """Confirmed, non-expired (inclusive) records: global or exact product/plan target."""
        for value in (product, plan_id):
            if value is not None and type(value) is not str:
                raise TypeError("product and plan_id must be strings or None")
        if on_date is None:
            day = _dt.datetime.now(_MSK).date().isoformat()
        elif isinstance(on_date, _dt.datetime):
            raise TypeError("on_date must be a date or YYYY-MM-DD string")
        elif isinstance(on_date, _dt.date):
            day = on_date.isoformat()
        else:
            day = _check_date(on_date, "on_date")
        return [r for r in self.entries()
                if r["status"] == "confirmed"
                and (r["expires_on"] is None or r["expires_on"] >= day)
                and (r["scope"] == "global"
                     or (r["scope"] == "product" and r["target"] == product)
                     or (r["scope"] == "plan" and r["target"] == plan_id))]

    def export_markdown(self):
        with self._txn() as con:
            return _render(self._load(con))

    def write_backup(self, path=None):
        if path is None and self.backup_path is None:
            raise ValueError("no backup path configured or given")
        target = self.backup_path if path is None else self._backup_target(path)
        with self._txn() as con:  # snapshot + write under lock: never stale
            _atomic_write(target, _render(self._load(con)).encode("utf-8"))
        return target

    def restore_markdown(self, text):
        """Explicit restore: 'restored' into empty DB, 'unchanged' if identical, else ConflictError."""
        events = parse_archive(text)  # also rejects oversized canonical export before any write
        with self._txn() as con:
            current = self._load(con)
            if current == events:
                result = "unchanged"
            elif current:
                raise ConflictError("destination is not empty and differs from archive; refusing to merge")
            else:
                con.executemany("INSERT INTO events(seq, event_id, payload) VALUES (?, ?, ?)",
                                [(e["seq"], e["event_id"], _canon(e)) for e in events])
                result = "restored"
        self._mirror(None, None)
        return result
