"""Exception types. Every one carries a message that is safe to print (no secrets)."""


class AgentError(Exception):
    """Base class."""


class WorkflowFileError(AgentError):
    """workflows file is missing, unreadable or not valid YAML."""


class SchemaError(AgentError):
    """Workflow structure is invalid. `.problems` lists every problem found."""

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


class ApprovalError(AgentError):
    """Workflow is not approved, or changed after approval."""


class SafetyBlocked(AgentError):
    """Environment / origin / write gate refused the operation (fail closed)."""


class SensitiveDataError(AgentError):
    """Sensitive data was detected and could not be safely redacted."""


class StepBlocked(AgentError):
    """A step cannot run (feature not found, credentials missing, gate refused). Reported as BLOCKED."""


class StepNotTested(AgentError):
    """A step was deliberately not executed. Reported as NOT TESTED."""


class StepNotVerified(AgentError):
    """A step ran but its result cannot be trusted. Reported as NOT VERIFIED."""
