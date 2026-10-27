import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from unittest import mock

import planner_memory as pm

DAY = "2026-10-27"


def read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def rebuild(md, mutate):
    """Mutate the machine body and re-seal it with a valid digest (synthetic tampering)."""
    lines = md.split("\n")
    i = lines.index(pm.MARKER) + 2
    body = json.loads(lines[i])
    mutate(body)
    dump = lambda o: json.dumps(o, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sealed = {k: body[k] for k in ("format", "schema_version", "events")}
    body["sha256"] = hashlib.sha256(dump(sealed).encode("utf-8")).hexdigest()
    lines[i] = dump(body)
    return "\n".join(lines)


class PlannerMemoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)

    def store(self, name="m.db", backup=None):
        return pm.MemoryStore(self.path(name), backup and self.path(backup))

    def populated(self, name="a.db"):
        s = self.store(name)
        ids = {"rule": s.propose("Keep two pallets in reserve", reason="ops note"),
               "case": s.propose("Late truck case", kind="case", scope="plan", target="P-7",
                                 expires_on="2026-12-31"),
               "rej": s.propose("Ignore stock limits"),
               "rev": s.propose("Old product rule", scope="product", target="SKU-1"),
               "pend": s.propose("Pending idea Ünïcödé")}
        s.confirm(ids["rule"]); s.confirm(ids["case"]); s.reject(ids["rej"])
        s.confirm(ids["rev"]); s.revoke(ids["rev"])
        return s, ids

    def test_reopen_retains_states_and_full_history(self):
        s, ids = self.populated()
        again = self.store("a.db")
        self.assertEqual({r["id"]: r["status"] for r in again.entries()},
                         {ids["rule"]: "confirmed", ids["case"]: "confirmed", ids["rej"]: "rejected",
                          ids["rev"]: "revoked", ids["pend"]: "pending"})
        self.assertEqual(again.history(), s.history())
        self.assertEqual([e["seq"] for e in again.history()], list(range(1, 11)))

    def test_transitions_idempotent_and_conflicts_write_nothing(self):
        s = self.store()
        a = s.propose("rule a")
        with self.assertRaises(ValueError):
            s.revoke(a)  # pending cannot be revoked
        self.assertEqual(s.entries()[0]["status"], "pending")
        self.assertEqual(s.confirm(a), "confirmed")
        self.assertEqual(s.confirm(a), "confirmed")
        self.assertEqual(len(s.history()), 2)
        with self.assertRaises(ValueError):
            s.reject(a)
        s.revoke(a)
        with self.assertRaises(ValueError):
            s.confirm(a)  # never resurrect
        self.assertEqual([e["type"] for e in s.history()], ["create", "confirm", "revoke"])
        for bad in (str(uuid.uuid4()), "not-a-uuid", 5):
            with self.assertRaises(ValueError):
                s.confirm(bad)
        self.assertEqual(len(s.history()), 3)

    def test_applicable_scopes_expiry_cases(self):
        s = self.store()
        def ok(text, **kw):
            i = s.propose(text, **kw); s.confirm(i); return i
        g = ok("global rule")
        p = ok("product rule", scope="product", target="SKU-1")
        ok("other product", scope="product", target="SKU-2")
        c = ok("plan case", kind="case", scope="plan", target="P-7")
        ok("expired", expires_on="2026-10-26")
        t = ok("ends today", expires_on=DAY)
        s.propose("pending global")
        s.revoke(ok("revoked"))
        got = s.applicable(product="SKU-1", plan_id="P-7", on_date=DAY)
        self.assertEqual([x["id"] for x in got], [g, p, c, t])
        self.assertEqual([x["kind"] for x in got], ["rule", "rule", "case", "rule"])
        self.assertEqual([x["id"] for x in s.applicable(on_date=DAY)], [g, t])
        self.assertEqual([x["id"] for x in s.applicable(on_date="2026-10-28")], [g])
        self.assertIsInstance(s.applicable(), list)

    def test_invalid_input_and_secret_shaped_values_rejected(self):
        s = self.store()
        bad = [("", {}), ("x" * 4001, {}), ("x", {"target": "SKU"}), ("x", {"scope": "product"}),
               ("x", {"scope": "shop", "target": "a"}), ("x", {"kind": "note"}), (1, {}),
               ("x", {"expires_on": "2026-13-01"}), ("x", {"expires_on": "27.10.2026"}),
               ("x", {"reason": None})]
        secrets = ["gh" + "p_" + "a1B2c3D4" * 5, "use " + "sk" + "-" + "Zz9yX8" * 5,
                   "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----", "12345678" + "9:" + "AAH" * 12]
        bad += [(v, {}) for v in secrets] + [("ok", {"reason": secrets[0]})]
        for text, kw in bad:
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                s.propose(text, **kw)
        self.assertEqual(s.history(), [])

    def test_export_roundtrip_idempotent_and_export_only(self):
        s, _ = self.populated()
        md = s.export_markdown()
        self.assertEqual(md, s.export_markdown())
        self.assertNotIn("m.db", os.listdir(self.dir))
        with self.assertRaises(ValueError):
            s.write_backup()
        self.assertEqual(read(s.write_backup(self.path("explicit.md"))), md)
        dst = self.store("b.db")
        self.assertEqual(dst.restore_markdown(md), "restored")
        self.assertEqual(dst.history(), s.history())
        self.assertEqual(dst.entries(), s.entries())
        self.assertEqual(dst.restore_markdown(md.replace("\n", "\r\n")), "unchanged")
        self.assertEqual(dst.export_markdown(), md)

    def test_user_text_cannot_forge_machine_block(self):
        s = self.store()
        s.propose("x\n" + pm.MARKER + "\n```json\n{}\n```")
        dst = self.store("b.db")
        dst.restore_markdown(s.export_markdown())
        self.assertEqual(dst.history(), s.history())

    def test_invalid_archives_restore_nothing(self):
        s, ids = self.populated()
        md = s.export_markdown()
        def drop_create(b):
            b["events"] = [e for e in b["events"] if not (e["type"] == "create" and e["entry_id"] == ids["rule"])]
            for n, e in enumerate(b["events"], 1):
                e["seq"] = n
        def setk(i, k, v):
            return lambda b: b["events"][i].update({k: v})
        def resurrect(b):
            ev = b["events"]
            ev.append({"seq": len(ev) + 1, "event_id": str(uuid.uuid4()), "type": "confirm",
                       "entry_id": ids["rev"], "at": ev[0]["at"]})
        cases = {
            "empty": "", "handwritten": "# Planner notes\n- keep two pallets\n", "none": None,
            "truncated": md[: len(md) // 2], "two_blocks": md + md,
            "checksum": md.replace("Keep two pallets", "Keep ten pallets"),
            "dup_key": md.replace('{"events":', '{"events":[],"events":', 1),
            "nan": md.replace('"schema_version":1', '"schema_version":NaN'),
            "future": rebuild(md, lambda b: b.update(schema_version=2)),
            "orphan": rebuild(md, drop_create), "resurrect": rebuild(md, resurrect),
            "seq_gap": rebuild(md, setk(1, "seq", 5)), "bool_seq": rebuild(md, setk(0, "seq", True)),
            "dup_event": rebuild(md, lambda b: b["events"][1].update(event_id=b["events"][0]["event_id"])),
            "bad_uuid": rebuild(md, setk(0, "entry_id", "not-a-uuid")),
            "unknown_key": rebuild(md, setk(0, "extra", 1)),
            "bad_time": rebuild(md, setk(0, "at", "2026-10-27 10:00")),
            "bad_date": rebuild(md, lambda b: b["events"][0]["data"].update(expires_on="2026-02-30")),
        }
        for name, text in cases.items():
            with self.subTest(name):
                dst = self.store(f"{name}.db")
                with self.assertRaises(pm.ArchiveError):
                    dst.restore_markdown(text)
                self.assertEqual(dst.history(), [])

    def test_nonempty_different_destination_refused(self):
        s, _ = self.populated()
        dst = self.store("b.db")
        dst.propose("local rule")
        before = dst.history()
        with self.assertRaises(pm.ConflictError):
            dst.restore_markdown(s.export_markdown())
        self.assertEqual(dst.history(), before)

    def test_invalid_existing_databases_left_untouched(self):
        junk, raw = self.path("junk.db"), b"definitely not sqlite " * 300
        with open(junk, "wb") as fh:
            fh.write(raw)
        with self.assertRaises(pm.StorageError):
            pm.MemoryStore(junk)
        with open(junk, "rb") as fh:
            self.assertEqual(fh.read(), raw)
        specs = {"foreign.db": ("CREATE TABLE stock(sku TEXT)", 7),
                 "future.db": ("CREATE TABLE events(seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,"
                               " payload TEXT NOT NULL)", 2)}
        for name, (ddl, ver) in specs.items():
            with closing(sqlite3.connect(self.path(name))) as con:
                con.execute(ddl); con.execute(f"PRAGMA user_version = {ver}"); con.commit()
            with self.assertRaises(pm.StorageError):
                self.store(name)
            with closing(sqlite3.connect(self.path(name))) as con:
                self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], ver)
        self.store("rows.db").propose("fine")
        with closing(sqlite3.connect(self.path("rows.db"))) as con:
            con.execute("UPDATE events SET payload = '{bad'"); con.commit()
        with self.assertRaises(pm.StorageError):
            self.store("rows.db")

    def test_backup_failure_after_commit_reports_saved_and_recovers(self):
        bp = self.path("memory.md")
        with open(bp, "w", encoding="utf-8", newline="") as fh:
            fh.write("old notes\n")
        s = self.store(backup="memory.md")
        self.assertEqual(read(bp), "old notes\n")  # constructor never overwrites backup
        with mock.patch.object(pm.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(pm.BackupError) as cm:
                s.propose("Saved despite mirror failure")
            eid = cm.exception.entry_id
            with self.assertRaises(pm.BackupError):
                s.confirm(eid)
        self.assertTrue(cm.exception.saved)
        self.assertEqual(read(bp), "old notes\n")
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])
        again = self.store(backup="memory.md")
        self.assertEqual([(r["id"], r["status"]) for r in again.entries()], [(eid, "confirmed")])
        self.assertEqual(again.confirm(eid), "confirmed")  # replay retries mirror
        dst = self.store("restored.db")
        self.assertEqual(dst.restore_markdown(read(bp)), "restored")
        self.assertEqual(dst.history(), again.history())

    def test_concurrent_instances_produce_full_mirror(self):
        stores = [self.store(backup="m.md") for _ in range(2)]
        errors = []
        def work(st, tag):
            try:
                for n in range(10):
                    st.propose(f"{tag} rule {n}")
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)
        threads = [threading.Thread(target=work, args=(st, f"w{i}")) for i, st in enumerate(stores)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        dst = self.store("copy.db")
        dst.restore_markdown(read(self.path("m.md")))
        self.assertEqual(len(dst.history()), 20)
        self.assertEqual(dst.history(), stores[0].history())


if __name__ == "__main__":
    unittest.main()
