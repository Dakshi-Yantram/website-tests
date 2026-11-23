"""LLM adapter: OpenAI-compatible chat completions, so Qwen hosts work. Uses a fake 'openai' module; no network."""
import sys, types
import pytest
from qa_agent import llm


def _fake_openai(monkeypatch, seen):
    class _Client:
        def __init__(self, **kw): seen['client'] = kw
        class chat:  # noqa: N801
            class completions:  # noqa: N801
                @staticmethod
                def create(**kw):
                    seen['call'] = kw
                    msg = types.SimpleNamespace(content=' {"steps": []} ')
                    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
    monkeypatch.setitem(sys.modules, 'openai', types.SimpleNamespace(OpenAI=_Client))


def test_qwen_base_url_key_and_model_are_used(monkeypatch):
    seen = {}
    _fake_openai(monkeypatch, seen)
    monkeypatch.setenv('QA_LLM_BASE_URL', 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1')
    monkeypatch.setenv('QA_LLM_API_KEY', 'k-test')
    monkeypatch.setenv('QA_LLM_MODEL', 'qwen-plus')
    out = llm.complete('sys', 'user', json_mode=True)
    assert out == '{"steps": []}'
    assert seen['client']['base_url'].endswith('/compatible-mode/v1') and seen['client']['api_key'] == 'k-test'
    assert seen['call']['model'] == 'qwen-plus' and seen['call']['response_format'] == {'type': 'json_object'}
    assert seen['call']['messages'][0]['role'] == 'system'


def test_missing_key_or_model_fails_with_clear_message(monkeypatch):
    for k in ('QA_LLM_API_KEY', 'OPENAI_API_KEY', 'QA_LLM_MODEL', 'QA_LLM_BASE_URL'):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match='No LLM key'):
        llm.complete('s', 'u', model='qwen-plus')
    monkeypatch.setenv('QA_LLM_API_KEY', 'x')
    with pytest.raises(RuntimeError, match='No LLM model'):
        llm.complete('s', 'u')
