"""Central configuration. Everything comes from environment / .env."""
import os
import uuid
import re
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "y")


ART = Path(os.getenv("ARTIFACTS_DIR", ROOT / "artifacts"))
SHOTS, LOGS, INV, REPORTS = ART / "screenshots", ART / "logs", ART / "inventory", ART / "reports"
for _d in (SHOTS, LOGS, INV, REPORTS):
    _d.mkdir(parents=True, exist_ok=True)

RUN_ID = os.getenv("RUN_ID") or uuid.uuid4().hex[:8]
TEST_MARKER = f"AUTOTEST_{RUN_ID}"

HEADLESS = env_bool("HEADLESS", True)
SLOW_MO = int(os.getenv("SLOW_MO_MS", "0"))
MAX_PAGES = int(os.getenv("MAX_PAGES_PER_ROLE", "40"))
NAV_TIMEOUT = int(os.getenv("NAV_TIMEOUT_MS", "30000"))
ALLOW_WRITE_REQUESTED = env_bool("ALLOW_WRITE_TESTS", False)
ALLOW_WRITE = False          # final value is computed at the bottom of this file: writes need a VERIFIED staging environment
# Only set true if a site has a broken certificate AND you accept that risk. Default: certificates are checked.
IGNORE_HTTPS_ERRORS = env_bool("IGNORE_HTTPS_ERRORS", False)
# A page that loads slower than this (browser "load" event, measured without extra waiting) is reported.
SLOW_LOAD_SECONDS = float(os.getenv("SLOW_LOAD_SECONDS", "10"))
LOGIN_TIMEOUT_S = int(os.getenv("LOGIN_TIMEOUT_S", "25"))

# Anything matching this is never clicked / submitted automatically.
DANGEROUS = re.compile(
    r"log\s?out|sign\s?out|delete|remove|discharge|cancel|pay\b|pay now|checkout|"
    r"confirm payment|deactivate|terminate|revoke|reset password|clear all|"
    r"withdraw|refund|cash\s?out",
    re.I,
)


@dataclass(frozen=True)
class App:
    key: str
    base: str
    login_path: str

    @property
    def login_url(self) -> str:
        return self.base.rstrip("/") + self.login_path


APPS = {
    "nurseconnect": App("nurseconnect",
                        os.getenv("NURSECONNECT_URL", "https://nurseconnect.co.in"),
                        os.getenv("NURSECONNECT_LOGIN_PATH", "/auth/login")),
    "hospital": App("hospital",
                    os.getenv("HOSPITAL_URL", "http://hospitaldashboard-env.eba-n7anfees.ap-south-1.elasticbeanstalk.com"),
                    os.getenv("HOSPITAL_LOGIN_PATH", "/")),
}


@dataclass(frozen=True)
class Role:
    name: str
    app: str
    email_env: str
    password_env: str
    landing_env: str = ""       # env var that can override landing_prefix
    landing_default: str = ""   # where this role is expected to land after login ("" = not known yet)

    @property
    def landing_prefix(self) -> str:
        return (os.getenv(self.landing_env, "") if self.landing_env else "").strip() or self.landing_default

    @property
    def email(self) -> str:
        return os.getenv(self.email_env, "").strip()

    @property
    def password(self) -> str:
        return os.getenv(self.password_env, "")


ROLES = {
    # landing_default values for nurse/consumer come from your earlier reports (/partner and /consumer).
    # Reception/Doctor are left empty on purpose until the hospital login has been confirmed.
    "nurse": Role("nurse", "nurseconnect", "NURSE_EMAIL", "NURSE_PASSWORD", "NURSE_LANDING_PATH", "/partner"),
    "consumer": Role("consumer", "nurseconnect", "CONSUMER_EMAIL", "CONSUMER_PASSWORD", "CONSUMER_LANDING_PATH", "/consumer"),
    "reception": Role("reception", "hospital", "RECEPTION_EMAIL", "RECEPTION_PASSWORD", "RECEPTION_LANDING_PATH", ""),
    "doctor": Role("doctor", "hospital", "DOCTOR_EMAIL", "DOCTOR_PASSWORD", "DOCTOR_LANDING_PATH", ""),
}
_ONLY = os.getenv("APP_ONLY", "").strip().lower()
if _ONLY:
    if _ONLY not in APPS:
        raise SystemExit(f"APP_ONLY must be one of {list(APPS)}")
    APPS = {k: v for k, v in APPS.items() if k == _ONLY}
    ROLES = {k: v for k, v in ROLES.items() if v.app == _ONLY}

# Menu items you showed in screenshots. Used to verify they are really present.
EXPECTED_MENU = {
    "nurse": ["Workspace", "Assignments", "Visits", "Documentation", "Care Packages",
              "Teleconsult", "Earnings", "Training", "Assessments", "Availability",
              "Help & Support"],
    "reception": ["OPD", "Emergency", "In Patient"],
    "doctor": ["OPD", "Emergency Green", "Inpatient", "OT"],
}


def secret_values():
    return [r.password for r in ROLES.values() if r.password]


def _verified_write_permission() -> bool:
    """ALLOW_WRITE_TESTS=true is only a request. Writes stay disabled (fail closed) unless TEST_ENV names an environment
    declared kind staging|local in config/environments.yml, every active app's URL is in that environment's explicit
    origin allowlist, and STAGING_WRITE_CONFIRMATION holds the confirmation phrase. See docs/STAGING_AND_SECURITY.md."""
    if not ALLOW_WRITE_REQUESTED:
        return False
    import sys
    from qa_agent.errors import SafetyBlocked
    from qa_agent.safety import CONFIRM_PREFIX, get_environment, normalize_origin, verify_environment
    try:
        name = os.getenv("TEST_ENV", "").strip()
        if not name:
            raise SafetyBlocked("TEST_ENV is not set, so the environment cannot be verified")
        env = get_environment(name)
        if not env.is_staging_like:
            raise SafetyBlocked(f"environment {name!r} is {env.kind}: production is read-only")
        if os.getenv("STAGING_WRITE_CONFIRMATION", "") != CONFIRM_PREFIX + env.name:
            raise SafetyBlocked("STAGING_WRITE_CONFIRMATION does not hold the confirmation phrase for this environment")
        for key, app in APPS.items():
            origin = normalize_origin(app.base)
            if origin not in env.allowed_origins or origin in env.production_origins:
                raise SafetyBlocked(f"{key} URL origin {origin} is not in the staging allowlist of {name!r}")
            verify_environment(env, key)
        return True
    except SafetyBlocked as e:
        print(f"[safety] ALLOW_WRITE_TESTS=true was IGNORED (read-only): {e}", file=sys.stderr)
        return False


ALLOW_WRITE = _verified_write_permission()
