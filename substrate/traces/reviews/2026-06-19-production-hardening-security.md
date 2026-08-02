---
status: completed
created_at: 2026-06-19
updated_at: 2026-06-19
reviewer: security-review-specialist
target: Smallville production hardening, Django frontend, Docker deployment, runtime config, and browser templates
scope: Current session working tree changes affecting runtime, executable, deployment, and configuration behavior
supporting_docs:
  - git status --short
  - git diff --stat
  - git diff -- scoped runtime and deployment files
  - substrate/traces/status/2026-06-19-production-ready-workspace-state.md
  - substrate/traces/operations/2026-06-19-uv-openrouter-migration.md
---

# Security review for production hardening changes

## Summary

Found 1 high, 4 medium, and 3 low findings. Main risk: production accepts the documented placeholder `DJANGO_SECRET_KEY`, so a copied `.env.example` can boot with a known signing secret. Other risks: public replay pages expose memory, untrusted third-party scripts run on login and operator pages, LLM-backed text reaches `innerHTML`, public demo step input can burn CPU, Docker builds ignore `uv.lock`, TLS redirect is hardcoded off, and `.env.*` files are not ignored by Git.

## Scope and methodology

Reviewed the requested working tree files, including `.gitignore`, `.dockerignore`, `.env.example`, `Dockerfile`, `docker-compose.prod.yml`, `README.md`, `pyproject.toml`, `uv.lock`, Django settings, URLs, WSGI, management entrypoints, translator views, login and simulation templates, Reverie service mode, prompt wrappers, and trace docs. Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Raw secrets were not recorded in this review.

## Findings by severity

### High

#### 🔴 bug: Production accepts documented placeholder `DJANGO_SECRET_KEY`

- **Location**: `.env.example:47`, `environment/frontend_server/frontend_server/settings/production.py:18-22`, `README.md:180-188`
- **Evidence**: `.env.example` sets `DJANGO_SECRET_KEY=change-me-to-a-random-secret-key`. Production settings reject only `"insecure-dev-key-change-me"` or empty values. README tells operators to copy `.env.example` to `.env` before `docker compose -f docker-compose.prod.yml up --build -d`.
- **Impact**: A production deploy can run with a public, known Django signing secret. Attackers who know `SECRET_KEY` can target Django signed data, password reset tokens, and any current or future signed-cookie/session configuration. This weakens all auth/session guarantees for the new operator login surface.
- **False-positive notes**: Compose requires `.env`, but it does not require operators to change the placeholder. Production enforcement exists, but it misses the documented placeholder.
- **Remediation**: Remove the placeholder value or leave `DJANGO_SECRET_KEY` commented in `.env.example`. Reject known placeholders in `production.py`. In Compose, use required interpolation such as `${DJANGO_SECRET_KEY:?set DJANGO_SECRET_KEY}` instead of relying only on `env_file`.

### Medium

#### 🟡 risk: Replay and persona-state pages expose saved simulation memory without auth

- **Location**: `environment/frontend_server/frontend_server/urls.py:16-24`, `environment/frontend_server/translator/views.py:185-223`, `environment/frontend_server/translator/views.py:227-240`, `environment/frontend_server/templates/persona_state/persona_state.html:149-203`
- **Evidence**: `replay` and `replay_persona_state` routes have no `login_required`. `replay_persona_state()` reads `scratch.json`, `spatial_memory.json`, and `associative_memory/nodes.json`, then the template renders events, conversation history, thoughts, and evidence fields.
- **Impact**: Anyone who knows or guesses a simulation code can view persona state and conversation memory. Production simulations can contain private prompts, LLM outputs, custom histories, and operator-generated content.
- **False-positive notes**: Public demos may be intended. Risk applies when the same production host stores private simulations under `storage/` or compressed demos that should not be public.
- **Remediation**: Require `login_required` for private `replay` and `replay_persona_state`, or split public demo routes from private storage with an explicit allowlist of publishable compressed simulations.

#### 🟡 risk: Third-party JavaScript runs on login and operator control pages

- **Location**: `environment/frontend_server/templates/registration/login.html:1`, `environment/frontend_server/templates/registration/login.html:29-43`, `environment/frontend_server/templates/base.html:22-29`, `environment/frontend_server/templates/home/home.html:76`, `environment/frontend_server/templates/path_tester/path_tester.html:13`
- **Evidence**: The login form extends `base.html`, which loads remote Bootstrap, `jquery-latest.min.js`, and `clustrmaps.com` scripts. Operator pages also load Phaser from jsDelivr. jQuery, ClustrMaps, and Phaser lack Subresource Integrity in the reviewed templates.
- **Impact**: Any compromised or malicious third-party script executes in the Smallville origin on credential and operator pages. It can read form fields, read DOM CSRF tokens, issue authenticated POSTs, and exfiltrate simulation data visible to the operator.
- **False-positive notes**: Bootstrap CSS/JS has SRI. The risk is the remaining remote executable JavaScript and the tracker script on auth pages.
- **Remediation**: Self-host required JavaScript, pin exact versions, add SRI for any CDN fallback, remove analytics/tracker scripts from auth and control pages, and add a restrictive Content Security Policy.

#### 🟡 risk: LLM-backed simulation text is inserted with `innerHTML`

- **Location**: `environment/frontend_server/translator/views.py:120-122`, `environment/frontend_server/templates/demo/main_script.html:70`, `environment/frontend_server/templates/demo/main_script.html:93`, `environment/frontend_server/templates/demo/main_script.html:397-420`, `environment/frontend_server/templates/home/main_script.html:547-561`
- **Evidence**: Views pass `json.dumps()` output to templates, and templates render it with `|safe`. Browser code then builds `chat_content` from movement/chat fields and assigns LLM-backed values to `innerHTML`.
- **Impact**: A stored script in simulation movement data, persona chat, or LLM output can execute in viewers' browsers. On authenticated operator pages, that script can call simulation POST endpoints with the operator's session and CSRF token.
- **False-positive notes**: Django autoescaping protects normal `{{ }}` output, but these paths explicitly use `|safe` and `innerHTML`. Exploitability depends on attacker influence over simulation storage, custom histories, or LLM output.
- **Remediation**: Use `json_script` for data transfer into JavaScript. Render untrusted fields with `textContent` or DOM text nodes. If limited markup is required, sanitize with a strict allowlist before insertion.

#### 🟡 risk: Public demo step input can cause unbounded CPU work

- **Location**: `environment/frontend_server/translator/views.py:28-30`, `environment/frontend_server/translator/views.py:57-59`, `environment/frontend_server/translator/views.py:77-78`, `README.md:161-162`
- **Evidence**: `_VALID_STEP_RE` allows up to 10 digits. `demo()` is unauthenticated and loops `range(step)` to advance time before reading movement data. README publishes the demo URL pattern with user-controlled `<starting-time-step>`.
- **Impact**: A request for a valid compressed simulation with a huge step value can tie up a Gunicorn worker in a long CPU loop, reducing availability.
- **False-positive notes**: The attacker must know a simulation code that has compressed metadata. The repository includes a documented demo code, so this is reachable in default deployments that ship demo data.
- **Remediation**: Cap `step` to a small configured maximum and to the actual movement length before looping. Return `400` or `404` for out-of-range values.

### Low

#### 🟡 risk: Docker build ignores `uv.lock` and uses floating build tools

- **Location**: `Dockerfile:28`, `Dockerfile:43-44`, `uv.lock:347-359`
- **Evidence**: The image copies `pyproject.toml` and `uv.lock`, but dependency install runs `uv pip install --system --no-cache -r pyproject.toml`, which resolves from the project spec instead of enforcing the lock. The `uv` binary is copied from `ghcr.io/astral-sh/uv:latest`.
- **Impact**: Production images can pull newer, unreviewed package versions or a changed `uv` image even when `uv.lock` is committed. This weakens supply-chain reproducibility.
- **False-positive notes**: `uv.lock` itself pins hashes. The issue is the Dockerfile install command and floating build image.
- **Remediation**: Pin the `uv` image by version or digest and install with lock enforcement, for example `uv sync --frozen --no-dev` or an exported frozen requirements file with hashes.

#### 🟡 risk: Compose hardcodes HTTPS redirect off in production

- **Location**: `docker-compose.prod.yml:35-38`, `environment/frontend_server/frontend_server/settings/production.py:29-32`, `README.md:215-217`
- **Evidence**: Production settings default `DJANGO_SECURE_SSL_REDIRECT` to true, but Compose hardcodes `DJANGO_SECURE_SSL_REDIRECT: "false"` and labels it a production default. README says the stack listens on plain HTTP by default.
- **Impact**: Direct exposure of the Compose web port serves production over HTTP without app-level redirect. Secure cookies reduce session leakage, but login/control pages and public simulation data can still be served without transport protection unless a proxy enforces TLS.
- **False-positive notes**: README recommends a reverse proxy for TLS. Risk remains because the production compose file disables Django's own redirect and exposes `${WEB_PORT:-8000}`.
- **Remediation**: Allow `DJANGO_SECURE_SSL_REDIRECT` to be set from `.env`, default it to true for production, and document proxy-only local exceptions separately.

#### 🟡 risk: Git ignores `.env` but not `.env.*`

- **Location**: `.gitignore:89-90`, `.dockerignore:13-16`, `.env.example:55-67`
- **Evidence**: Git ignores only `.env`. Docker ignores `.env.*` while preserving `.env.example`. The config docs mention deployment-specific secret values such as database and API credentials.
- **Impact**: Operators may create `.env.production`, `.env.local`, or similar secret files that Git will show as untracked and allow to be committed by mistake.
- **False-positive notes**: The main `.env` file is ignored, and `.dockerignore` already has the safer pattern.
- **Remediation**: Add `.env.*` to `.gitignore` and unignore `!.env.example`.

## Remediation timeline

1. Replace and enforce `DJANGO_SECRET_KEY` handling before any production deploy.
1. Remove third-party scripts from login/control pages and fix stored XSS sinks before exposing operator sessions.
1. Add auth or publication allowlists for replay/persona-state pages before storing private simulations on a public host.
1. Cap demo step input to prevent public worker exhaustion.
1. Enforce locked Docker dependencies and pin build images before release builds.
1. Revisit TLS redirect defaults and `.env.*` ignore coverage during deployment cleanup.

## Validation notes

Retest by copying `.env.example` unchanged and confirming production refuses to boot. Visit login, simulator, path tester, replay, demo, and persona-state pages with browser dev tools and confirm no remote scripts load on auth/control pages. Seed movement/chat fields with harmless HTML-like text and confirm it renders as text, not markup. Request demo with an out-of-range step and confirm fast `400` or `404`. Build Docker image with network logs or lock enforcement and confirm package versions match `uv.lock`.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- Production accepts documented placeholder `DJANGO_SECRET_KEY` (high): resolved. `production.py:17-24` now rejects both the base fallback and `.env.example` placeholder before production can boot.
- Replay and persona-state pages expose saved simulation memory without auth (medium): resolved. `views.py:201-204` and `views.py:238-241` now apply `@login_required` before replay storage reads.
- Third-party JavaScript runs on login and operator control pages (medium): resolved for executable scripts. `base.html:12-14` now loads only Bootstrap CSS with SRI, while operator pages load Phaser from local static storage at `home.html:76`, `path_tester.html:13`, and `demo.html:110`. Remaining remote atlas URLs at `home/main_script.html:216-218` and `path_tester/main_script.html:135-137` are data assets, not executable JavaScript.
- LLM-backed simulation text is inserted with `innerHTML` (medium): resolved on active templates. `demo/main_script.html:3-4` and `home/main_script.html:7` use `json_script`; `demo/main_script.html:102-123` and `home/main_script.html:138-159` render text through `textContent` or text nodes. Existing `innerHTML = ""` clears containers only. `home/main_script_old_dolores.html` still contains old `innerHTML` sinks, but no active template includes it.
- Public demo step input can cause unbounded CPU work (medium): resolved. `views.py:32-34` defines `_MAX_DEMO_STEP`, `views.py:67-73` validates step and play speed, and `views.py:88-92` clamps step to available movement data before looping.
- Docker build ignores `uv.lock` and uses floating build tools (low): unresolved. `Dockerfile:28` still copies `uv` from `ghcr.io/astral-sh/uv:latest`, and `Dockerfile:43-44` still installs from `pyproject.toml` instead of enforcing `uv.lock`.
- Compose hardcodes HTTPS redirect off in production (low): unresolved. `docker-compose.prod.yml:35-37` still sets `DJANGO_SECURE_SSL_REDIRECT: "false"`, and `README.md:215-217` documents plain HTTP by default.
- Git ignores `.env` but not `.env.*` (low): unresolved with regression. `.gitignore:89-91` now ignores `.env.*` and unignores `.env.example`, but it does not ignore root `.env`. `git status --short -- .env .gitignore` shows `?? .env`, and `git check-ignore -v .env` returns no match. Raw `.env` content was not read or recorded.

### New findings

No new independent production-hardening findings. The dotenv ignore problem is the same prior secret-exposure thread, now regressed for root `.env`.

### New validation notes

Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest by running `git check-ignore -v .env .env.production .env.example`; `.env` and `.env.production` should be ignored, while `.env.example` should stay tracked. Build with frozen lock enforcement and pinned image tags before release. Confirm production TLS redirect behavior matches deployment topology before public exposure.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- Production accepts documented placeholder `DJANGO_SECRET_KEY` (high): resolved. `production.py:19-24` rejects the empty value, base fallback, and `.env.example` placeholder before production can boot.
- Replay and persona-state pages expose saved simulation memory without auth (medium): resolved. `views.py:201-204` protects replay reads with `@login_required`, and `views.py:238-241` protects persona-state memory reads.
- Third-party JavaScript runs on login and operator control pages (medium): resolved for executable scripts. `base.html:12-14` loads only Bootstrap CSS with SRI. `home.html:76`, `path_tester.html:13`, and `demo.html:110` load Phaser from local static path `vendor/phaser-3.55.2/phaser.js`; the vendored asset identifies `VERSION: '3.55.2'` at `phaser.js:7604`. Remaining `home/main_script.html:216-218` and `path_tester/main_script.html:135-137` loads are remote atlas data assets, not executable JavaScript.
- LLM-backed simulation text is inserted with `innerHTML` (medium): resolved on active templates. `demo/main_script.html:3-4` and `home/main_script.html:7` use `json_script`; `demo/main_script.html:102-123` and `home/main_script.html:138-159` render untrusted fields through `textContent` or text nodes. Current `innerHTML = ""` uses only clear containers at `demo/main_script.html:110` and `home/main_script.html:146`. `home/main_script_old_dolores.html` still has old sinks but is not included by active templates.
- Public demo step input can cause unbounded CPU work (medium): resolved. `views.py:67-73` validates step and play speed, `_MAX_DEMO_STEP` caps user input at `views.py:32-34`, and `views.py:88-92` clamps to available movement data.
- Docker build ignores `uv.lock` and uses floating build tools (low): unresolved. `Dockerfile:28` still copies `uv` from `ghcr.io/astral-sh/uv:latest`, and `Dockerfile:43-44` still installs from `pyproject.toml` with `uv pip install` instead of enforcing `uv.lock`.
- Compose hardcodes HTTPS redirect off in production (low): unresolved. `docker-compose.prod.yml:33-39` still hardcodes `DJANGO_SECURE_SSL_REDIRECT: "false"`, overriding the secure default in `production.py:38-40`; `README.md:215-217` still documents plain HTTP by default.
- Git ignores `.env` but not `.env.*` (low): unresolved. `.gitignore:89-91` ignores `.env.*` and unignores `.env.example`, but it still omits root `.env`. `git check-ignore -v .env .env.production .env.example` matched `.env.production` and `.env.example` only; `git status --short -- .env .env.production .env.example .gitignore` showed `?? .env`. Raw `.env` content was not read or recorded.

### New findings

No new independent production-hardening findings in this final pass. The local Phaser asset removes the prior remote executable JavaScript risk but should still be treated as vendored third-party code during release review.

### New validation notes

Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest by running `git check-ignore -v .env .env.production .env.example`; root `.env` and `.env.production` should be ignored, while `.env.example` should remain trackable. Build with frozen lock enforcement and pinned `uv` image tag or digest. Confirm production TLS behavior behind the intended reverse proxy before public exposure.

## Update: 2026-06-19 by security-review-specialist

### Prior finding status

- Production accepts documented placeholder `DJANGO_SECRET_KEY` (high): resolved. `production.py:19-24` rejects the empty value, base fallback, and `.env.example` placeholder before production boot.
- Replay and persona-state pages expose saved simulation memory without auth (medium): resolved. `views.py:201-204` protects replay reads, and `views.py:238-241` protects persona-state memory reads.
- Third-party JavaScript runs on login and operator control pages (medium): resolved for executable scripts. `base.html:12-14` loads only SRI-pinned Bootstrap CSS, while `home.html:76`, `path_tester.html:13`, and `demo.html:110` load Phaser from local static storage.
- LLM-backed simulation text is inserted with `innerHTML` (medium): resolved on active templates. `demo/main_script.html:3-4` and `home/main_script.html:7` use `json_script`; `demo/main_script.html:102-123` and `home/main_script.html:138-159` render untrusted chat text through `textContent` or text nodes. Current active `innerHTML` writes at `demo/main_script.html:110` and `home/main_script.html:146` only clear containers.
- Public demo step input can cause unbounded CPU work (medium): resolved. `views.py:67-73` validates step and play speed, `_MAX_DEMO_STEP` caps user input at `views.py:32-34`, and `views.py:88-92` clamps to available movement data before looping.
- Docker build ignores `uv.lock` and uses floating build tools (low): resolved. `Dockerfile:28` pins `uv` by digest, and `Dockerfile:43-45` exports from `uv.lock` with `uv export --frozen` before installing dependencies.
- Compose hardcodes HTTPS redirect off in production (low): resolved. `docker-compose.prod.yml:37-39` makes `DJANGO_SECURE_SSL_REDIRECT` env-driven with secure default `true`; `production.py:38-40` also defaults redirect on; `README.md:217-223` documents disabling only for no-proxy local cases.
- Git ignores `.env` but not `.env.*` (low): resolved. `.gitignore:89-92` ignores `.env` and `.env.*` while unignoring `.env.example`; `git check-ignore -v .env .env.production .env.example` matched those rules and did not require reading secret content.

### New findings

No new independent production-hardening findings in this final pass. Residual release hardening not tracked as blocker: `python:3.12-slim` and `postgres:16-alpine` remain tag-pinned rather than digest-pinned, but prior `uv` and lockfile findings are closed.

### New validation notes

Used static source and diff review only. No Docker, scanner, network, dependency install, or active runtime tests run. Retest by booting production with copied `.env.example` unchanged and confirming startup fails on `DJANGO_SECRET_KEY`, then booting with a generated key and confirming HTTPS redirect behavior behind the intended reverse proxy. Build once and confirm dependency versions come from `uv.lock`.
