"""Privacy filter: secret redaction, PII masking, prompt-injection hygiene. Applied before any AI sees text
and again before any note is written."""
import re

SECRET_PATTERNS = [
    r"sk-ant-[A-Za-z0-9_\-]{20,}", r"sk-(?:proj-)?[A-Za-z0-9]{32,}", r"gh[pousr]_[A-Za-z0-9]{30,}",
    r"github_pat_[A-Za-z0-9_]{40,}", r"AKIA[0-9A-Z]{16}", r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b",
    r"AIza[0-9A-Za-z_\-]{35}", r"xox[baprs]-[A-Za-z0-9\-]{10,}",
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s'\"<>]+",
    r"\b[A-Za-z0-9+/]{40,}={0,2}(?=\s|$|[\"'`])",   # long base64-ish blobs (keys, tokens)
]
SECRET_RE = [re.compile(p) for p in SECRET_PATTERNS]
KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|client[_-]?secret|api[_-]?key|access[_-]?token|auth[_-]?token|"
    r"refresh[_-]?token|bearer|token|private[_-]?key|service[_-]?role[_-]?key|anon[_-]?key)"
    r"(\s*[\"']?\s*[:=]\s*[\"']?|\s+)([^\s\"',;]{6,})")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b")
PHONE_RE = re.compile(r"(?<![\w.:/])\+?\d[\d \-()]{8,16}\d(?![\w.:/])")
IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
SAFE_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}


def redact(text, cfg=None):
    """Return (redacted_text, counts)."""
    if not text:
        return text, {}
    p = (cfg.raw.get("privacy", {}) if cfg else {}) or {}
    counts = {"secret": 0, "email": 0, "phone": 0, "ip": 0, "customer": 0}

    def sub(rx, repl, s, key):
        new, n = rx.subn(repl, s)
        counts[key] += n
        return new

    for rx in SECRET_RE:
        text = sub(rx, "[redacted-secret]", text, "secret")
    text, n = KV_RE.subn(lambda m: m.group(1) + m.group(2) + "[redacted-secret]", text)
    counts["secret"] += n
    if p.get("redact_emails", True):
        allowed = {d.lower() for d in p.get("allowed_email_domains", [])}

        def em(m):
            if m.group(1).lower() in allowed:
                return m.group(0)
            counts["email"] += 1
            return "[redacted-email]"
        text = EMAIL_RE.sub(em, text)
    if p.get("redact_ips", True):
        def ip(m):
            if m.group(0) in SAFE_IPS or m.group(0).startswith(("10.", "192.168.", "172.16.")):
                return m.group(0)
            counts["ip"] += 1
            return "[redacted-ip]"
        text = IPV4_RE.sub(ip, text)
    if p.get("redact_phones", True):
        def ph(m):
            digits = re.sub(r"\D", "", m.group(0))
            if 10 <= len(digits) <= 13 and not re.fullmatch(r"20\d{6,}", digits):  # skip dates/ids like 20261120...
                counts["phone"] += 1
                return "[redacted-phone]"
            return m.group(0)
        text = PHONE_RE.sub(ph, text)
    for term in p.get("customer_terms", []):
        if term:
            text = sub(re.compile(re.escape(term), re.I), "[redacted-customer]", text, "customer")
    return text, {k: v for k, v in counts.items() if v}


def neutralize_for_prompt(text):
    """Transcript text is untrusted data. Fence it so it cannot close our delimiters."""
    return text.replace("<<<", "<\u200b<<").replace(">>>", ">\u200b>>")


def residual_secrets(text):
    """Count secret-looking strings left after redaction (used as a publish gate)."""
    return sum(len(rx.findall(text)) for rx in SECRET_RE[:10])
