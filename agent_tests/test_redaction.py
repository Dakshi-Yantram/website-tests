"""Secret and patient-data redaction, fail-closed behaviour, safe logging."""
import json

import pytest

from qa_agent import redaction as R
from qa_agent.errors import SensitiveDataError

FAKE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf9Cnzj4p4WGeKLs1Pt8Qu\n-----END RSA PRIVATE KEY-----"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk0"
FAKE_GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0" * 2

SECRET_SAMPLES = {
    "private key": FAKE_KEY,
    "jwt": f"token seen {FAKE_JWT} in log",
    "github token": f"clone with {FAKE_GH}",
    "aws": "key AKIAABCDEFGHIJKLMNOP here",
    "bearer": "Authorization: Bearer abcdef1234567890abcdef",
    "password kv": "password=hunter2hunter2",
    "json password": '{"password": "S3cretValue!"}',
    "cookie": "Cookie: sessionid=abc123def456; csrftoken=zzz",
    "db url": "postgres://admin:pa55w0rd@db.internal:5432/prod",
    "url creds": "https://user:pass1234@example.org/x",
    "api key kv": "api_key: 0123456789abcdef0123",
}


@pytest.mark.parametrize("name", SECRET_SAMPLES)
def test_secrets_are_removed(name):
    text = SECRET_SAMPLES[name]
    out = R.redact(text)
    for frag in ("hunter2hunter2", "S3cretValue", "abcdef1234567890abcdef", "pa55w0rd", "pass1234", "0123456789abcdef0123",
                 "abc123def456", "MIIBOgIBAAJBAKj34", FAKE_GH, "AKIAABCDEFGHIJKLMNOP", FAKE_JWT):
        assert frag not in out, (name, out)
    assert not R.find_secrets(out), (name, out, R.find_secrets(out))


def test_patient_and_personal_data_is_removed():
    text = ("Patient Name: Ramesh Kumar; UHID: AB12345; MRN 99887766; Aadhaar 1234 5678 9012; PAN ABCDE1234F; "
            "phone 9876543210; mail ramesh.kumar@gmail.com; DOB: 12/03/1980; diagnosis: type 2 diabetes")
    out = R.redact(text)
    for frag in ("Ramesh", "AB12345", "99887766", "1234 5678 9012", "ABCDE1234F", "9876543210", "ramesh.kumar", "12/03/1980", "diabetes"):
        assert frag not in out, (frag, out)
    assert not R.find_personal(out), out


def test_registered_and_env_secrets_are_masked_everywhere(monkeypatch):
    monkeypatch.setenv("SOME_SERVICE_PASSWORD", "zebra-quartz-77")
    R.register_secret("one-off-value-123")
    try:
        out = R.redact("a zebra-quartz-77 b one-off-value-123 c")
        assert "zebra-quartz-77" not in out and "one-off-value-123" not in out
        assert R.find_secrets("x zebra-quartz-77") == ["registered-secret-value"]
    finally:
        R.clear_registered_secrets()


def test_assert_clean_fails_closed():
    with pytest.raises(SensitiveDataError):
        R.assert_clean("here is ghp_" + "a" * 36)
    with pytest.raises(SensitiveDataError):
        R.assert_clean("call 9876543210")
    R.assert_clean("Normal sentence about a login page")


def test_safe_text_returns_clean_text():
    assert "hunter2hunter2" not in R.safe_text("password=hunter2hunter2")


def test_redact_obj_walks_nested_structures():
    obj = {"a": ["x ghp_" + "b" * 30, {"k": "password: abcdefgh"}], "n": 5}
    out = R.redact_obj(obj)
    assert "ghp_" not in json.dumps(out) and "abcdefgh" not in json.dumps(out) and out["n"] == 5
    R.assert_clean_obj(out)


def test_ordinary_text_is_not_mangled():
    s = "Login page loads and shows a Sign in button"
    assert R.redact(s) == s


def test_legacy_utils_redact_still_works(monkeypatch):
    monkeypatch.setenv("NURSE_PASSWORD", "Sup3rS3cret!")
    from utils.redact import redact
    assert "Sup3rS3cret" not in redact("login with Sup3rS3cret! now")
