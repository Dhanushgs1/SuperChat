"""SuperChat - local web app (127.0.0.1 only).

Gmail-style chat organisation (All / Kept / Deleted, one click, Undo) on top of CASS, plus a separate
"Extract valuable knowledge" flow: a chat can yield 0..n knowledge drafts; only approved ones go to Obsidian.
Raw chats never leave this PC unless you click Extract and confirm.
Usage: python server.py [port] [--no-open]
"""
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sc import cassapi, extract, privacy, transcript, vault  # noqa: E402
from sc.registry import Registry  # noqa: E402

PORT = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8790)
CASS = os.path.join(os.path.expanduser("~"), ".local", "bin", "cass.exe")
DATA = os.environ.get("SC_DATA") or os.path.join(HERE, "data")
VAULT = os.environ.get("SC_VAULT") or os.path.join(os.path.expanduser("~"), "Documents", "SuperChat")
REG = Registry(os.path.join(DATA, "superchat.db"))
CACHE = os.path.join(DATA, "sessions_cache.json")
SEED = os.path.join(os.path.expanduser("~"), "ai-engineer-dashboard", "ai-memory", "sessions_cache.json")
CFG = SimpleNamespace(cass=CASS, raw={"privacy": {"redact_emails": True, "redact_phones": True, "redact_ips": True}})
STATE = {"sessions": [], "refreshing": False, "loaded_at": 0}
LOCK = threading.Lock()
VIEWS = {"all": "active", "kept": "kept", "deleted": "deleted"}


def employee():
    return REG.setting("employee", "Dhanush")


# ------------------------------------------------------------------ sessions (from CASS)
def _clean_title(t):
    t = re.sub(r"<[^>]+>", " ", t or "")
    t = re.sub(r'@"[^"]*(?:"|$)', "", t)
    return privacy.redact(re.sub(r"\s+", " ", t).strip(" -:")[:120], CFG)[0] or "(untitled chat)"


def load_sessions():
    for p in (CACHE, SEED):
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
                for s in d["sessions"]:
                    s["title"] = privacy.redact(s.get("title") or "", CFG)[0] or "(untitled chat)"
                STATE["sessions"], STATE["loaded_at"] = d["sessions"], d.get("loaded_at", 0)
                return
            except Exception:
                pass


def refresh_sessions():
    """Discover new chats via `cass sessions` (slow, ~1-2 min); keeps already-known titles."""
    with LOCK:
        if STATE["refreshing"]:
            return
        STATE["refreshing"] = True
    try:
        raw = cassapi.sessions(CASS)
        known = {s["path"]: s for s in STATE["sessions"]}
        out = []
        for s in raw:
            p = s.get("path") or ""
            old = known.get(p, {})
            out.append({"path": p, "agent": s.get("agent") or "other",
                        "title": old.get("title") if old.get("title") not in (None, "(untitled chat)") else _clean_title(s.get("title")),
                        "project": old.get("project") or os.path.basename((s.get("workspace") or "").rstrip("\\/")) or "Home folder",
                        "modified": old.get("modified") or _ms(s.get("modified")), "messages": s.get("message_count") or 0,
                        "sub": "/subagents/" in p.replace("\\", "/")})
        out.sort(key=lambda x: x["modified"] or 0, reverse=True)
        now = int(time.time() * 1000)
        STATE["sessions"], STATE["loaded_at"] = out, now
        json.dump({"sessions": out, "loaded_at": now}, open(CACHE, "w", encoding="utf-8"))
    except Exception as e:
        print("refresh failed:", e)
    finally:
        STATE["refreshing"] = False


def _ms(v):
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def session_meta(path):
    return next((s for s in STATE["sessions"] if s["path"] == path), None)


def session_messages(path):
    md = transcript.load_markdown(CFG, path)
    msgs = transcript.parse(md)
    for m in msgs:
        m["text"], _ = privacy.redact(m["text"], CFG)
    return msgs


def list_sessions(view, agent="", text="", helpers=False):
    states = REG.states()
    counts = {"all": 0, "kept": 0, "deleted": 0}
    out, want = [], VIEWS.get(view, "active")
    text = text.lower()
    for s in STATE["sessions"]:
        st = states.get(s["path"], "active")
        if st == "purged" or (s.get("sub") and not helpers):
            continue
        counts[{"active": "all", "kept": "kept", "deleted": "deleted"}[st]] += 1
        if st != want or (agent and s["agent"] != agent):
            continue
        if text and text not in (s["title"] + " " + (s.get("project") or "")).lower():
            continue
        out.append({**s, "state": st})
    return out, counts


# ------------------------------------------------------------------ knowledge actions
def _decode(r):
    r = dict(r)
    r["draft"] = json.loads(r["draft"]) if r.get("draft") else None
    return r


def act_extract(path, client="", product="", category=""):
    meta = session_meta(path)
    if not meta:
        raise LookupError("unknown chat")
    if REG.state_of(path) in ("deleted", "purged"):
        raise ValueError("restore this chat before extracting from it")
    if not REG.setting("consent_claude", False):
        raise PermissionError("consent_required")
    msgs = [m for m in session_messages(path)]
    if not any(not m["tool"] for m in msgs):
        raise ValueError("this chat has no readable messages")
    prompt = extract.build_prompt(msgs, {"client": client, "product": product, "category": category})
    out, usage = extract.run_claude(prompt)
    out = json.loads(privacy.redact(json.dumps(out, ensure_ascii=False), CFG)[0])  # second privacy pass on AI output
    items = []
    for it in out.get("items", []):
        if not (it.get("title") or "").strip():
            continue
        it["_score"] = extract.quality_score(it)
        it["_band"] = extract.band(it["_score"])
        items.append(it)
    # duplicate detection: inside this batch and against approved knowledge
    approved = REG.knowledge_list("approved") + REG.knowledge_list("submitted")
    for i, it in enumerate(items):
        it["_similar"] = vault.find_similar(approved, it, {"client": client}, threshold=0.45)
        it["_dup_in_batch"] = [j for j in range(i) if vault.similarity(it.get("title", "") + " " + it.get("problem", ""),
                                                                         items[j].get("title", "") + " " + items[j].get("problem", "")) >= 0.8]
    ids = REG.add_drafts(employee(), path, meta, items, client, product,
                         {"chars_sent": len(prompt), **{k: v for k, v in usage.items() if v is not None}})
    if client:
        REG.add_catalog(client, product)
    return {"items": [_decode(REG.knowledge(i)) for i in ids], "skip_reason": out.get("skip_reason", ""), "usage": usage}


def act_approve(kid, draft, client, product):
    k = REG.knowledge(kid)
    if not k or k["status"] != "draft":
        raise ValueError("only drafts can be approved")
    client, product = (client or "").strip(), (product or "").strip()
    if not client:
        raise ValueError("choose a client before approving")
    if not (draft.get("title") or "").strip():
        raise ValueError("title is required")
    clean = {kk: v for kk, v in draft.items() if not kk.startswith("_")}
    clean = json.loads(privacy.redact(json.dumps(clean, ensure_ascii=False), CFG)[0])
    if privacy.residual_secrets(json.dumps(clean)):
        raise ValueError("note still contains secret-looking text; remove it before approving")
    score = extract.quality_score(clean)
    src = {"client": client, "product": product, "agent": k["agent"], "path": k["path"], "scope": "conversation"}
    path = vault.write_note(VAULT, employee(), src, clean, kid, score)
    clean["_score"] = score
    REG.update_knowledge(employee(), kid, "approved", draft=clean, note_path=path, client=client, product=product,
                         title=clean["title"], category=clean.get("category"), score=score)
    REG.add_catalog(client, product)
    vault.rebuild_index(VAULT, REG.knowledge_list("approved") + REG.knowledge_list("submitted"), employee())
    return {"note_path": path, "score": score}


# ------------------------------------------------------------------ search
def search(q):
    """Hybrid: approved knowledge (authoritative) + raw CASS chats (evidence). Purged chats are excluded."""
    notes, words = [], re.findall(r"\w{3,}", q.lower())
    for r in REG.knowledge_list("approved") + REG.knowledge_list("submitted"):
        d = json.loads(r["draft"])
        hits = sum(json.dumps(d, ensure_ascii=False).lower().count(w) for w in words)
        if hits:
            notes.append({"id": r["id"], "title": d.get("title"), "client": r["client"], "product": r["product"],
                          "category": d.get("category"), "verification": (d.get("verification") or {}).get("status"),
                          "summary": d.get("summary"), "note_path": r["note_path"], "hits": hits})
    notes.sort(key=lambda x: -x["hits"])
    chats, states = [], REG.states()
    try:
        d = cassapi.search(CASS, q, limit=40, mode="lexical")
        seen = set()
        for h in d.get("hits", []):
            p = h.get("source_path")
            if p in seen or states.get(p) == "purged":
                continue
            seen.add(p)
            m = session_meta(p) or {}
            chats.append({"path": p, "agent": h.get("agent"), "title": m.get("title") or _clean_title(h.get("title")),
                          "project": m.get("project"), "state": states.get(p, "active"),
                          "snippet": privacy.redact((h.get("snippet") or "")[:300], CFG)[0]})
    except Exception as e:
        chats = [{"error": str(e)[:200]}]
    return {"notes": notes[:20], "chats": chats[:20]}


# ------------------------------------------------------------------ http
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(b)

    def _guard(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._guard():
            return self._send(403, {"error": "forbidden"})
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, open(os.path.join(HERE, "static", "index.html"), "rb").read(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                _, counts = list_sessions("all", helpers=q.get("helpers") == "1")
                return self._send(200, {"employee": employee(), "catalog": REG.catalog(), "consent_claude": REG.setting("consent_claude", False),
                                        "vault": VAULT, "refreshing": STATE["refreshing"], "counts": counts,
                                        "drafts": len(REG.knowledge_list("draft")),
                                        "knowledge": len(REG.knowledge_list("approved")) + len(REG.knowledge_list("submitted"))})
            if u.path == "/api/sessions":
                out, counts = list_sessions(q.get("view", "all"), q.get("agent", ""), q.get("q", ""), q.get("helpers") == "1")
                return self._send(200, {"sessions": out[:500], "total": len(out), "counts": counts})
            if u.path == "/api/session":
                path = q.get("path", "")
                if not session_meta(path) or REG.state_of(path) == "purged":
                    return self._send(404, {"error": "unknown chat"})
                return self._send(200, {"meta": session_meta(path), "state": REG.state_of(path), "messages": session_messages(path),
                                        "knowledge": [_decode(r) for r in REG.knowledge_list(path=path)]})
            if u.path == "/api/knowledge":
                return self._send(200, {"items": [_decode(r) for r in REG.knowledge_list(q.get("status") or None)]})
            if u.path == "/api/search":
                qq = (q.get("q") or "").strip()
                return self._send(400, {"error": "empty"}) if not qq else self._send(200, search(qq))
            if u.path == "/api/audit":
                return self._send(200, {"audit": [dict(r) for r in REG.con.execute("select * from audit order by id desc limit 150")]})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)[:400]})

    def do_POST(self):
        # CSRF guard: same host + JSON + custom header (cross-site pages can't send it without a preflight we never allow)
        if not self._guard() or self.headers.get("X-SuperChat") != "1" or "json" not in (self.headers.get("Content-Type") or ""):
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            return self._send(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        u = urllib.parse.urlparse(self.path).path
        try:
            if u == "/api/chat/state":  # keep / unkeep / delete / restore / undo - all idempotent
                path = body.get("path", "")
                meta = session_meta(path)
                if not meta:
                    return self._send(404, {"error": "unknown chat"})
                state, prev, changed = REG.set_state(employee(), path, body.get("state", ""), meta)
                _, counts = list_sessions("all")
                return self._send(200, {"path": path, "state": state, "prev": prev, "changed": changed, "counts": counts})
            if u == "/api/chat/purge":
                path = body.get("path", "")
                if body.get("confirm") != "DELETE":
                    return self._send(400, {"error": "confirmation required"})
                n = REG.purge(employee(), path)
                _, counts = list_sessions("all")
                return self._send(200, {"ok": True, "drafts_deleted": n, "counts": counts})
            if u == "/api/extract":
                return self._send(200, act_extract(body.get("path", ""), body.get("client", "").strip(), body.get("product", "").strip(),
                                                   body.get("category", "")))
            if u == "/api/knowledge/approve":
                return self._send(200, act_approve(int(body["id"]), body.get("draft") or {}, body.get("client"), body.get("product")))
            if u == "/api/knowledge/discard":
                k = REG.knowledge(int(body["id"]))
                if not k or k["status"] != "draft":
                    return self._send(400, {"error": "only drafts can be discarded"})
                REG.update_knowledge(employee(), k["id"], "discarded")
                return self._send(200, {"ok": True})
            if u == "/api/knowledge/submit":
                k = REG.knowledge(int(body["id"]))
                if not k or k["status"] != "approved":
                    return self._send(400, {"error": "approve it first"})
                REG.update_knowledge(employee(), k["id"], "submitted")
                return self._send(200, {"ok": True, "note": "Queued for admin review. Central sharing (Supabase) arrives in Phase 5."})
            if u == "/api/consent":
                REG.set_setting("consent_claude", bool(body.get("value")), employee())
                return self._send(200, {"ok": True})
            if u == "/api/employee":
                REG.set_setting("employee", vault.safe(body.get("name", ""), 40), employee())
                return self._send(200, {"ok": True})
            if u == "/api/refresh":
                threading.Thread(target=refresh_sessions, daemon=True).start()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except PermissionError as e:
            return self._send(403, {"error": str(e)})
        except LookupError as e:
            return self._send(404, {"error": str(e)})
        except (ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)[:400]})


def serve(port=PORT, open_browser=True):
    os.makedirs(DATA, exist_ok=True)
    vault.init(VAULT)
    load_sessions()
    if not STATE["sessions"]:
        threading.Thread(target=refresh_sessions, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}/"
    print("SuperChat running at", url, flush=True)
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    serve(PORT, "--no-open" not in sys.argv)
