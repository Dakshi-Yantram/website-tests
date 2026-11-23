# AI-driven agent (Qwen)

`agent.py auto --ai` lets a model (Qwen or any OpenAI-compatible model) decide what to click or type next.
Code, not the model, validates every action and decides what is a bug.

## Setup (Windows PowerShell)

```powershell
.venv\Scripts\activate
pip install -r requirements-ai.txt
```

Set the key in the current PowerShell window only (never commit it, never paste it into chat):

```powershell
# Qwen on Alibaba Cloud (international endpoint)
$env:QA_LLM_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
$env:QA_LLM_API_KEY  = Read-Host "Qwen API key"
$env:QA_LLM_MODEL    = "qwen-plus"

# OR Qwen on local Ollama (free, runs on your PC)
$env:QA_LLM_BASE_URL = "http://localhost:11434/v1"
$env:QA_LLM_API_KEY  = "ollama"
$env:QA_LLM_MODEL    = "qwen2.5:14b"
```

Open-source Qwen3 models on Alibaba Cloud need thinking turned off: `$env:QA_LLM_EXTRA_BODY = '{"enable_thinking": false}'`

## Run

```powershell
python agent.py auto --app nurseconnect --env staging --ai --ai-steps 40 --headed
```

Optional: `--ai-goal "focus on the booking flow"`, `--role nurse`, `--ai-page-text` (also sends a short redacted text snippet).

Output: the normal report in `artifacts\reports\` plus `agent_trace_*.json` (every decision, refusal and result).

## Safety rules (enforced in code)

- Only declared `staging` or `local` environments run; production and unknown environments are refused (page labels go to an external model).
- Read-only: every non-GET request is still blocked by the network guard.
- Refused before execution: delete/remove/pay/cancel/logout/message controls, external links, password fields, file downloads,
  typed values that look like real personal data.
- Bugs come from code detectors (HTTP errors, JS/console errors, failing APIs, blank pages, error text). Model remarks are stored as
  *AI observation (unverified)*, severity Low/Info only.
- If the model fails 3 times in a row, the built-in crawler finishes the run.
- Page text is untrusted data; the model has no tool that could act on instructions found in a page.
- Not covered: native Android/iOS, payments, and anything the read-only guard blocks.
