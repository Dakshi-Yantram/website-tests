"""Secret / personal-data detection and redaction. Standalone (no project config import).

Heuristic by nature: it catches common credential formats and common Indian patient identifiers.
It cannot prove that arbitrary free text is free of personal data. Callers therefore use
`assert_clean()` as a fail-closed last check on anything that is written to disk or sent anywhere.
"""
import os
import re

from .errors import SensitiveDataError

# ---- secret patterns (each is replaced by a fixed marker) ---------------------------------------
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_JWT = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}")
_GH_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_SLACK = re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")
_GOOGLE_KEY = re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]{8,}=*")
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_COOKIE = re.compile(r"(?i)\b(set-cookie|cookie)\s*:(?!\s*\[REDACTED\])\s*[^\r\n]+")
_KV = re.compile(
    r"(?i)((?<![A-Za-z])(?:password|passwd|pwd|passcode|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"client[_-]?secret|authorization|session(?:id)?|sid|auth)[\"']?\s*[:=]\s*[\"']?)(?!\[[A-Z-]+\])([^\s&\"',}\]]{3,})"
)
_DB_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://\S+")

# ---- personal / patient data (heuristic) --------------------------------------------------------
_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE = re.compile(r"(?<![\w.])(?:\+?91[\s-]?)?[6-9]\d{9}(?!\w)")
_AADHAAR = re.compile(r"(?<!\d)\d{4}[\s-]\d{4}[\s-]\d{4}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_PATIENT_ID = re.compile(r"(?i)\b(uhid|mrn|patient[\s_-]?id|ip[\s_-]?no|op[\s_-]?no|reg(?:istration)?[\s_-]?(?:no|number))\b(\s*[:#=-]?\s*)([A-Z0-9][A-Z0-9/-]{3,})")
_PATIENT_FIELD = re.compile(r"(?i)\b(patient(?:\s+name)?|dob|date of birth|diagnosis|prescription|medication)\b(?!\s*[:=]\s*\[[A-Z-]+\])(\s*[:=]\s*)([^\n,;|]{2,60})")

_PATTERNS = [
    ("[PRIVATE-KEY]", _PRIVATE_KEY), ("[JWT]", _JWT), ("[GITHUB-TOKEN]", _GH_TOKEN),
    ("[AWS-KEY]", _AWS_KEY), ("[SLACK-TOKEN]", _SLACK), ("[API-KEY]", _GOOGLE_KEY),
    ("[DB-URL]", _DB_URL),
]

# env var names whose VALUES must never appear anywhere (they are registered automatically)
_SECRET_ENV_NAME = re.compile(r"(?i)(password|passwd|secret|token|private_key|api_key|apikey|credential|cookie)")
_extra_secrets: set = set()


def register_secret(value) -> None:
    """Add a literal value that must be masked everywhere (e.g. a freshly minted token)."""
    if value and len(str(value)) >= 4:
        _extra_secrets.add(str(value))


def clear_registered_secrets() -> None:
    _extra_secrets.clear()


def _env_secret_values():
    for k, v in os.environ.items():
        if v and len(v) >= 4 and _SECRET_ENV_NAME.search(k) and not k.endswith(("_FILE", "_PATH", "_NAME")):
            yield v


def known_secret_values():
    return set(_extra_secrets) | set(_env_secret_values())


def redact(text, *, personal: bool = True) -> str:
    """Return `text` with secrets (and, by default, personal / patient identifiers) masked."""
    if text is None:
        return ""
    s = str(text)
    for v in sorted(known_secret_values(), key=len, reverse=True):
        s = s.replace(v, "[REDACTED]")
    for marker, rx in _PATTERNS:
        s = rx.sub(marker, s)
    s = _URL_CRED.sub(r"\1[REDACTED]@", s)
    s = _COOKIE.sub(lambda m: f"{m.group(1)}: [REDACTED]", s)
    s = _BEARER.sub(lambda m: f"{m.group(1)} [REDACTED]", s)
    s = _KV.sub(lambda m: f"{m.group(1)}[REDACTED]", s)
    if personal:
        s = _PATIENT_ID.sub(lambda m: f"{m.group(1)}{m.group(2)}[PATIENT-ID]", s)
        s = _PATIENT_FIELD.sub(lambda m: f"{m.group(1)}{m.group(2)}[PATIENT-DATA]", s)
        s = _AADHAAR.sub("[AADHAAR]", s)
        s = _PAN.sub("[PAN]", s)
        s = _EMAIL.sub(r"\1***@\2", s)
        s = _PHONE.sub("[PHONE]", s)
    return s


def find_secrets(text) -> list:
    """Names of secret-like patterns present in `text` (does not return the values)."""
    s = "" if text is None else str(text)
    hits = []
    for v in known_secret_values():
        if v in s:
            hits.append("registered-secret-value")
            break
    for marker, rx in _PATTERNS:
        if rx.search(s):
            hits.append(marker.strip("[]").lower())
    for name, rx in (("bearer-token", _BEARER), ("url-credentials", _URL_CRED), ("cookie-header", _COOKIE)):
        if rx.search(s):
            hits.append(name)
    for m in _KV.finditer(s):
        if m.group(2) != "[REDACTED]":
            hits.append("credential-assignment")
            break
    return sorted(set(hits))


def find_personal(text) -> list:
    s = "" if text is None else str(text)
    hits = []
    for name, rx in (("patient-id", _PATIENT_ID), ("patient-field", _PATIENT_FIELD), ("aadhaar", _AADHAAR),
                     ("pan", _PAN), ("phone", _PHONE)):
        if rx.search(s):
            hits.append(name)
    for m in _EMAIL.finditer(s):
        if "***@" not in m.group(0):
            hits.append("email")
            break
    return sorted(set(hits))


def assert_clean(text, *, what: str = "output", personal: bool = True) -> None:
    """Fail closed: raise if secret/personal patterns remain. Call on redacted text before writing it."""
    hits = find_secrets(text) + (find_personal(text) if personal else [])
    if hits:
        raise SensitiveDataError(f"{what} still contains sensitive-looking data ({', '.join(sorted(set(hits)))}); refusing to write it")


def assert_clean_obj(obj, *, what: str = "output", personal: bool = True) -> None:
    """assert_clean() on every string (keys and values) inside a nested structure."""
    if isinstance(obj, str):
        assert_clean(obj, what=what, personal=personal)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            assert_clean_obj(v, what=what, personal=personal)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            assert_clean_obj(v, what=what, personal=personal)


def safe_text(text, *, what: str = "output", personal: bool = True) -> str:
    """redact() then assert_clean(): the standard way to prepare any text for disk, reports or an AI model."""
    out = redact(text, personal=personal)
    assert_clean(out, what=what, personal=personal)
    return out


def redact_obj(obj, *, personal: bool = True):
    """Recursively redact every string inside dict/list/tuple structures."""
    if isinstance(obj, str):
        return redact(obj, personal=personal)
    if isinstance(obj, dict):
        return {redact(k, personal=personal) if isinstance(k, str) else k: redact_obj(v, personal=personal) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, personal=personal) for v in obj]
    return obj
