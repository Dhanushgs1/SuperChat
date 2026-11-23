"""Load and condense a session transcript (CASS markdown export; Hermes export as fallback)."""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile

from . import cassapi

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _hermes_export(session_id):
    hermes = shutil.which("hermes") or "hermes"
    tmp = tempfile.mkdtemp(prefix="tk_")
    try:
        subprocess.run([hermes, "sessions", "export", "--format", "md", "--redact", "--yes", "--session-id", session_id, tmp + os.sep],
                       capture_output=True, text=True, timeout=180, creationflags=NOWIN)
        fs = [f for f in os.listdir(tmp) if f.endswith(".md")]
        return open(os.path.join(tmp, fs[0]), encoding="utf-8", errors="replace").read() if fs else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def load_markdown(cfg, path):
    try:
        return cassapi.export_markdown(cfg.cass, path)
    except cassapi.CassError:
        m = re.search(r"state\.db[\\/](.+)$", path)
        if m:
            md = _hermes_export(m.group(1))
            if md:
                return md
        raise


def parse(md):
    """Return list of {i, role, text, tool} messages."""
    msgs = []
    if re.search(r"^## (👤 User|🤖 Assistant)", md, re.M):
        parts = re.split(r"^## (👤 User|🤖 Assistant|.+?)\s*$", md, flags=re.M)
        pairs = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
        role_of = lambda h: "user" if "User" in h else "assistant" if "Assistant" in h else "other"
    else:  # Hermes export layout: "### User — <ts>"
        parts = re.split(r"^###\s+(User|Assistant|Tool|Session_meta|System)\b[^\n]*$", md, flags=re.M)
        pairs = [(parts[i], parts[i + 1]) for i in range(1, len(parts) - 1, 2)]
        role_of = lambda h: {"User": "user", "Assistant": "assistant", "Tool": "tool"}.get(h, "other")
    for head, body in pairs:
        body = re.sub(r"(?:^|\n)\s*---\s*$", "", body.strip()).strip()
        body = re.sub(r"<system-reminder>[\s\S]*?</system-reminder>", "", body)
        body = re.sub(r"<image_files>[\s\S]*?</image_files>", "[image]", body)
        body = re.sub(r"</?user_query>|<timestamp>[\s\S]*?</timestamp>", "", body).strip()
        role = role_of(head)
        if not body or role == "other":
            continue
        lines = [l for l in body.splitlines() if l.strip()]
        tool = role == "tool" or (lines and all(l.strip().startswith("[Tool:") for l in lines))
        msgs.append({"i": len(msgs), "role": "assistant" if role == "tool" else role, "text": body, "tool": bool(tool)})
    return msgs


def condense(msgs, budget):
    """Render a compact, index-tagged transcript within a character budget.
    Keeps all user turns, assistant prose, and one-line tool steps; trims the middle if needed."""
    rendered = []
    for m in msgs:
        if m["tool"]:
            t = " | ".join(l.strip()[:160] for l in m["text"].splitlines() if l.strip())[:400]
            rendered.append(f"[#{m['i']} tool] {t}")
        else:
            rendered.append(f"[#{m['i']} {m['role']}]\n{m['text'][:6000]}")
    text = "\n\n".join(rendered)
    if len(text) <= budget:
        return text, False
    head, tail = rendered[: max(1, len(rendered) // 3)], rendered[-max(1, len(rendered) // 2):]
    while len("\n\n".join(head + tail)) > budget and (len(head) > 1 or len(tail) > 1):
        if len(head) >= len(tail) and len(head) > 1:
            head.pop()
        elif len(tail) > 1:
            tail.pop(0)
    return "\n\n".join(head) + "\n\n[... middle of conversation omitted for length ...]\n\n" + "\n\n".join(tail), True


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:16]
