"""Obsidian vault writer for SuperChat.

Layout (Phase 1 = employee area only; Shared-Knowledge is filled by the admin approval step in Phase 5):
  SuperChat/
    Employees/<Employee>/<Client>/<Product>/<Category folder>/<date> <title>.md
    Employees/<Employee>/<Client>/Progress.md
    Shared-Knowledge/<Client>/<Product>/...      (admin-approved, later)
    Index/Knowledge-Registry.md, Index/Projects.md
"""
import os
import re
import shutil
import time
from datetime import datetime

FOLDERS = {
    "bug_solution": "Bugs-and-Solutions", "debugging": "Debugging", "qa": "Questions-and-Answers",
    "feedback": "Feedback-and-Improvements", "decision": "Technical-Decisions", "architecture": "Architecture",
    "best_practice": "Best-Practices", "unresolved": "Unresolved-Issues", "progress": "Progress-Notes",
}
LABEL = {"verified": "✅ Verified", "partially_verified": "🟡 Partially verified", "unverified": "⚠️ Unverified"}


def safe(name, n=90):
    name = re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:n].strip(" .") or "untitled"


def yq(s):
    return '"' + str(s or "").replace("\\", "\\\\").replace('"', "'").replace("\n", " ") + '"'


def init(vault):
    for d in ("Employees", "Shared-Knowledge", "Index", "Templates"):
        os.makedirs(os.path.join(vault, d), exist_ok=True)
    readme = os.path.join(vault, "README.md")
    if not os.path.exists(readme):
        open(readme, "w", encoding="utf-8").write(
            "# SuperChat Knowledge Vault\n\n- **Employees/** - knowledge each employee kept and approved, by client and product.\n"
            "- **Shared-Knowledge/** - admin-approved knowledge for the whole team (Phase 5).\n"
            "- **Index/** - [[Knowledge-Registry]] and [[Projects]] (auto-generated).\n\n"
            "Notes are written by SuperChat. Edit them freely; SuperChat never overwrites an approved note, it adds a new version.\n")


def _section(title, body):
    return f"## {title}\n\n{body.strip()}\n\n" if body and str(body).strip() else ""


def _bullets(items):
    return "\n".join(f"- {i}" for i in items if str(i).strip())


def render(rec, keep, employee, kid, score):
    v = rec.get("verification") or {}
    today = datetime.now().strftime("%Y-%m-%d")
    tags = ["superchat", rec.get("category", "")] + [re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-") for t in rec.get("technologies", [])][:8]
    fm = ["---", f"knowledge_id: SC-{kid:05d}", f"title: {yq(rec.get('title'))}", f"client: {yq(keep.get('client'))}",
          f"product: {yq(keep.get('product'))}", f"category: {rec.get('category')}",
          f"technologies: [{', '.join(yq(t) for t in rec.get('technologies', []))}]",
          f"tags: [{', '.join(t for t in tags if t)}]", f"contributor: {yq(employee)}",
          f"source_agent: {keep.get('agent')}", f"source_session: {yq(keep.get('path'))}",
          f"source_messages: {rec.get('source_messages', [])}", f"keep_scope: {keep.get('scope')}",
          f"date_discovered: {today}", f"date_updated: {today}",
          f"verification: {v.get('status', 'unverified')}", "review_status: approved-by-contributor",
          "sharing_status: private", f"quality_score: {score}", "---", ""]
    out = [f"# {rec.get('title')}\n", f"> {LABEL.get(v.get('status'), '⚠️ Unverified')} · **{keep.get('client')}**"
           + (f" / {keep.get('product')}" if keep.get('product') else "") + f" · score {score}/100 · by {employee}\n\n"]
    out.append(_section("Summary", rec.get("summary")))
    out.append(_section("Question / problem", rec.get("problem")))
    out.append(_section("Context", rec.get("context")))
    if rec.get("errors"):
        out.append(_section("Symptoms and error messages", "\n".join(f"```\n{e}\n```" for e in rec["errors"])))
    out.append(_section("Root cause", rec.get("root_cause")))
    if rec.get("attempts"):
        out.append(_section("Attempts and investigation", "\n".join(
            f"- **{a.get('action')}** → {a.get('result')}" + (f" *(failed because: {a['why_failed']})*" if a.get("why_failed") else "")
            for a in rec["attempts"])))
    out.append(_section("Final solution" if v.get("status") == "verified" else "Current best answer (not fully verified)", rec.get("solution")))
    if rec.get("implementation"):
        out.append(_section("Technical implementation", "\n\n".join(
            (f"{i.get('note')}\n" if i.get("note") else "") + f"```{i.get('language') or ''}\n{i.get('content')}\n```"
            for i in rec["implementation"])))
    if rec.get("files_affected"):
        out.append(_section("Files affected", _bullets(f"`{f}`" for f in rec["files_affected"])))
    out.append(_section("Verification", f"**{LABEL.get(v.get('status'), v.get('status'))}** - {v.get('evidence') or 'no evidence recorded'}"))
    if rec.get("decisions"):
        out.append(_section("Decisions", "\n".join(
            f"- **{d.get('decision')}**" + (f" - alternatives: {d['alternatives']}" if d.get("alternatives") else "")
            + (f" - why: {d['reason']}" if d.get("reason") else "") for d in rec["decisions"])))
    out.append(_section("Feedback and improvements", _bullets(rec.get("feedback", []))))
    out.append(_section("Lessons learned", _bullets(rec.get("lessons", []))))
    out.append(_section("Unresolved", _bullets(rec.get("unresolved", []))))
    out.append(_section("Missing information", _bullets(rec.get("missing_info", []))))
    proj = f"[[{safe(keep.get('client'))} - {safe(keep.get('product') or 'General')}]]"
    out.append(_section("Related", proj))
    out.append(_section("Source", f"- Agent: {keep.get('agent')}\n- Session: `{keep.get('path')}`\n"
                                  f"- Messages: {', '.join('#' + str(i) for i in rec.get('source_messages', [])) or 'whole chat'}\n"
                                  f"- Kept by {employee} ({keep.get('scope')})"))
    return "\n".join(fm) + "".join(out)


def note_path(vault, employee, keep, rec):
    folder = os.path.join(vault, "Employees", safe(employee), safe(keep.get("client")), safe(keep.get("product") or "General"),
                          FOLDERS.get(rec.get("category"), "Notes"))
    name = safe(f"{datetime.now().strftime('%Y-%m-%d')} {rec.get('title')}")
    return os.path.join(folder, name + ".md")


def write_note(vault, employee, keep, rec, kid, score):
    """Write a new note. If a note already exists at that path, keep the old one as a version (never overwrite)."""
    path = note_path(vault, employee, keep, rec)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        hist = os.path.join(os.path.dirname(path), ".versions")
        os.makedirs(hist, exist_ok=True)
        shutil.copy2(path, os.path.join(hist, f"{os.path.basename(path)[:-3]} v{int(time.time())}.md"))
    open(path, "w", encoding="utf-8").write(render(rec, keep, employee, kid, score))
    _append_progress(vault, employee, keep, rec, path)
    return path


def _append_progress(vault, employee, keep, rec, path):
    p = os.path.join(vault, "Employees", safe(employee), safe(keep.get("client")), "Progress.md")
    if not os.path.exists(p):
        open(p, "w", encoding="utf-8").write(f"# {keep.get('client')} - progress ({employee})\n\n")
    link = os.path.splitext(os.path.basename(path))[0]
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"- {datetime.now().strftime('%Y-%m-%d')} · {keep.get('product') or 'General'} · {rec.get('category')} · [[{link}]]\n")


def tokens(s):
    return {w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())} - {"the", "and", "for", "with", "not", "from", "this", "that"}


def similarity(a, b):
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def find_similar(registry_rows, rec, keep, threshold=0.45):
    """Compare against already-approved knowledge for the same client (title + problem + errors)."""
    probe = " ".join([rec.get("title", ""), rec.get("problem", ""), " ".join(rec.get("errors", []))])
    out = []
    for r in registry_rows:
        if (keep.get("client") and r["client"] != keep.get("client")) or not r.get("draft"):
            continue
        import json
        d = json.loads(r["draft"])
        sim = similarity(probe, " ".join([d.get("title", ""), d.get("problem", ""), " ".join(d.get("errors", []))]))
        if sim >= threshold:
            out.append({"id": r["id"], "title": d.get("title"), "similarity": round(sim, 2),
                        "verification": (d.get("verification") or {}).get("status"), "note_path": r.get("note_path")})
    return sorted(out, key=lambda x: -x["similarity"])[:5]


def rebuild_index(vault, approved_rows, employee):
    import json
    rows = []
    for r in approved_rows:
        d = json.loads(r["draft"]) if r.get("draft") else {}
        rows.append((r["client"], r["product"] or "General", d.get("category", ""), d.get("title", r.get("title")),
                     (d.get("verification") or {}).get("status", ""), os.path.splitext(os.path.basename(r.get("note_path") or ""))[0],
                     r.get("status")))
    rows.sort()
    reg = ["# Knowledge Registry", "", f"_Auto-generated {datetime.now().strftime('%d %b %Y %H:%M')} · {len(rows)} items_", "",
           "| Client | Product | Category | Knowledge | Verification | Status |", "|---|---|---|---|---|---|"]
    reg += [f"| {c} | {p} | {cat} | [[{n}|{t}]] | {LABEL.get(v, v)} | {s} |" for c, p, cat, t, v, n, s in rows]
    open(os.path.join(vault, "Index", "Knowledge-Registry.md"), "w", encoding="utf-8").write("\n".join(reg) + "\n")
    projects = {}
    for c, p, cat, t, v, n, s in rows:
        projects.setdefault((c, p), []).append((cat, t, v, n))
    pj = ["# Projects", ""]
    for (c, p), items in sorted(projects.items()):
        pj.append(f"- [[{safe(c)} - {safe(p)}]] · {len(items)} items")
        hub = [f"# {c} / {p}", "", f"Knowledge kept for **{c}** - **{p}**.", ""]
        for cat in sorted({i[0] for i in items}):
            hub += [f"## {FOLDERS.get(cat, cat)}", ""] + [f"- [[{n}|{t}]] · {LABEL.get(v, v)}" for cc, t, v, n in items if cc == cat] + [""]
        open(os.path.join(vault, "Index", f"{safe(c)} - {safe(p)}.md"), "w", encoding="utf-8").write("\n".join(hub))
    open(os.path.join(vault, "Index", "Projects.md"), "w", encoding="utf-8").write("\n".join(pj) + "\n")
