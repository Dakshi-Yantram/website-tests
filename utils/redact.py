"""Remove credentials, tokens and personal data from anything we log or report."""
import re
from config import secret_values

_JWT = re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}")
_BEARER = re.compile(r"(?i)(bearer\s+)[\w\-\.=]+")
_KV = re.compile(r"(?i)((?:password|passwd|pwd|token|secret|api[_-]?key|authorization)[\"']?\s*[:=]\s*[\"']?)([^\s&\"',}]+)")
_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE = re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)")


def redact(text) -> str:
    if text is None:
        return ""
    s = str(text)
    for secret in secret_values():
        if secret and len(secret) >= 3:
            s = s.replace(secret, "[REDACTED]")
    s = _JWT.sub("[JWT]", s)
    s = _BEARER.sub(r"\1[REDACTED]", s)
    s = _KV.sub(r"\1[REDACTED]", s)
    s = _EMAIL.sub(r"\1***@\2", s)
    s = _PHONE.sub("[PHONE]", s)
    return s
