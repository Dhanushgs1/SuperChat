# SuperChat (Phase 1 prototype)

Keep what matters from your AI coding chats and turn it into reviewed, reusable knowledge.

```
Claude Code · Cursor · Codex · Gemini · Hermes · Antigravity
                 │  (CASS indexes chats locally, read-only)
                 ▼
SuperChat (127.0.0.1:8790)  ── Keep whole chat / selected messages + client + product
                 │            registry: data/superchat.db (keeps, catalog, settings, audit)
                 ▼  Extract (only after consent; redacted, selected messages only; Claude runs with no tools)
       Knowledge draft (problem, root cause, attempts, fix, verification, score)
                 │  employee reviews + edits + approves
                 ▼
Obsidian vault  Documents/SuperChat/Employees/<Name>/<Client>/<Product>/<Type>/...
                 │  "Submit for team sharing" → admin queue (Phase 5: Supabase + RLS)
                 ▼
Shared-Knowledge/<Client>/...  (admin-approved, Phase 5)
```

## Run

- Double-click `launch.bat`, or ask Hermes `/launch`.
- Opens http://127.0.0.1:8790 (this computer only).

## What Phase 1 does

| Feature | Status |
|---|---|
| Browse all agent chats (CASS), filter by agent / kept / not kept | ✅ |
| Keep whole chat or ticked messages, with client, product, type | ✅ |
| Unkeep (registry only - original chat untouched) | ✅ |
| Claude extraction with consent gate, redaction before and after, no tools | ✅ |
| Quality score (7 criteria, 0-100; unverified capped at 79) | ✅ |
| Review / edit / approve → Obsidian note, never overwrites (old versions kept in `.versions/`) | ✅ |
| Similar-knowledge warning before approve | ✅ (token similarity; semantic later) |
| Knowledge-Registry, Projects and per-product hub notes | ✅ |
| Hybrid search: approved notes (trusted) + raw CASS chats (evidence) | ✅ |
| Audit log of every keep / extract / approve / setting | ✅ |
| Submit for team sharing → admin queue | ✅ (status only) |
| Curated CASS archive (only kept chats indexed) | Phase 3 - needs testing of CASS `--data-dir` + exclusions |
| Supabase sync with per-client row-level security + pgvector | Phase 5 |
| Central AI answers / KT generator | Phase 6-7 |

## Privacy and security

- Server binds to 127.0.0.1, checks the Host header, and requires a custom header on every write (blocks other websites).
- Before anything reaches Claude: CASS export redaction + SuperChat redaction (keys, tokens, passwords, connection
  strings, emails, phone numbers, public IPs, configurable customer names). Applied again to the AI output.
- Only the kept messages are sent. Transcript text is fenced as untrusted data; Claude runs with `--tools ""`,
  `--strict-mcp-config`, `--no-session-persistence`.
- Approve is blocked if secret-looking text remains.
- Obsidian has no permissions: per-client access for the team must come from Supabase RLS (Phase 5), not from a shared vault.

## Tests

```
python -m unittest tests.test_core -v              # 9 offline tests
set SC_LIVE_CLAUDE=1 & python -m unittest tests.test_core -v   # + 1 live Claude call on synthetic data
```

## Files

- `server.py` - web server + API
- `sc/registry.py` - keep/approval registry and audit
- `sc/extract.py` - extraction prompt, JSON schema, Claude CLI call, scoring
- `sc/vault.py` - Obsidian note rendering, versioning, index notes, similarity
- `sc/privacy.py`, `sc/transcript.py`, `sc/cassapi.py` - redaction, transcript parsing, CASS wrappers
- `static/index.html` - the UI
