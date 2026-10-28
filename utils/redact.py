"""Remove credentials, tokens and personal data from anything we log or report.
Thin wrapper kept for backwards compatibility: the implementation lives in qa_agent/redaction.py."""
from config import secret_values
from qa_agent.redaction import redact as _redact, register_secret


def redact(text) -> str:
    for v in secret_values():
        register_secret(v)
    return _redact(text)
