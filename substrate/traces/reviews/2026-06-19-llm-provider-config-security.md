---
status: completed
created_at: 2026-06-19
updated_at: 2026-06-19
reviewer: security-review-specialist
target: Smallville session changes for LLM provider config, env handling, prompt runners, and dependency config
scope: Modified files listed by requester plus referenced runtime files needed to validate impact
supporting_docs:
  - git status --short
  - git diff -- scoped modified files
---

# Security review for LLM provider and config changes

## Summary

Found 1 medium and 3 low findings. Main risk: LLM provider resolution accepts unvalidated persona and environment values, then fails open to OpenAI for unknown providers, so prompt and memory data can leave through an unintended vendor. Other risks: debug logging defaults to on, new uv config carries an unsupported Django branch, and one modified persona argument crashes iterative chat generation.

## Scope and methodology

Reviewed `.gitignore`, `.env.example`, `pyproject.toml`, `uv.lock`, `README.md`, `reverie/backend_server/utils.py`, `gpt_structure.py`, `run_gpt_prompt.py`, `defunct_run_gpt_prompt.py`, `scratch.py`, `plan.py`, and `test.py`. Also read `persona.py`, `converse.py`, and `print_prompt.py` for call-path and logging impact. Used source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run.

## Findings by severity

### Medium

#### 🟡 risk: LLM provider selection can fail open to unintended OpenAI egress

- **Location**: `reverie/backend_server/persona/memory_structures/scratch.py:239-240`, `reverie/backend_server/persona/prompt_template/gpt_structure.py:64-96`, `reverie/backend_server/persona/prompt_template/gpt_structure.py:29-41`, `README.md:77-88`
- **Evidence**: `scratch.py` loads `llm_provider` and `llm_model` from persona `scratch.json`. `_resolve_model()` then uses those values without normalization or allowlist validation. `_get_client()` sends only exact `provider == "openrouter"` to OpenRouter; every other value uses `OpenAI(api_key=openai_api_key)`. README says scratch fields have priority over `LLM_PERSONA_MODELS`, but code applies the env map after scratch fields, so env overrides scratch for matching names.
- **Impact**: Typoed or malicious persona storage, such as `OpenRouter`, `openrouter `, or another string, can route prompts, memory, and chat context to OpenAI when an OpenAI key is present. This is data egress to an unintended LLM vendor, with privacy and data-residency impact. Env map precedence mismatch can also override scratch-level provider controls despite docs saying scratch wins.
- **False-positive notes**: Inputs are config and simulation storage, not direct HTTP parameters. Base URL is fixed, so this is not SSRF. Risk matters when scratch files or env maps are imported from less-trusted sources, or when provider selection enforces privacy boundaries.
- **Remediation**: Normalize and validate provider values before client selection. Fail closed on unknown providers. Enforce documented precedence, or update docs and comments. Example: allow only `{ "openai", "openrouter" }`, strip and lowercase scratch/env values, and raise a configuration error instead of falling through to OpenAI.

### Low

#### 🟡 risk: Debug logging defaults expose prompt and response content

- **Location**: `.env.example:41`, `reverie/backend_server/utils.py:74`, `reverie/backend_server/persona/prompt_template/run_gpt_prompt.py:80-82`, `reverie/backend_server/persona/prompt_template/print_prompt.py:23-39`
- **Evidence**: `.env.example` sets `DEBUG=true`; `utils.py` also defaults `DEBUG` to true when unset. Prompt runners call `print_run_prompts()` when `debug` is true, and `print_run_prompts()` prints prompt input, full prompt, and output.
- **Impact**: Logs can capture persona memory, user-like chat text, and LLM responses by default. Shared terminals, process supervisors, or hosted logs can leak sensitive simulation content.
- **False-positive notes**: API keys are not printed by this path. Existing debug print sites predate this change, but the new environment path makes debug-on the documented/default behavior.
- **Remediation**: Set `.env.example` to `DEBUG=false` and default `utils.py` to false. Keep verbose prompt dumps behind explicit opt-in, and redact or truncate prompt content in shared logs.

#### 🟡 risk: New dependency config locks an unsupported Django branch

- **Location**: `pyproject.toml:8`, `uv.lock:214-223`, `README.md:15-19`
- **Evidence**: New install flow recommends `uv sync`; `pyproject.toml` constrains Django to `>=2.2,<3.0`, and `uv.lock` resolves Django `2.2.28`. Django 2.2 is unsupported and receives no security fixes.
- **Impact**: If the environment server is exposed beyond localhost or reused in deployment, known and future Django vulnerabilities remain unpatched by the selected major version.
- **False-positive notes**: Existing `requirements.txt` already used Django 2.2, so this is carried into the new recommended dependency path rather than first introduced as a package family. Lock entries include PyPI hashes and no git/path dependencies were found.
- **Remediation**: Upgrade to a supported Django LTS, such as 4.2 or newer, with compatibility fixes. If this app is strictly local demo software, document localhost-only use and avoid production deployment with this dependency set.

#### 🟡 risk: Modified persona argument crashes iterative chat generation

- **Location**: `reverie/backend_server/persona/prompt_template/run_gpt_prompt.py:2821-2905`, `reverie/backend_server/persona/cognitive_modules/converse.py:106-118`, `reverie/backend_server/persona/cognitive_modules/plan.py:277-283`
- **Evidence**: `run_gpt_generate_iterative_chat_utt()` has parameters `maze, init_persona, target_persona, ...` and no outer `persona` variable, but the modified call passes `persona=persona`. `converse.py` calls this function from `generate_one_utterance()`, and `plan.py` uses `agent_chat_v2()` for conversations.
- **Impact**: Conversation generation can raise `NameError` before the LLM call, stopping simulation progress. If simulation steps are exposed to untrusted users, this becomes a low-severity availability issue.
- **False-positive notes**: No code execution or data leak found. This is a runtime availability regression. It is reachable through the default conversation path shown in `plan.py` and `converse.py`.
- **Remediation**: Pass `persona=init_persona` in `run_gpt_generate_iterative_chat_utt()` or rename the function parameter. Add a small regression test for this call path.

## Remediation timeline

1. Fix provider validation and precedence first; this protects LLM prompt confidentiality.
1. Change debug defaults to off before shared or long-running runs.
1. Fix iterative chat `persona=persona` crash before relying on conversation simulation.
1. Plan Django upgrade before any non-local deployment.

## Validation notes

Retest with `LLM_PROVIDER=openrouter`, mixed-case scratch providers, invalid providers, and both scratch plus `LLM_PERSONA_MODELS` set. Confirm invalid providers raise before any API request. Confirm debug default is false with no `.env`. Exercise `agent_chat_v2()` through `generate_convo()` and confirm no `NameError`. Run dependency audit after Django upgrade.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- LLM provider selection fail-open (medium): resolved. `utils.py:49-51` normalizes and validates global `LLM_PROVIDER`; `gpt_structure.py:84-87` normalizes and validates scratch providers; `gpt_structure.py:92-109` only applies `LLM_PERSONA_MODELS` where scratch fields are unset; `gpt_structure.py:111-118` validates the resolved provider before client use. `_get_client()` still has an `else` OpenAI branch at `gpt_structure.py:29-41`, but current reviewed call paths reach it only after `_resolve_model()` validation.
- Debug logging defaults expose prompt and response content (low): partially resolved. `.env.example:41` now sets `DEBUG=false`, and `utils.py:89` defaults unset `DEBUG` to false. Unconditional prompt and response prints remain in `run_gpt_prompt.py:2787-2791` and `run_gpt_prompt.py:2901-2906`, so prompt content can still reach logs outside the debug gate.
- Unsupported Django branch in uv config (low): unresolved. `pyproject.toml:8` still constrains Django to `>=2.2,<3.0`, and `uv.lock:214-223` still locks Django `2.2.28`.
- Iterative chat persona argument crash (low): resolved. `run_gpt_prompt.py:2821` defines `init_persona`, and `run_gpt_prompt.py:2904-2905` now passes `persona=init_persona`.

### New findings

No new independent security findings in the requested scope. The remaining prompt logging risk is the same prior logging thread, now partially remediated.

### New validation notes

Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest by importing configuration with invalid `LLM_PROVIDER`, mixed-case scratch providers, scratch plus `LLM_PERSONA_MODELS`, unset `DEBUG`, and `agent_chat_v2()` conversation flow. Confirm prompt and output prints are quiet unless explicit debug or verbose mode is enabled. Run dependency audit after upgrading Django.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- LLM provider selection fail-open (medium): resolved. `utils.py:49-51` normalizes and validates global provider config. `gpt_structure.py:84-87` normalizes and validates scratch providers. `gpt_structure.py:92-112` preserves scratch priority, applies known env map prefixes only when scratch fields are unset, and validates the resolved provider before client use.
- Debug logging defaults expose prompt and response content (low): partially resolved. `.env.example:41` and `utils.py:89` keep debug off by default. The previously noted safety score and iterative prompt/output dumps are now behind `verbose` at `run_gpt_prompt.py:2782-2795` and `run_gpt_prompt.py:2901-2914`. Remaining leak points: `run_gpt_prompt.py:2840` still prints prior conversation context, and `run_gpt_prompt.py:2882-2888` still prints validation debug text plus parsed LLM response outside `debug` or `verbose` gates.
- Unsupported Django branch in uv config (low): partially resolved. `README.md:98` now warns that the Django stack is localhost/development-only and not for production exposure. Dependency risk remains because `pyproject.toml:8` still constrains Django to `>=2.2,<3.0`, and `uv.lock:214-223` still locks Django `2.2.28`.
- Iterative chat persona argument crash (low): resolved. `run_gpt_prompt.py:2825` defines `init_persona`, and `run_gpt_prompt.py:2911-2912` now passes `persona=init_persona`.

### New findings

No new independent security findings in the requested scope. Remaining issues are prior logging and dependency threads.

### New validation notes

Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest by running iterative chat with default `DEBUG=false` and `verbose=False`; no conversation context or LLM response should print. Run dependency audit after upgrading or formally accepting Django local-only risk.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- LLM provider selection fail-open (medium): resolved. `utils.py:49-51` normalizes and validates global provider config. `gpt_structure.py:84-87` normalizes and validates scratch providers. `gpt_structure.py:92-112` preserves scratch priority, applies known env map prefixes only when scratch fields are unset, and validates resolved provider before client use.
- Debug logging defaults expose prompt and response content (low): resolved. `.env.example:41` and `utils.py:89` keep debug off by default. Prior prompt and output dumps are now gated by `verbose` at `run_gpt_prompt.py:2782-2795` and `run_gpt_prompt.py:2906-2919`; prior conversation and validation prints are now gated by `debug or verbose` at `run_gpt_prompt.py:2840-2841` and `run_gpt_prompt.py:2882-2893`.
- Unsupported Django branch in uv config (low): still open, with documentation mitigation. `README.md:98` now warns the Django stack is localhost/development-only and not for production exposure. Dependency risk remains because `pyproject.toml:8` still constrains Django to `>=2.2,<3.0`, and `uv.lock:214-223` still locks Django `2.2.28`.
- Iterative chat persona argument crash (low): resolved. `run_gpt_prompt.py:2825` defines `init_persona`, and `run_gpt_prompt.py:2916-2917` passes `persona=init_persona`.

### New findings

No new independent security findings in the requested scope. Only remaining unresolved thread is the unsupported Django dependency branch.

### New validation notes

Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest by running iterative chat with default `DEBUG=false` and `verbose=False`; no conversation context, prompt, or LLM response should print. Run dependency audit after upgrading Django or after formally accepting localhost-only legacy risk.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- LLM provider selection fail-open (medium): resolved. Current review found no unvalidated fallback path in `_resolve_model()` or provider client selection.
- Debug logging defaults expose prompt and response content (low): reopened. Prior updates covered iterative chat logging, but active planning prompts still print prompt inputs, prompts, and LLM outputs outside `debug` or `verbose` gates. Evidence: `run_gpt_prompt.py:323-325`, `run_gpt_prompt.py:359-362`, `run_gpt_prompt.py:437-440`, and `run_gpt_prompt.py:452-456`. `plan.py:141-164` calls `run_gpt_prompt_task_decomp()` during normal planning.
- Unsupported Django branch in uv config (low): resolved. `pyproject.toml:9` now requires `Django>=5.2,<6.0`, and `uv.lock:347-349` locks Django `5.2.15`.
- Iterative chat persona argument crash (low): resolved. No regression found in the reviewed iterative chat call path.

### New findings

No new independent LLM provider findings. The prompt logging item remains the same prior logging thread and is unresolved.

### New validation notes

Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest by running normal simulation planning with `DEBUG=false` and `verbose=False`; logs should not contain persona schedules, full prompts, LLM responses, or parsed outputs. Gate all debug prints behind `if debug or verbose` and redact prompt bodies before shared logging.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- LLM provider selection fail-open (medium): resolved. `utils.py:49-51` validates global provider config, `gpt_structure.py:84-87` validates scratch providers, `gpt_structure.py:92-112` preserves scratch priority and validates the resolved provider before client use.
- Debug logging defaults expose prompt and response content (low): resolved. `utils.py:89` defaults debug off, `.env.example:41` sets `DEBUG=false`, and the previously reopened task-decomposition prints are gated at `run_gpt_prompt.py:323-365`, `run_gpt_prompt.py:440-460`. Iterative chat debug prints are gated at `run_gpt_prompt.py:2861-2862`, `run_gpt_prompt.py:2904-2915`, and `run_gpt_prompt.py:2928-2941`.
- Unsupported Django branch in uv config (low): resolved. `pyproject.toml:9` now requires `Django>=5.2,<6.0`, and `uv.lock:348-349` locks Django `5.2.15`.
- Iterative chat persona argument crash (low): resolved. `run_gpt_prompt.py:2846` defines `init_persona`, and `run_gpt_prompt.py:2938-2939` passes `persona=init_persona`.

### New findings

No new independent LLM provider findings in this final pass.

### New validation notes

Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest with invalid `LLM_PROVIDER`, mixed-case scratch providers, scratch plus `LLM_PERSONA_MODELS`, unset `DEBUG`, and normal simulation planning. Confirm no prompt, schedule, conversation context, or LLM response logs appear unless `debug` or `verbose` is explicitly enabled.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- LLM provider selection fail-open (medium): resolved. `utils.py:49-51` validates global provider config, `gpt_structure.py:84-87` validates scratch providers, and `gpt_structure.py:111-118` validates resolved provider before client use.
- Debug logging defaults expose prompt and response content (low): reopened and unresolved. `utils.py:89` and `.env.example:41` keep debug off by default, but normal prompt paths still force verbose logging. `gpt_structure.py:237-254` prints prompts and retry responses whenever `verbose=True`; active calls pass `True` at `run_gpt_prompt.py:844-845`, `run_gpt_prompt.py:1021-1022`, `run_gpt_prompt.py:1647-1648`, `run_gpt_prompt.py:1903-1904`, `run_gpt_prompt.py:1975-1976`, `run_gpt_prompt.py:2048-2049`, `run_gpt_prompt.py:2125-2126`, `run_gpt_prompt.py:2253-2254`, `run_gpt_prompt.py:2322-2323`, `run_gpt_prompt.py:2534-2535`, and `run_gpt_prompt.py:2754-2755`. Conversation flow also prints state outside debug gates at `converse.py:117-122`, `converse.py:128-134`, and `converse.py:174-177`; `plan.py:277-282` routes normal conversations through that flow.
- Unsupported Django branch in uv config (low): resolved. `pyproject.toml:9` requires `Django>=5.2,<6.0`, and `uv.lock:348-349` locks Django `5.2.15`.
- Iterative chat persona argument crash (low): resolved. `run_gpt_prompt.py:2846` defines `init_persona`, and `run_gpt_prompt.py:2938-2939` passes `persona=init_persona`.

### New findings

No new independent LLM provider findings in this final pass. Prompt and response logging remains the prior logging thread.

### New validation notes

Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest with `DEBUG=false` and default `verbose=False` by running normal action planning and conversation generation. Logs should not contain prompts, persona schedules, retrieved memories, relationship summaries, conversation text, or raw LLM responses. Fix by passing `verbose=(debug or verbose)` to prompt helpers and gating all `print()` calls in `converse.py` behind `if debug or verbose` or structured redacted logging.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- LLM provider selection fail-open (medium): resolved. Current review found provider validation still fails closed before client use at `gpt_structure.py:61-68`, `gpt_structure.py:84-87`, and `gpt_structure.py:111-118`.
- Debug logging defaults expose prompt and response content (low): resolved. `utils.py:89` keeps unset `DEBUG` false, `.env.example:41` documents `DEBUG=false`, prompt helper prints remain gated by `verbose` at `gpt_structure.py:237-254` and `gpt_structure.py:360-381`, active ChatGPT prompt calls now pass the function `verbose` value instead of hardcoded `True` at `run_gpt_prompt.py:844-845`, `run_gpt_prompt.py:1021-1022`, `run_gpt_prompt.py:1647-1648`, `run_gpt_prompt.py:1903-1904`, `run_gpt_prompt.py:1975-1976`, `run_gpt_prompt.py:2048-2049`, `run_gpt_prompt.py:2125-2126`, `run_gpt_prompt.py:2253-2254`, `run_gpt_prompt.py:2322-2323`, `run_gpt_prompt.py:2453-2454`, `run_gpt_prompt.py:2535-2536`, and `run_gpt_prompt.py:2755-2756`. Conversation debug prints are gated at `converse.py:120-128`, `converse.py:133-141`, `converse.py:163-164`, and `converse.py:182-186`.
- Unsupported Django branch in uv config (low): resolved. `pyproject.toml:9` requires `Django>=5.2,<6.0`, and `uv.lock:348-349` locks Django `5.2.15`.
- Iterative chat persona argument crash (low): resolved. `run_gpt_prompt.py:2847-2849` defines `init_persona` flow, and `run_gpt_prompt.py:2939-2940` passes `persona=init_persona`.

### New findings

No new independent LLM provider or logging findings in this final read-only pass.

### New validation notes

Used static source, diff, and ignore-rule review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest with unset `DEBUG`, `DEBUG=false`, and default `verbose=False` by running normal planning, action selection, and conversation generation. Logs should not contain prompts, persona schedules, retrieved memories, relationship summaries, conversation text, or raw LLM responses. Confirm explicit `verbose=True` remains a deliberate local debug mode only.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- LLM provider selection fail-open (medium): resolved. Current review found provider validation still fails closed before client use at `gpt_structure.py:61-68`, `gpt_structure.py:84-87`, and `gpt_structure.py:111-118`.
- Debug logging defaults expose prompt and response content (low): unresolved. Correction to the immediately prior update: prompt helper and conversation prints are now gated, but normal planning still prints persona schedule and LLM planning output outside `debug` or `verbose` gates. Evidence: `plan.py:356-369` prints persona name and truncated schedule during `generate_new_decomp_schedule()`, `plan.py:455-458` prints `new_daily_req` from `ChatGPT_single_request()`, and `plan.py:598-612` prints the full daily schedule and persona name inside `_determine_action()`. Call-path evidence: `_create_react()` calls `generate_new_decomp_schedule()` at `plan.py:842-844`, and `plan()` calls `_determine_action()` during normal action planning at `plan.py:957-959`.
- Unsupported Django branch in uv config (low): resolved. `pyproject.toml:9` requires `Django>=5.2,<6.0`, and `uv.lock:348-349` locks Django `5.2.15`.
- Iterative chat persona argument crash (low): resolved. `run_gpt_prompt.py:2847-2849` defines `init_persona` flow, and `run_gpt_prompt.py:2939-2940` passes `persona=init_persona`.

### New findings

No new independent findings. The remaining issue is the same prior logging thread, still open because planning code prints sensitive simulation state by default.

### New validation notes

Used static source, diff, and ignore-rule review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest with unset `DEBUG`, `DEBUG=false`, and default `verbose=False` by running normal planning, action selection, and reaction planning. Logs should not contain persona names, schedules, daily plans, prompts, retrieved memories, relationship summaries, conversation text, or raw LLM responses. Fix by gating the `plan.py` debug prints behind `if debug or verbose` or replacing them with redacted structured logs.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- LLM provider selection fail-open (medium): resolved. Current review found provider validation still fails closed before client use at `gpt_structure.py:61-68`, `gpt_structure.py:84-87`, and `gpt_structure.py:111-118`; direct planning calls pass `persona=persona` at `plan.py:426`, `plan.py:432`, `plan.py:444`, and `plan.py:455`.
- Debug logging defaults expose prompt and response content (low): resolved. `utils.py:89` keeps unset `DEBUG` false, `.env.example:41` documents `DEBUG=false`, and the previously blocking planning prints are gated at `plan.py:356`, `plan.py:369`, `plan.py:457`, and `plan.py:598-613`.
- Unsupported Django branch in uv config (low): resolved. `pyproject.toml:9` requires `Django>=5.2,<6.0`, and `uv.lock:348-349` locks Django `5.2.15`.
- Iterative chat persona argument crash (low): resolved. `run_gpt_prompt.py:2847-2849` defines the `init_persona` flow, and `run_gpt_prompt.py:2939-2940` passes `persona=init_persona`.

### New findings

No new independent LLM provider or logging findings in this final read-only pass. No unresolved finding remains in this thread.

### New validation notes

Used static source, diff, and ignore-rule review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest with unset `DEBUG`, `DEBUG=false`, and default `verbose=False` by running normal planning, action selection, reaction planning, and conversation generation. Logs should not contain persona names, schedules, daily plans, prompts, retrieved memories, relationship summaries, conversation text, or raw LLM responses unless explicit debug or verbose mode is enabled.
