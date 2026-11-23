"""Gmail-style workflow tests. Runs a real SuperChat server on a spare port against a TEMP data dir and TEMP vault
(the user's real registry and vault are never touched), using real CASS session metadata."""
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sc.registry import Registry  # noqa: E402

SEED = os.path.join(os.path.expanduser("~"), "ai-engineer-dashboard", "ai-memory", "sessions_cache.json")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Server:
    def __init__(self, data, vault):
        self.data, self.vault, self.port, self.p = data, vault, free_port(), None

    def start(self):
        env = dict(os.environ, SC_DATA=self.data, SC_VAULT=self.vault)
        self.p = subprocess.Popen([sys.executable, os.path.join(ROOT, "server.py"), str(self.port), "--no-open"], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(100):
            try:
                self.get("/api/state")
                return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self):
        if self.p:
            self.p.kill()
            self.p.wait()

    def get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=60) as r:
            return json.loads(r.read())

    def post(self, path, body, headers=True):
        h = {"Content-Type": "application/json", "X-SuperChat": "1"} if headers else {"Content-Type": "application/json"}
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


@unittest.skipUnless(os.path.exists(SEED), "needs the CASS sessions cache")
class Workflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sc_test_")
        cls.data, cls.vault = os.path.join(cls.tmp, "data"), os.path.join(cls.tmp, "vault")
        os.makedirs(cls.data)
        shutil.copy(SEED, os.path.join(cls.data, "sessions_cache.json"))
        cls.srv = Server(cls.data, cls.vault).start()
        cls.paths = [s["path"] for s in cls.srv.get("/api/sessions?view=all")["sessions"][:6]]

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def counts(self):
        return self.srv.get("/api/state")["counts"]

    def view(self, v):
        return [s["path"] for s in self.srv.get(f"/api/sessions?view={v}")["sessions"]]

    def setstate(self, path, state):
        code, r = self.srv.post("/api/chat/state", {"path": path, "state": state})
        self.assertEqual(code, 200, r)
        return r

    def test_1_keep_unkeep_delete_restore(self):
        p = self.paths[0]
        c0 = self.counts()
        r = self.setstate(p, "kept")
        self.assertEqual((r["prev"], r["state"], r["changed"]), ("active", "kept", True))
        self.assertNotIn(p, self.view("all"))
        self.assertIn(p, self.view("kept"))
        self.assertEqual(r["counts"]["all"], c0["all"] - 1)
        self.assertEqual(r["counts"]["kept"], c0["kept"] + 1)
        # idempotent
        r = self.setstate(p, "kept")
        self.assertFalse(r["changed"])
        self.assertEqual(r["counts"]["kept"], c0["kept"] + 1)
        # unkeep -> All
        self.setstate(p, "active")
        self.assertIn(p, self.view("all"))
        self.assertNotIn(p, self.view("kept"))
        # kept -> delete removes from All and Kept
        self.setstate(p, "kept")
        r = self.setstate(p, "deleted")
        self.assertEqual(r["prev"], "kept")
        self.assertNotIn(p, self.view("all"))
        self.assertNotIn(p, self.view("kept"))
        self.assertIn(p, self.view("deleted"))
        # restore -> All, NOT kept
        r = self.setstate(p, "active")
        self.assertIn(p, self.view("all"))
        self.assertNotIn(p, self.view("kept"))
        self.assertEqual(self.counts(), c0)

    def test_2_undo_returns_previous_state(self):
        p = self.paths[1]
        self.setstate(p, "kept")
        r = self.setstate(p, "deleted")          # user deletes a kept chat
        self.setstate(p, r["prev"])              # Undo = set back to 'prev'
        self.assertIn(p, self.view("kept"))
        self.setstate(p, "active")

    def test_3_bad_input(self):
        code, _ = self.srv.post("/api/chat/state", {"path": self.paths[0], "state": "archived"})
        self.assertEqual(code, 400)
        code, _ = self.srv.post("/api/chat/state", {"path": "C:/nope.jsonl", "state": "kept"})
        self.assertEqual(code, 404)
        code, _ = self.srv.post("/api/chat/state", {"path": self.paths[0], "state": "kept"}, headers=False)
        self.assertEqual(code, 403)  # CSRF guard

    def test_4_persists_after_restart(self):
        p_keep, p_del = self.paths[2], self.paths[3]
        self.setstate(p_keep, "kept")
        self.setstate(p_del, "deleted")
        self.srv.stop()
        self.srv.start()
        self.assertIn(p_keep, self.view("kept"))
        self.assertIn(p_del, self.view("deleted"))
        self.setstate(p_keep, "active")
        self.setstate(p_del, "active")

    def test_5_purge_scope(self):
        p = self.paths[4]
        orig = p if os.path.exists(p) else None
        h0 = hashlib.sha256(open(orig, "rb").read()).hexdigest() if orig else None
        code, _ = self.srv.post("/api/chat/purge", {"path": p, "confirm": "DELETE"})
        self.assertEqual(code, 400)  # must be in Deleted first
        self.setstate(p, "deleted")
        code, _ = self.srv.post("/api/chat/purge", {"path": p})
        self.assertEqual(code, 400)  # confirmation required
        con = sqlite3.connect(os.path.join(self.data, "superchat.db"))
        con.execute("insert into knowledge(path,draft,status,created_at,updated_at) values(?,?,?,0,0)", (p, "{}", "draft"))
        con.execute("insert into knowledge(path,draft,status,note_path,created_at,updated_at) values(?,?,?,?,0,0)", (p, "{}", "approved", "x.md"))
        con.commit()
        code, r = self.srv.post("/api/chat/purge", {"path": p, "confirm": "DELETE"})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["drafts_deleted"], 1)
        for v in ("all", "kept", "deleted"):
            self.assertNotIn(p, self.view(v))
        code, _ = self.srv.post("/api/chat/state", {"path": p, "state": "active"})
        self.assertEqual(code, 400)  # stays gone
        left = con.execute("select status from knowledge where path=?", (p,)).fetchall()
        self.assertEqual(left, [("approved",)])  # approved knowledge is kept
        con.close()
        if orig:
            self.assertEqual(hashlib.sha256(open(orig, "rb").read()).hexdigest(), h0)  # original chat file untouched
        self.srv.stop()
        self.srv.start()
        self.assertNotIn(p, self.view("all"))

    def test_6_keep_writes_nothing_to_obsidian(self):
        def files():
            return sorted(os.path.relpath(os.path.join(d, f), self.vault) for d, _, fs in os.walk(self.vault) for f in fs)
        before = files()
        self.setstate(self.paths[5], "kept")
        self.assertEqual(files(), before)
        code, r = self.srv.post("/api/extract", {"path": self.paths[5]})
        self.assertEqual((code, r.get("error")), (403, "consent_required"))  # nothing sent without consent
        self.setstate(self.paths[5], "active")


class Migration(unittest.TestCase):
    def test_v1_to_v2(self):
        tmp = tempfile.mkdtemp()
        try:
            db = os.path.join(tmp, "r.db")
            con = sqlite3.connect(db)
            con.executescript("""create table keeps (id integer primary key autoincrement, path text not null, agent text, title text, project text,
              scope text not null, message_ids text, client text, product text, category text, note text, status text not null, draft text,
              note_path text, content_hash text, created_at integer, updated_at integer);
              create table settings (key text primary key, value text);""")
            d = json.dumps({"title": "JWT fix", "category": "bug_solution", "_score": 88})
            rows = [("a", "kept", None, None), ("b", "extracted", d, None), ("c", "approved", d, "C:/v/n.md"), ("d", "unkept", None, None)]
            for p, st, dr, npth in rows:
                con.execute("insert into keeps(path,agent,title,scope,client,product,status,draft,note_path,created_at,updated_at) "
                            "values(?,?,?,?,?,?,?,?,?,1,1)", (p, "cursor", "t-" + p, "whole", "K", "B", st, dr, npth))
            con.commit()
            con.close()
            reg = Registry(db)
            self.assertEqual(reg.states(), {"a": "kept", "b": "kept", "c": "kept"})
            ks = {k["path"]: k["status"] for k in reg.knowledge_list()}
            self.assertEqual(ks, {"b": "draft", "c": "approved"})
            self.assertEqual(reg.con.execute("select count(*) from keeps").fetchone()[0], 4)  # v1 data kept
            reg.con.close()
            reg = Registry(db)  # second open: no duplicate migration
            self.assertEqual(len(reg.knowledge_list()), 2)
            reg.con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class KnowledgeFlow(unittest.TestCase):
    """Extraction -> drafts -> approve one / discard one, with Claude mocked (no data leaves the PC)."""

    def test_extract_approve_discard(self):
        tmp = tempfile.mkdtemp()
        try:
            os.environ["SC_DATA"], os.environ["SC_VAULT"] = os.path.join(tmp, "data"), os.path.join(tmp, "vault")
            import importlib
            import server as S
            S = importlib.reload(S)
            from sc import vault as V
            V.init(S.VAULT)
            path = "C:/fake/chat.jsonl"
            S.STATE["sessions"] = [{"path": path, "agent": "cursor", "title": "Login bug", "project": "app", "modified": 0, "messages": 3}]
            S.session_messages = lambda p: [{"i": 0, "role": "user", "text": "401 after deploy", "tool": False},
                                            {"i": 1, "role": "assistant", "text": "fixed: use REFRESH_SECRET; tests pass", "tool": False}]
            item = {"title": "JWT refresh returns 401", "category": "bug_solution", "summary": "s", "problem": "401 after deploy",
                    "solution": "use REFRESH_SECRET", "verification": {"status": "verified", "evidence": "tests pass"},
                    "source_messages": [0, 1], "scores": dict.fromkeys(("reusability", "specificity", "evidence", "clarity", "novelty", "completeness", "relevance"), 9)}
            S.extract.run_claude = lambda prompt, **kw: ({"items": [item, dict(item, title="Generic tip", category="best_practice")]}, {"cost_usd": 0})
            with self.assertRaises(PermissionError):
                S.act_extract(path)
            S.REG.set_setting("consent_claude", True)
            r = S.act_extract(path, client="", product="")
            self.assertEqual(len(r["items"]), 2)
            self.assertEqual(os.listdir(os.path.join(S.VAULT, "Employees")), [])  # drafts are not in Obsidian
            a, b = r["items"]
            with self.assertRaises(ValueError):
                S.act_approve(a["id"], a["draft"], "", "")  # client required
            out = S.act_approve(a["id"], a["draft"], "Company K", "Product B")
            self.assertTrue(os.path.exists(out["note_path"]))
            S.REG.update_knowledge("t", b["id"], "discarded")
            notes = [f for d, _, fs in os.walk(os.path.join(S.VAULT, "Employees")) for f in fs if f.endswith(".md") and f != "Progress.md"]
            self.assertEqual(len(notes), 1)
            self.assertEqual(S.REG.state_of(path), "active")  # extracting doesn't change the chat's Keep state
            # zero items is valid
            S.extract.run_claude = lambda prompt, **kw: ({"items": [], "skip_reason": "casual chat"}, {})
            r = S.act_extract(path)
            self.assertEqual((r["items"], r["skip_reason"]), ([], "casual chat"))
            S.REG.con.close()
        finally:
            os.environ.pop("SC_DATA", None)
            os.environ.pop("SC_VAULT", None)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
