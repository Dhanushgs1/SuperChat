"""Thin wrappers over documented CASS 0.11 CLI commands (robot/JSON mode only, never the TUI)."""
import json
import subprocess

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CassError(RuntimeError):
    pass


def _run(cass, args, timeout=120):
    p = subprocess.run([cass, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NOWIN)
    return p.returncode, p.stdout, p.stderr


def _json(out):
    i = out.find("{")
    if i < 0:
        raise CassError("no JSON in CASS output")
    return json.loads(out[i:])


def sessions(cass, limit=5000, timeout=900):
    code, out, err = _run(cass, ["sessions", "--robot-format", "compact", "--limit", str(limit)], timeout)
    if code != 0:
        raise CassError(f"cass sessions failed ({code}): {err[-300:]}")
    return _json(out).get("sessions", [])


def export_markdown(cass, path, timeout=120):
    """Redacted markdown transcript (CASS redacts recognised secrets by default)."""
    code, out, err = _run(cass, ["export", path, "--format", "markdown"], timeout)
    if code != 0 or not out.strip():
        raise CassError(f"cass export failed ({code}): {err[-300:]}")
    return out


def search(cass, query, limit=20, mode=None, timeout=60):
    args = ["search", query, "--robot", "--limit", str(limit), "--max-tokens", "20000"]
    if mode:
        args += ["--mode", mode]
    code, out, err = _run(cass, args, timeout)
    if code != 0:
        raise CassError(f"cass search failed ({code}): {err[-300:]}")
    return _json(out)


def index_incremental(cass, timeout=1800):
    code, _, err = _run(cass, ["index"], timeout)
    return code == 0, err[-300:]


def sources_sync(cass, timeout=3600):
    """Pull configured remote sources (additive-only rsync/scp/SFTP over SSH)."""
    code, out, err = _run(cass, ["sources", "sync", "--json"], timeout)
    return code, (out or err)[-2000:]


def health(cass):
    code, _, _ = _run(cass, ["health"], 30)
    return code == 0
