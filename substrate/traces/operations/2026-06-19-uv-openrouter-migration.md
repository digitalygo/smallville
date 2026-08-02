---
status: completed
created_at: 2026-06-19
updated_at: 2026-08-01
files_edited:
  - .gitignore
  - .env.example
  - README.md
  - pyproject.toml
  - uv.lock
  - reverie/backend_server/utils.py
  - reverie/backend_server/persona/prompt_template/gpt_structure.py
  - reverie/backend_server/persona/prompt_template/run_gpt_prompt.py
  - reverie/backend_server/persona/prompt_template/defunct_run_gpt_prompt.py
  - reverie/backend_server/persona/memory_structures/scratch.py
  - reverie/backend_server/persona/cognitive_modules/plan.py
  - reverie/backend_server/test.py
rationale:
  - Move the project to uv while preserving the verified Python 3.9 runtime.
  - Replace manual secret editing with a committed .env-driven configuration path.
  - Add OpenRouter attribution and per-persona model routing with minimal disruption to the legacy prompt stack.
supporting_docs:
  - https://openrouter.ai/docs/app-attribution
  - /home/luca/Documents/github-digitalygo/dotfiles/home/dot_config/exact_opencode/plugins/openrouter-attribution.ts
  - substrate/traces/reviews/2026-06-19-llm-provider-config-security.md
---

# Uv and OpenRouter migration

## Summary of changes

The repository now uses `uv` for dependency management, loads runtime configuration from a root `.env` file, and supports OpenRouter with app attribution headers and per-persona model overrides. The backend LLM wrapper was updated so legacy completion-style prompt calls are normalized through the current chat-completions path while preserving the existing higher-level prompt runner structure.

## Technical reasoning

The repository was previously configured around a manually created `reverie/backend_server/utils.py` and a `requirements.txt` workflow. That setup made onboarding brittle and embedded a secret-management step inside source-like configuration. A committed `utils.py` that loads `.env` preserves the existing `from utils import *` import contract while removing the manual file-creation requirement.

OpenRouter support was implemented in the centralized GPT wrapper to avoid broad call-site churn. The code now applies `HTTP-Referer`, `X-Title`, `X-OpenRouter-Title`, and `X-OpenRouter-Categories` headers using defaults aligned with the requested attribution setup and categories `game,roleplay`. Provider resolution now fails closed for unknown values and respects the documented precedence order `scratch.json` overrides, then `LLM_PERSONA_MODELS`, then global defaults.

Per-persona model selection was added by extending scratch memory with optional `llm_provider` and `llm_model` fields and by threading persona context through the relevant prompt helpers. This keeps persona-specific routing close to existing simulation state while allowing fast global overrides from `.env`.

The uv migration intentionally keeps the legacy Django 2.2 branch for compatibility with the current codebase and verified Python 3.9 target. This avoids a much larger framework migration in the same change set.

## Impact assessment

Project setup is now centered on `uv sync` and `uv run`, with `pyproject.toml` and `uv.lock` as the canonical dependency metadata. Users no longer need to hand-author `utils.py`, and OpenRouter can be enabled by copying `.env.example` to `.env` and providing an API key.

Simulation behavior can now vary models per character without rewriting prompt templates. Existing persona scratch files remain backward compatible because the new LLM fields are optional.

One security-related legacy constraint remains: the environment server still depends on Django 2.2 for compatibility. The README now documents this as localhost-only development infrastructure, and the security review records that it should not be treated as production-safe until the framework is upgraded.

## Validation steps

1. Confirmed the repository had no pre-existing local changes before implementation with `git status --short` and `git diff --stat`.
2. Verified the resulting Python environment with `uv sync`.
3. Verified syntax across the codebase with `uv run python -m compileall -q reverie environment/frontend_server`.
4. Verified the Django environment server configuration with `uv run python manage.py check` from `environment/frontend_server/`.
5. Ran import and provider-resolution smoke checks under uv, including normalized OpenRouter provider resolution and fail-closed invalid provider handling.
6. Reviewed the resulting diffs and read the modified files directly.
7. Ran `security-review-specialist` and reviewed `substrate/traces/reviews/2026-06-19-llm-provider-config-security.md`.

The final security review resolved the new provider-routing and logging regressions. One low-risk legacy issue remains open: Django 2.2 is still unsupported and should be considered localhost-only until upgraded.

## Update: 2026-08-01 superseded by production hardening

The legacy constraints recorded above are historical and no longer describe the repository. At the time of this migration the stack intentionally kept the environment server on Django 2.2 with a Python 3.9 target, and the notes about Django 2.2 being unsupported or localhost-only applied then. Those notes are now superseded: the production-hardening operation (`substrate/traces/operations/2026-06-19-production-hardening.md`) later upgraded the environment server to Django 5.2 on Python 3.12+, with `pyproject.toml` and `uv.lock` as the only dependency manifests. See that operation record for the current supported baseline; the statements in this record before this update are preserved as history only.
