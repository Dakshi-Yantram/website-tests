"""Exceptions of the login agent. Messages are safe to print (never contain secrets)."""


class LoginAgentError(Exception):
    """Base class."""


class SafetyBlocked(LoginAgentError):
    """An environment / origin / network gate refused the operation (fail closed)."""


class SensitiveDataError(LoginAgentError):
    """Sensitive data survived redaction, so the output must not be written."""
