"""SuperChat registry (SQLite).

Two separate things live here:
  1. chat_state - Gmail-style organisation of chats: active | kept | deleted (+ 'purged' tombstones).
                  A chat with no row is 'active'. Keeping a chat never writes anything to Obsidian.
  2. knowledge  - curated knowledge items extracted from a chat (0..n per chat): draft -> approved -> submitted.
                  Only approved items are written to Obsidian.
The v1 'keeps' table is never dropped; it is migrated once (schema_version 1 -> 2) inside one transaction.
CASS and the original agent chat files are never modified.
"""
import json
import os
import sqlite3
import time

SCHEMA = """
create table if not exists keeps (
  id integer primary key autoincrement, path text not null, agent text, title text, project text,
  scope text not null, message_ids text, client text, product text, category text, note text,
  status text not null, draft text, note_path text, content_hash text, created_at integer, updated_at integer);
create table if not exists catalog (client text not null, product text not null default '', primary key (client, product));
create table if not exists settings (key text primary key, value text);
create table if not exists audit (id integer primary key autoincrement, ts integer, actor text, action text, target text, detail text);

create table if not exists chat_state (
  path text primary key,
  state text not null check (state in ('active','kept','deleted','purged')),
  prev_state text, agent text, title text,
  kept_at integer, deleted_at integer, updated_at integer);
create table if not exists knowledge (
  id integer primary key autoincrement,
  path text not null, agent text, chat_title text,
  client text, product text, category text, title text,
  draft text not null,
  status text not null check (status in ('draft','approved','submitted','discarded')),
  score integer, note_path text, batch text,
  created_at integer, updated_at integer);
create index if not exists knowledge_path on knowledge(path);
"""

STATES = ("active", "kept", "deleted")


class Registry:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.con = sqlite3.connect(path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self._migrate_v2()

    def _log(self, actor, action, target, detail=None, now=None):
        self.con.execute("insert into audit(ts,actor,action,target,detail) values(?,?,?,?,?)",
                         (now or int(time.time()), actor, action, str(target),
                          json.dumps(detail, ensure_ascii=False) if detail is not None else None))

    # ------------------------------------------------------------ migration
    def _migrate_v2(self):
        if self.setting("schema_version", 1) >= 2:
            return
        now = int(time.time())
        with self.con:  # single transaction: all or nothing
            for k in [dict(r) for r in self.con.execute("select * from keeps order by id")]:
                if k["status"] in ("kept", "extracted", "approved", "submitted"):
                    self.con.execute("insert or ignore into chat_state(path,state,prev_state,agent,title,kept_at,updated_at) "
                                     "values(?, 'kept', 'active', ?, ?, ?, ?)", (k["path"], k["agent"], k["title"], k["updated_at"] or now, now))
                if k.get("draft") and k["status"] in ("extracted", "approved", "submitted"):
                    d = json.loads(k["draft"])
                    self.con.execute(
                        "insert into knowledge(path,agent,chat_title,client,product,category,title,draft,status,score,note_path,batch,"
                        "created_at,updated_at) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (k["path"], k["agent"], k["title"], k["client"], k["product"], d.get("category"), d.get("title"), k["draft"],
                         {"extracted": "draft"}.get(k["status"], k["status"]), d.get("_score"), k["note_path"], f"v1-{k['id']}",
                         k["created_at"] or now, now))
            self.con.execute("insert into settings(key,value) values('schema_version','2') on conflict(key) do update set value='2'")
            self._log("system", "migrate", "schema", {"to": 2}, now)

    # ------------------------------------------------------------ settings / audit / catalog
    def setting(self, key, default=None):
        r = self.con.execute("select value from settings where key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    def set_setting(self, key, value, actor="user"):
        with self.con:
            self.con.execute("insert into settings(key,value) values(?,?) on conflict(key) do update set value=excluded.value",
                             (key, json.dumps(value)))
            self._log(actor, "setting", key, value)

    def audit(self, actor, action, target, detail=None):
        with self.con:
            self._log(actor, action, target, detail)

    def add_catalog(self, client, product=""):
        client, product = (client or "").strip(), (product or "").strip()
        if client:
            with self.con:
                self.con.execute("insert or ignore into catalog(client,product) values(?,?)", (client, product))

    def catalog(self):
        out = {}
        for r in self.con.execute("select client, product from catalog order by client, product"):
            out.setdefault(r["client"], [])
            if r["product"]:
                out[r["client"]].append(r["product"])
        return out

    # ------------------------------------------------------------ chat state (Gmail-style)
    def states(self):
        return {r["path"]: r["state"] for r in self.con.execute("select path, state from chat_state")}

    def state_of(self, path):
        r = self.con.execute("select state from chat_state where path=?", (path,)).fetchone()
        return r["state"] if r else "active"

    def set_state(self, actor, path, state, meta=None):
        """Idempotent move between active/kept/deleted. Returns (state, previous, changed)."""
        if state not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        meta, now = meta or {}, int(time.time())
        with self.con:
            prev = self.state_of(path)
            if prev == "purged":
                raise ValueError("this chat was permanently deleted from SuperChat")
            if prev == state:
                return state, prev, False
            self.con.execute(
                "insert into chat_state(path,state,prev_state,agent,title,kept_at,deleted_at,updated_at) values(?,?,?,?,?,?,?,?) "
                "on conflict(path) do update set state=excluded.state, prev_state=excluded.prev_state, "
                "kept_at=case when excluded.state='kept' then excluded.kept_at else chat_state.kept_at end, "
                "deleted_at=excluded.deleted_at, updated_at=excluded.updated_at, "
                "agent=coalesce(chat_state.agent, excluded.agent), title=coalesce(chat_state.title, excluded.title)",
                (path, state, prev, meta.get("agent"), meta.get("title"), now if state == "kept" else None,
                 now if state == "deleted" else None, now))
            self._log(actor, f"chat:{prev}->{state}", path, None, now)
        return state, prev, True

    def purge(self, actor, path):
        """Permanently remove a chat from SuperChat: tombstone (it never reappears) + delete its unapproved drafts.
        Approved knowledge notes, CASS's index and the original agent files are NOT touched."""
        now = int(time.time())
        with self.con:
            if self.state_of(path) != "deleted":
                raise ValueError("only chats in Deleted can be permanently deleted")
            n = self.con.execute("delete from knowledge where path=? and status in ('draft','discarded')", (path,)).rowcount
            self.con.execute("update chat_state set state='purged', prev_state='deleted', title=null, updated_at=? where path=?", (now, path))
            self._log(actor, "chat:purged", path, {"drafts_deleted": n}, now)
        return n

    # ------------------------------------------------------------ knowledge items
    def add_drafts(self, actor, path, meta, items, client="", product="", usage=None):
        now, batch = int(time.time()), f"b{int(time.time() * 1000)}"
        ids = []
        with self.con:
            # a new extraction replaces this chat's earlier, still-unreviewed drafts
            self.con.execute("update knowledge set status='discarded', updated_at=? where path=? and status='draft'", (now, path))
            for it in items:
                c = self.con.execute(
                    "insert into knowledge(path,agent,chat_title,client,product,category,title,draft,status,score,batch,created_at,updated_at) "
                    "values(?,?,?,?,?,?,?,?,'draft',?,?,?,?)",
                    (path, meta.get("agent"), meta.get("title"), client, product, it.get("category"), it.get("title"),
                     json.dumps(it, ensure_ascii=False), it.get("_score"), batch, now, now))
                ids.append(c.lastrowid)
            self._log(actor, "knowledge:extracted", path, {"items": len(items), "batch": batch, **(usage or {})}, now)
        return ids

    def knowledge(self, kid):
        r = self.con.execute("select * from knowledge where id=?", (kid,)).fetchone()
        return dict(r) if r else None

    def knowledge_list(self, status=None, path=None):
        q, args = "select * from knowledge where status != 'discarded'", []
        if status:
            q, args = q + " and status=?", args + [status]
        if path:
            q, args = q + " and path=?", args + [path]
        return [dict(r) for r in self.con.execute(q + " order by updated_at desc, id", args)]

    def update_knowledge(self, actor, kid, status, **fields):
        allowed = {"draft", "note_path", "client", "product", "category", "title", "score"}
        sets, vals = ["status=?", "updated_at=?"], [status, int(time.time())]
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            sets.append(f"{k}=?")
            vals.append(json.dumps(v, ensure_ascii=False) if k == "draft" and not isinstance(v, str) else v)
        with self.con:
            self.con.execute(f"update knowledge set {', '.join(sets)} where id=?", (*vals, kid))
            self._log(actor, f"knowledge:{status}", kid)
