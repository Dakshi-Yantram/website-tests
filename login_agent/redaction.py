"""Secret and personal-data redaction for everything the login agent prints or writes.

redact()            mask secrets (always) and e-mail addresses / phone numbers (personal=True)
find_secrets()      names of secret patterns still present in a text
assert_clean*()     fail closed (SensitiveDataError) when something sensitive-looking remains
register_secret()   mask one literal value (e.g. a password read from the environment) wherever it appears
Secret values are also picked up automatically from environment variables whose NAME looks secret (PASSWORD, TOKEN, OTP ...).
"""
import os
import re

from .errors import SensitiveDataError

MASK = "[REDACTED]"

_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)
_JWT = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{4,}")
_GH_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/]{8,}=*")
_URL_CRED = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_COOKIE = re.compile(r"(?i)\b(set-cookie|cookie)\s*:(?!\s*\[REDACTED\])\s*[^\r\n]+")
# key = value / key: value / "key": "value" for secret-looking key names, including OTP and verification codes
_KV = re.compile(
    r"(?i)(?P<key>[\"']?\b(?:[a-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|authorization|otp|passcode|pin)"
    r"|verification[_ -]?code|one[_ -]?time[_ -]?(?:code|password))[a-z0-9_.-]*\b[\"']?)"
    r"(?P<sep>\s*[:=]\s*)(?P<val>(?![\"']?(?:bearer|basic)\s)\"[^\"]*\"|'[^']*'|(?!(?:bearer|basic)\s)[^\s,;&}\]]+)")
_OTP_WORD = re.compile(r"(?i)\b(otp|passcode|verification code|one[- ]time (?:code|password))\b([^\n]{0,20}?)\b(\d{4,8})\b")
_DB_URL = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://\S+")
_EMAIL = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_PHONE = re.compile(r"(?<![\w.])(?:\+?91[\s-]?)?[6-9]\d{9}(?!\w)")

_SECRET_PATTERNS = [("private key", _PRIVATE_KEY), ("jwt", _JWT), ("github token", _GH_TOKEN), ("aws key", _AWS_KEY),
                    ("bearer token", _BEARER), ("url credentials", _URL_CRED), ("cookie", _COOKIE), ("database url", _DB_URL)]
_SECRET_ENV_NAME = re.compile(r"(?i)(password|passwd|secret|token|otp|private_key|api_key|apikey|credential|cookie)")
_BORING = {"true", "false", "none", "null", "yes", "no", "production", "staging", "local"}
_registered: set = set()


def register_secret(value) -> None:
    """Mask this literal value everywhere from now on. Values shorter than 4 characters are ignored (too ambiguous)."""
    if value and len(str(value)) >= 4:
        _registered.add(str(value))


def clear_registered_secrets() -> None:
    _registered.clear()


def known_secret_values() -> set:
    env = {v for k, v in os.environ.items()
           if v and len(v) >= 4 and v.lower() not in _BORING and _SECRET_ENV_NAME.search(k) and not k.endswith(("_FILE", "_PATH", "_NAME", "_ENV"))}
    return set(_registered) | env


def _mask_kv(m):
    val = m.group("val")
    if val.strip("\"'") == "" or val == MASK or "[REDACTED" in val:
        return m.group(0)
    return f"{m.group('key')}{m.group('sep')}{MASK}"


def redact(text, *, personal: bool = True) -> str:
    """Return `text` with secrets (and, by default, e-mail addresses and phone numbers) masked."""
    if text is None:
        return ""
    out = str(text)
    for secret in sorted(known_secret_values(), key=len, reverse=True):     # literal values first: they may sit inside any pattern
        out = out.replace(secret, MASK)
    out = _PRIVATE_KEY.sub(MASK, out)
    out = _URL_CRED.sub(r"\1" + MASK + "@", out)
    out = _DB_URL.sub(MASK, out)
    out = _COOKIE.sub(lambda m: f"{m.group(1)}: {MASK}", out)
    out = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", out)
    for rx in (_JWT, _GH_TOKEN, _AWS_KEY):
        out = rx.sub(MASK, out)
    out = _KV.sub(_mask_kv, out)
    out = _OTP_WORD.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", out)
    if personal:
        out = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", out)
        out = _PHONE.sub("[PHONE]", out)
    return out


def find_secrets(text) -> list:
    """Labels of secret patterns that are still present (an empty list means clean)."""
    t = str(text or "")
    hits = [label for label, rx in _SECRET_PATTERNS if rx.search(t)]
    if any(s in t for s in known_secret_values()):
        hits.append("known secret value")
    for m in _KV.finditer(t):
        if "[REDACTED" not in m.group("val"):
            hits.append("secret key/value")
            break
    if _OTP_WORD.search(t):
        hits.append("otp code")
    return hits


def find_personal(text) -> list:
    t = str(text or "")
    hits = []
    if _EMAIL.search(t):
        hits.append("email address")
    if _PHONE.search(t):
        hits.append("phone number")
    return hits


def assert_clean(text, *, what: str = "output", personal: bool = True) -> None:
    hits = find_secrets(text) + (find_personal(text) if personal else [])
    if hits:
        raise SensitiveDataError(f"{what} still contains sensitive-looking data ({', '.join(sorted(set(hits)))}); refusing to write it")


def assert_clean_obj(obj, *, what: str = "output", personal: bool = True) -> None:
    """assert_clean() on every string (keys and values) inside nested dict/list structures."""
    if isinstance(obj, str):
        assert_clean(obj, what=what, personal=personal)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            assert_clean_obj(k, what=what, personal=personal)
            assert_clean_obj(v, what=what, personal=personal)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            assert_clean_obj(v, what=what, personal=personal)


def redact_obj(obj, *, personal: bool = True):
    if isinstance(obj, str):
        return redact(obj, personal=personal)
    if isinstance(obj, dict):
        return {redact(k, personal=personal) if isinstance(k, str) else k: redact_obj(v, personal=personal) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v, personal=personal) for v in obj]
    return obj
