"""Staging-only writes, production blocked, unknown origins blocked, fail-closed verification."""
import pytest
import yaml

from agent_tests.conftest import make_env_file
from qa_agent.errors import SafetyBlocked
from qa_agent.safety import (CONFIRM_PREFIX, RequestGuard, check_write_allowed, get_environment, load_environments,
                             normalize_origin, verify_environment)


def test_normalize_origin():
    assert normalize_origin("HTTPS://Example.ORG:443/a/b?x=1") == "https://example.org"
    assert normalize_origin("http://example.org:8080/x") == "http://example.org:8080"
    for bad in ("ftp://x", "not a url", "", "javascript:alert(1)"):
        with pytest.raises(SafetyBlocked):
            normalize_origin(bad)


def test_missing_environment_file_fails_closed(tmp_path):
    with pytest.raises(SafetyBlocked, match="not found"):
        load_environments(tmp_path / "missing.yml")


def test_invalid_yaml_and_shape_fail_closed(tmp_path):
    p = tmp_path / "e.yml"
    p.write_text("environments: [", encoding="utf-8")
    with pytest.raises(SafetyBlocked):
        load_environments(p)
    p.write_text("version: 1\n", encoding="utf-8")
    with pytest.raises(SafetyBlocked, match="environments"):
        load_environments(p)


def test_shipped_environments_file_declares_production_only():
    envs = load_environments()
    assert envs["production"].kind == "production"
    assert not [e for e in envs.values() if e.is_staging_like], "no staging may be declared until the owner adds one"


def test_staging_requires_explicit_allowed_origins(tmp_path):
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump({"version": 1, "environments": {"staging": {"kind": "staging", "apps": {"nurseconnect": {"base_url": "https://staging.example.org"}}}}}))
    with pytest.raises(SafetyBlocked, match="explicit allowed_origins"):
        load_environments(p)


def test_staging_cannot_reuse_a_production_origin_or_host(tmp_path):
    doc = {"version": 1, "environments": {
        "production": {"kind": "production", "apps": {"nurseconnect": {"base_url": "https://app.example.org"}}},
        "staging": {"kind": "staging", "allowed_origins": ["http://app.example.org:81"], "allow_insecure": True,
                    "apps": {"nurseconnect": {"base_url": "http://app.example.org:81"}}}}}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(SafetyBlocked, match="production origin"):
        load_environments(p)


def test_plain_http_staging_needs_explicit_opt_in(tmp_path):
    doc = {"version": 1, "environments": {"staging": {"kind": "staging", "allowed_origins": ["http://stg.example.org"],
                                                       "apps": {"nurseconnect": {"base_url": "http://stg.example.org"}}}}}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(SafetyBlocked, match="plain http"):
        load_environments(p)


def test_base_url_must_be_inside_allowlist(tmp_path):
    doc = {"version": 1, "environments": {"staging": {"kind": "staging", "allowed_origins": ["https://a.example.org"],
                                                       "apps": {"nurseconnect": {"base_url": "https://b.example.org"}}}}}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(SafetyBlocked, match="not in allowed_origins"):
        load_environments(p)


def good(tmp_path, base="http://127.0.0.1:9", **kw):
    f = make_env_file(tmp_path, base, **kw)
    return get_environment("localtest", f)


CONF = CONFIRM_PREFIX + "localtest"


def test_write_allowed_only_when_every_gate_holds(tmp_path):
    env = good(tmp_path)
    check_write_allowed(env, "nurseconnect", "wf_one_id", allow_write=True, confirm_phrase=CONF, confirmed_workflows=["wf_one_id"])


@pytest.mark.parametrize("kw,msg", [
    (dict(allow_write=False, confirm_phrase=CONF, confirmed_workflows=["wf_one_id"]), "not enabled"),
    (dict(allow_write=True, confirm_phrase="", confirmed_workflows=["wf_one_id"]), "confirmation phrase"),
    (dict(allow_write=True, confirm_phrase="I-CONFIRM-STAGING-WRITES-ON-other", confirmed_workflows=["wf_one_id"]), "confirmation phrase"),
    (dict(allow_write=True, confirm_phrase=CONF, confirmed_workflows=[]), "not explicitly confirmed"),
    (dict(allow_write=True, confirm_phrase=CONF, confirmed_workflows=["different_wf"]), "not explicitly confirmed"),
])
def test_each_missing_confirmation_blocks_the_write(tmp_path, kw, msg):
    with pytest.raises(SafetyBlocked, match=msg):
        check_write_allowed(good(tmp_path), "nurseconnect", "wf_one_id", **kw)


def test_no_environment_means_no_writes():
    with pytest.raises(SafetyBlocked, match="No verified environment"):
        check_write_allowed(None, "nurseconnect", "x", allow_write=True, confirm_phrase=CONF, confirmed_workflows=["x"])


def test_production_environment_is_never_writable(tmp_path):
    env = get_environment("production", make_env_file(tmp_path, "http://127.0.0.1:9"))
    with pytest.raises(SafetyBlocked, match="production is read-only"):
        check_write_allowed(env, "nurseconnect", "x", allow_write=True, confirm_phrase=CONFIRM_PREFIX + "production", confirmed_workflows=["x"])


def test_the_word_staging_proves_nothing(tmp_path):
    """An environment NAMED staging whose kind is production, or a URL containing 'staging', is still read-only."""
    doc = {"version": 1, "environments": {"staging": {"kind": "production", "apps": {"nurseconnect": {"base_url": "https://staging.realprod.example.org"}}}}}
    p = tmp_path / "e.yml"
    p.write_text(yaml.safe_dump(doc))
    env = get_environment("staging", p)
    with pytest.raises(SafetyBlocked, match="production is read-only"):
        check_write_allowed(env, "nurseconnect", "x", allow_write=True, confirm_phrase=CONFIRM_PREFIX + "staging", confirmed_workflows=["x"])


def test_unknown_origin_is_blocked_even_in_a_staging_env(tmp_path):
    env = good(tmp_path)
    object.__setattr__(env, "base_urls", {"nurseconnect": "https://unlisted.example.org"})
    with pytest.raises(SafetyBlocked, match="not in the staging allowlist"):
        check_write_allowed(env, "nurseconnect", "wf_one_id", allow_write=True, confirm_phrase=CONF, confirmed_workflows=["wf_one_id"])


def test_verification_probe_must_match_and_fails_closed(tmp_path):
    env = good(tmp_path, verify={"path": "/__env", "json_key": "environment", "equals": "staging-local"})
    verify_environment(env, "nurseconnect", fetch=lambda url: '{"environment": "staging-local"}')
    with pytest.raises(SafetyBlocked, match="mismatch"):
        verify_environment(env, "nurseconnect", fetch=lambda url: '{"environment": "production"}')
    with pytest.raises(SafetyBlocked, match="could not be verified"):
        verify_environment(env, "nurseconnect", fetch=lambda url: "<html>not json</html>")

    def boom(url):
        raise OSError("network down")
    with pytest.raises(SafetyBlocked, match="could not be verified"):
        verify_environment(env, "nurseconnect", fetch=boom)
    with pytest.raises(SafetyBlocked):
        check_write_allowed(env, "nurseconnect", "wf_one_id", allow_write=True, confirm_phrase=CONF, confirmed_workflows=["wf_one_id"], fetch=boom)


# ------------------------------------------------------------------ request guard decisions
def test_guard_allows_reads_everywhere_and_blocks_unknown_writes(tmp_path):
    env = good(tmp_path, base="http://127.0.0.1:5555")
    g = RequestGuard(env, "http://127.0.0.1:5555", writes_enabled=False)
    assert g.decide("GET", "https://cdn.example.org/a.js")[0]
    assert not g.decide("POST", "http://127.0.0.1:5555/api/save")[0]
    assert not g.decide("DELETE", "https://unknown.example.org/x")[0]
    assert g.decide("POST", "http://127.0.0.1:5555/api/auth/login")[0], "login/session POST to own origin is allowed"
    assert not g.decide("POST", "https://unknown.example.org/api/auth/login")[0], "…but not to unknown origins"


def test_guard_writes_only_on_allowlisted_origin_when_enabled(tmp_path):
    env = good(tmp_path, base="http://127.0.0.1:5555")
    g = RequestGuard(env, "http://127.0.0.1:5555", writes_enabled=True)
    assert g.decide("POST", "http://127.0.0.1:5555/api/save")[0]
    assert not g.decide("POST", "http://127.0.0.1:6666/api/save")[0]
    g.block_all = True
    assert not g.decide("POST", "http://127.0.0.1:5555/api/save")[0]
    assert not g.decide("POST", "http://127.0.0.1:5555/api/auth/login")[0], "guarded steps block even login POSTs"


def test_guard_never_writes_to_production(tmp_path):
    f = make_env_file(tmp_path, "http://127.0.0.1:5555", extra_prod="https://prod.example.org")
    prod = get_environment("production", f)
    g = RequestGuard(prod, "https://prod.example.org", writes_enabled=True)   # even if someone wrongly enables it
    assert not g.decide("POST", "https://prod.example.org/api/patients")[0]
    assert not RequestGuard(None, "https://prod.example.org").decide("PUT", "https://prod.example.org/api/x")[0]


def test_guard_auth_origins_allow_a_separate_login_api_host(tmp_path):
    f = make_env_file(tmp_path, "http://127.0.0.1:5555", auth_origins=["http://127.0.0.1:7000"])
    env = get_environment("localtest", f)
    g = RequestGuard(env, "http://127.0.0.1:5555")
    assert g.decide("POST", "http://127.0.0.1:7000/auth/login")[0]
    assert not g.decide("POST", "http://127.0.0.1:7000/patients")[0]
