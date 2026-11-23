"""Provider-neutral LLM client for the QA agent (Qwen, OpenAI, Ollama, vLLM, OpenRouter...).

Uses the widely supported OpenAI-compatible *chat completions* API, so any Qwen host works.
Configuration (environment only; never put keys in files):
  QA_LLM_BASE_URL    e.g. https://dashscope-intl.aliyuncs.com/compatible-mode/v1   (Qwen via Alibaba Cloud)
                          http://localhost:11434/v1                                (Qwen via local Ollama)
                          empty = OpenAI default
  QA_LLM_API_KEY     key for that host (falls back to OPENAI_API_KEY). Ollama accepts any non-empty value.
  QA_LLM_MODEL       e.g. qwen-plus, qwen3-coder-plus, qwen2.5:14b   (the --model flag / QA_DIAGNOSIS_MODEL override it)
  QA_LLM_EXTRA_BODY  optional JSON passed to the host, e.g. {"enable_thinking": false} for open-source Qwen3 on Alibaba Cloud
"""
from __future__ import annotations
import json
import os
import re

_THINK_RX = re.compile(r'<think>.*?</think>', re.S | re.I)


def api_key() -> str:
    return (os.environ.get('QA_LLM_API_KEY') or os.environ.get('OPENAI_API_KEY') or '').strip()


def is_configured() -> bool:
    return bool(api_key())


def default_model() -> str:
    return os.environ.get('QA_LLM_MODEL', '').strip()


def host_label() -> str:
    """Host name only (never the key) - recorded in reports so a reader knows where page data was sent."""
    from urllib.parse import urlsplit
    return urlsplit(os.environ.get('QA_LLM_BASE_URL', '').strip() or 'https://api.openai.com').netloc


def complete(instructions: str, prompt: str, *, model: str = '', max_tokens: int = 1500,
             timeout: float = 60, retries: int = 1, json_mode: bool = False) -> str:
    """One system+user round trip; returns the reply text. Raises RuntimeError with a clear message when not usable."""
    key = api_key()
    if not key:
        raise RuntimeError('No LLM key: set QA_LLM_API_KEY (or OPENAI_API_KEY) in the environment')
    model = (model or default_model()).strip()
    if not model:
        raise RuntimeError('No LLM model: pass --model or set QA_LLM_MODEL (for Qwen e.g. qwen-plus)')
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError('Install the optional dependency: pip install openai') from e
    client = OpenAI(api_key=key, base_url=os.environ.get('QA_LLM_BASE_URL', '').strip() or None,
                    timeout=timeout, max_retries=retries)
    kwargs = dict(model=model, max_tokens=max_tokens, temperature=0,
                  messages=[{'role': 'system', 'content': instructions}, {'role': 'user', 'content': prompt}])
    extra = os.environ.get('QA_LLM_EXTRA_BODY', '').strip()
    if extra:
        try:
            kwargs['extra_body'] = json.loads(extra)
        except ValueError as e:
            raise RuntimeError('QA_LLM_EXTRA_BODY is not valid JSON') from e
    if json_mode:
        kwargs['response_format'] = {'type': 'json_object'}
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as e:
        if json_mode and 'response_format' in str(e):          # some hosts reject JSON mode: ask again as plain text
            kwargs.pop('response_format')
            resp = client.chat.completions.create(**kwargs)
        else:
            raise
    text = resp.choices[0].message.content or ''
    return _THINK_RX.sub('', text).strip()                       # Qwen3 may prepend <think>...</think>
