---
status: completed
created_at: 2026-06-19
updated_at: 2026-08-01
files_edited:
  - .gitignore
  - .dockerignore
  - .env.example
  - Dockerfile
  - Dockerfile.db
  - docker-compose.prod.yml
  - scripts/init_storage.sh
  - README.md
  - pyproject.toml
  - uv.lock
  - environment/frontend_server/Procfile
  - environment/frontend_server/manage.py
  - environment/frontend_server/frontend_server/settings/__init__.py
  - environment/frontend_server/frontend_server/settings/base.py
  - environment/frontend_server/frontend_server/settings/dev.py
  - environment/frontend_server/frontend_server/settings/production.py
  - environment/frontend_server/frontend_server/urls.py
  - environment/frontend_server/db.sqlite3
  - environment/frontend_server/frontend_server/db.sqlite3
  - environment/frontend_server/frontend_server/wsgi.py
  - environment/frontend_server/translator/test_simulator_scripts.py
  - environment/frontend_server/translator/tests.py
  - environment/frontend_server/translator/views.py
  - environment/frontend_server/templates/base.html
  - environment/frontend_server/templates/demo/demo.html
  - environment/frontend_server/templates/demo/main_script.html
  - environment/frontend_server/templates/home/home.html
  - environment/frontend_server/templates/home/main_script.html
  - environment/frontend_server/templates/path_tester/path_tester.html
  - environment/frontend_server/templates/path_tester/main_script.html
  - environment/frontend_server/templates/registration/login.html
  - environment/frontend_server/static_dirs/vendor/phaser-3.55.2/phaser.js
  - reverie/backend_server/durability.py
  - reverie/backend_server/reverie.py
  - reverie/backend_server/test.py
  - reverie/backend_server/test_artifact_pruning.py
  - reverie/backend_server/test_checkpoints.py
  - reverie/backend_server/test_durability.py
  - reverie/backend_server/test_embedding_privacy.py
  - reverie/backend_server/test_llm_config_hardening.py
  - reverie/backend_server/test_llm_provider_retries.py
  - reverie/backend_server/test_memory_durability.py
  - reverie/backend_server/test_reverie_lifecycle.py
  - reverie/backend_server/test_scratch_serialization.py
  - reverie/backend_server/utils.py
  - reverie/backend_server/persona/cognitive_modules/converse.py
  - reverie/backend_server/persona/cognitive_modules/execute.py
  - reverie/backend_server/persona/cognitive_modules/perceive.py
  - reverie/backend_server/persona/cognitive_modules/plan.py
  - reverie/backend_server/persona/cognitive_modules/reflect.py
  - reverie/backend_server/persona/cognitive_modules/retrieve.py
  - reverie/backend_server/persona/memory_structures/associative_memory.py
  - reverie/backend_server/persona/memory_structures/scratch.py
  - reverie/backend_server/persona/memory_structures/spatial_memory.py
  - reverie/backend_server/persona/prompt_template/gpt_structure.py
  - reverie/backend_server/persona/prompt_template/run_gpt_prompt.py
  - substrate/traces/reviews/2026-06-19-production-hardening-security.md
  - substrate/traces/reviews/2026-06-19-llm-provider-config-security.md
rationale:
  - Move the repository from a legacy local-only posture to a production-oriented deployment shape with supported Python and Django versions.
  - Harden Django settings, operator authentication, browser surfaces, and backend service behavior so the app can be deployed with explicit production controls.
  - Add reproducible container artifacts and deployment documentation aligned with the repo's file-based frontend/backend coordination model.
supporting_docs:
  - https://www.djangoproject.com/download/
  - https://docs.djangoproject.com/en/6.0/howto/deployment/checklist/
  - https://devguide.python.org/versions/
  - substrate/traces/reviews/2026-06-19-production-hardening-security.md
  - substrate/traces/reviews/2026-06-19-llm-provider-config-security.md
---

# Production hardening

## Summary of changes

The repository now targets a supported production baseline with Django 5.2 on Python 3.12+, explicit dev and production settings, operator authentication for private simulation surfaces, hardened browser templates, a resumable non-interactive backend service mode, and a reproducible Docker Compose deployment path.

## Technical reasoning

The previous codebase still carried local-research defaults that were incompatible with a production claim: Django 2.2, Python 3.9, hardcoded secrets, disabled CSRF protection, public replay surfaces, browser templates that trusted unescaped simulation content, and an interactive backend that required stdin to run. The hardening work addressed those issues without rewriting the core Smallville architecture.

The Django frontend was refactored to use env-driven settings and separate `dev` and `production` modules. This made it possible to enforce secure cookies, HSTS, SSL redirect defaults, WhiteNoise static serving, authentication-backed operator pages, and production deploy checks. Sensitive replay and persona-state routes were moved behind authentication while the explicit public demo route remained available.

On the frontend side, executable third-party JavaScript was removed from login and operator pages, Phaser was vendored locally, and previously unsafe `innerHTML` paths were converted to safer `json_script`, `textContent`, and DOM-node rendering patterns. This removed the active stored-XSS paths identified during security review.

The Reverie backend kept backward-compatible interactive mode while gaining a non-interactive `--serve` mode with resume support and autosave behavior, which makes it deployable as a supervised service. Container artifacts were then added around that model, preserving the repository's shared-volume storage handshake instead of forcing a larger architectural rewrite.

## Impact assessment

Operators can now run the project through either direct `uv` commands or the new `docker-compose.prod.yml` stack. The production path is documented, reproducible, and secured by default, while opt-out exceptions such as plain HTTP without a reverse proxy now require an explicit configuration override instead of being silently enabled.

Security posture improved materially across three layers:

- application settings and auth controls
- browser-side rendering and asset loading
- build and deployment reproducibility

The main architectural trade-off remains intentional: the frontend and backend still coordinate through shared storage and temp files. The hardening work made that model safer and deployable, but it did not replace it with a network-native service protocol.

## Validation steps

1. Verified Python and Django integrity with `uv run python -m compileall -q reverie environment/frontend_server`.
2. Verified Django runtime configuration with `uv run python manage.py check`.
3. Verified production settings with `DJANGO_SECRET_KEY="..." DJANGO_ENV=production uv run python manage.py check --deploy --settings=frontend_server.settings.production`.
4. Verified auth protections through Django test-client redirects for replay and simulator routes.
5. Verified production secret enforcement by confirming that placeholder production keys fail closed.
6. Verified the backend service CLI with `uv run python reverie.py --help` and a resume-mode startup smoke test.
7. Verified container build and runtime with:
   - `docker build -t smallville:test .`
   - `docker run --rm smallville:test python -c "import django; print(django.get_version())"`
   - `docker run --rm --entrypoint sh smallville:test -c "cd /app/environment/frontend_server && python manage.py --help >/dev/null && gunicorn --version"`
   - `docker compose -f docker-compose.prod.yml config --no-interpolate`
8. Ran `security-review-specialist` after each hardening pass and reviewed the resulting updates in both security review documents.

The security review resolved every finding it raised during the hardening passes. It did not claim the branch had passed a production-readiness gate at that point: it recorded one residual release-hardening item (the base images were tag-pinned rather than digest-pinned). The dependency and image remediation update below addresses that residual, and the overall production-readiness conclusion for the branch is verified separately rather than asserted here.

## Update: 2026-08-01 dependency and image remediation

### Summary of new work

A follow-up scan found vulnerable packages in the original lockfile and high or critical findings in the Debian runtime image. Dependency floors and `uv.lock` were updated, stale requirements manifests were removed, the unused `gensim` dependency was dropped, and the runtime image moved to a digest-pinned Alpine base with a digest-pinned uv build helper.

### Technical reasoning

Trivy reported a critical NLTK issue and additional fixed vulnerabilities in `aiohttp`, Pillow, `python-dotenv`, Requests, and urllib3. The lockfile now resolves scanner-fixed releases. The Debian image also contained operating-system findings without available Debian fixes, so the runtime moved to the scanner-clean Alpine Python image. Build-only uv, pip, compilers, and temporary caches are removed from the final image.

### Impact assessment

`pyproject.toml` and `uv.lock` are now the only dependency manifests. At the time of the update, the production image contained no scanner-detected operating-system or Python-package vulnerability. The Dockerfile intentionally omits a generic image-level healthcheck because the same image serves both one-shot provisioning (the `seed` job) and two long-running services (`web` and `backend`) with different readiness semantics. `docker-compose.prod.yml` defines dedicated healthchecks for `web` and `backend`, while the one-shot `seed` service relies on completion ordering and idempotent execution.

### Validation steps

1. Ran `trivy fs --scanners vuln,secret,misconfig --exit-code 0 .`; `uv.lock` reported zero vulnerabilities and no secrets were detected. The only filesystem note was the intentionally delegated service-level healthcheck.
2. Built the hardened image with `docker build -t smallville:test .`.
3. Ran `trivy image --scanners vuln,secret,misconfig --exit-code 0 smallville:test`; the final Alpine and Python package targets reported zero vulnerabilities.
4. Verified imports in the image with `docker run --rm smallville:test python -c "import django, numpy, scipy, psycopg2; print(django.get_version())"`.
5. Re-ran frozen uv synchronization, compilation, Django checks, production deploy checks, and Compose validation.

## Update: 2026-08-01 runtime hardening completion

### Summary of new work

The remediation evidenced in the previous update was completed with the remaining runtime hardening. The application image is built from a digest-pinned Alpine base, a derived PostgreSQL image removes the vulnerable `gosu` binary, dependency installation is hash-enforced, a deterministic seed service provisions the base simulations while the compressed demo ships in the image at its runtime path, Django logging is stdout-safe, production authentication and validation are tightened, the backend saves its state on SIGTERM, persona-aware embedding routing is in place, and sensitive logs are gated. Verification now rests on 47 Django tests and 52 backend tests.

### Technical reasoning

- The `seed` service in `docker-compose.prod.yml` runs `scripts/init_storage.sh`, which copies the base simulations from `/app/seed_storage` into the shared storage volume on first run and never overwrites existing data. This makes base simulation provisioning deterministic across deploys. The compressed demo data (`compressed_storage`) ships in the image at its runtime path and needs no seeding, so the public demo route works out of the box while the large pre-run July1 step folders stay out of the image via `.dockerignore`.
- Django logging in `base.py` and `production.py` is now stdout-safe: both settings modules route through a console `StreamHandler` only, with no file handlers. This keeps container logs on standard output instead of writing to an unmounted filesystem path, and it is asserted by a dedicated production settings test.
- Production authentication and validation were tightened. `production.py` fails closed unless `DJANGO_SECRET_KEY` is a strong, non-placeholder value of at least 50 characters. `base.py` defines `LOGIN_URL` and `LOGIN_REDIRECT_URL` explicitly, private operator routes stay behind `@login_required`, and invalid input (malformed JSON, invalid sim code or step) is rejected with `400`/`404` responses.
- The test suite was expanded to 47 Django tests in `environment/frontend_server/translator/tests.py` and 52 backend tests across `test_embedding_privacy.py` (12), `test_llm_config_hardening.py` (26), and `test_reverie_lifecycle.py` (14). The Django tests cover auth redirects, login flow, CSRF and method enforcement, input validation, demo and replay limits, the health endpoint, production settings policy, and reverie test-module import safety. The backend tests cover per-persona embedding routing, gated sensitive prints, LLM config hardening (malformed JSON fail-fast, separate embedding versus chat model selection, OpenRouter attribution), fresh-fork and resume semantics, the first movement step on a fresh fork, and SIGTERM save behavior, all without touching the network.
- Persona-aware embedding routing was added to `get_embedding`: the provider and model resolve from persona context (scratch fields, then the per-persona env map, then global defaults), and unknown providers fail closed before any API call. Every active embedding caller in the cognitive modules threads `persona=persona`, and sensitive normal-run prints (memories, plans, insights, schedules, prompts, and LLM outputs) are gated behind `debug`/`verbose`.
- Service mode now saves on SIGTERM. `_install_sigterm_handler` converts SIGTERM into `KeyboardInterrupt`, so `serve_forever` runs its existing save-on-interrupt path before exiting, matching container runtime shutdown semantics. This is verified by a subprocess-based lifecycle test.
- Dependency installation is hash-enforced in the Dockerfile: `uv export --frozen --no-dev` writes the lockfile hashes into a requirements file and `uv pip install --system --require-hashes` consumes it, so no package can resolve outside `uv.lock`.
- The runtime images were hardened. The application image uses a digest-pinned `python:3.12-alpine` base. A derived PostgreSQL image (`Dockerfile.db`) uses a digest-pinned `postgres:16-alpine` base and removes the unused `gosu`/`su-exec` binaries, which carry Go stdlib CVEs (1 critical, 14 high under Trivy); `docker-compose.prod.yml` runs the db service as uid/gid 70 (postgres), so the root-drop path gosu serves is dead code.

### Impact assessment

Scanner evidence measured at the time of the update: the application image reported zero vulnerabilities, the hardened database image reported zero vulnerabilities, and `uv.lock` reported zero vulnerabilities. The only remaining note is the low-severity generic Dockerfile `HEALTHCHECK` absence, which is intentional because the single application image serves both web and backend roles with different readiness semantics; `docker-compose.prod.yml` defines dedicated healthchecks for the web, backend, and db services, while the one-shot seed service relies on completion ordering and idempotent execution (web and backend start only after the seed service completes successfully).

The embedded test suite now protects the auth, validation, logging, embedding-privacy, and lifecycle guarantees that the hardening depends on, so regressions surface in CI or `uv run` verification rather than in a deployed container. Base simulations provision deterministically and the public demo route works without operator setup.

This update records completed remediation and the scanner evidence measured at the time of the update. The final external quality and security gate for the branch is run separately as part of the release process, not pre-claimed here.

### Validation steps

1. Ran the Django suite with `uv run python manage.py test` from `environment/frontend_server` and confirmed 47 tests pass.
2. Ran the Reverie suite with `uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v` and confirmed 52 tests pass (12 embedding-privacy, 26 config hardening, 14 lifecycle).
3. Built the application image with `docker build -t smallville:test .` and re-ran `trivy image --scanners vuln,secret,misconfig --exit-code 0 smallville:test`; zero vulnerabilities in the application image.
4. Built the hardened database image with `docker build -f Dockerfile.db -t smallville-db:test .` and ran `trivy image --scanners vuln,secret,misconfig --exit-code 0 smallville-db:test`; zero vulnerabilities with `gosu`/`su-exec` removed.
5. Re-ran `trivy fs --scanners vuln,secret,misconfig --exit-code 0 .`; `uv.lock` reported zero vulnerabilities and no secrets; the only note was the delegated service-level healthcheck.
6. Verified the seed service by reviewing `scripts/init_storage.sh` and the Compose up order (seed completes before web and backend start).
7. Verified hash-enforced dependency resolution by inspecting the Dockerfile `uv export`/`--require-hashes` pipeline and re-running frozen uv synchronization.
8. Verified SIGTERM save behavior through the lifecycle test's subprocess assertion and re-ran compilation, Django checks, production deploy checks, and Compose validation.

## Update: 2026-08-01 documented state correction

### Summary of new work

This documentation-only update aligns this record and the README with the latest verified state. Several behaviors were finalized after the previous sections were written: `DJANGO_ENV` accepts only `dev` and `production` and fails closed on anything else, production settings reject DEBUG and SQLite and require an external database, Compose passes separate `DB_*` fields instead of a connection URL, the broad `env_file` injection was removed, runtime storage is excluded from the image wholesale, the tracked `db.sqlite3` files were removed, the fresh-fork first movement step and runtime directory creation were fixed, service mode fails fast on missing codes, browser debug logging was removed, the source-build-only scikit-learn dependency was removed, and install artifacts are hash-enforced. The Django suite contains 50 tests; the earlier 37 figure recorded in the previous update was miscounted and is corrected here.

### Technical reasoning

- Settings selection: `frontend_server/settings/__init__.py` maps `DJANGO_ENV` to exactly `dev` or `production` and raises `ImproperlyConfigured` for any other value, so a mistyped environment can never boot the wrong settings module. Django tests cover unknown, empty, default, and explicit env values.
- Production fail-closed: `production.py` refuses startup when `DJANGO_DEBUG` is enabled, when `DATABASE_URL` resolves to SQLite, or when neither `DATABASE_URL` nor the five `DB_*` fields are present. Django tests cover each branch, including a database password that contains URL-significant characters.
- Compose database wiring: `docker-compose.prod.yml` passes `DB_HOST`, `DB_NAME`, `DB_PASSWORD`, `DB_PORT`, and `DB_USER` to the web service directly and never assembles a `DATABASE_URL`, so passwords containing characters such as `@`, `:`, `/`, or `%` need no URL escaping. The db service receives only `POSTGRES_*` variables.
- Env scoping: the previous broad `env_file` injection was removed. Each service declares an explicit `environment:` block: web gets Django and database fields, backend gets LLM provider and simulation fields, db gets `POSTGRES_*` only. Secrets stay scoped to the service that consumes them.
- Image content: `.dockerignore` ignores `environment/frontend_server/storage/*` wholesale and allowlists only the two immutable base simulations, which the Dockerfile copies to `/app/seed_storage`. `temp_storage/`, `static_root/`, `media_root/`, and `*.sqlite3` never enter the image.
- SQLite removal: the tracked `environment/frontend_server/db.sqlite3` and `environment/frontend_server/frontend_server/db.sqlite3` files were removed from the working tree and `*.sqlite3` is ignored. The web service initializes the database with `migrate --noinput` at container start, so production never uses a committed database file. The removed files remain in Git history (added in the initial commit of July 2023), so any credential hashes they contain must be treated as compromised. Rotation of affected credentials and history cleanup for private forks are recommended; the public history has not been rewritten.
- Backend fixes: `reverie.py` now creates `movement/`, `environment/`, and `temp_storage/` during initialization when missing, a fresh fork writes `movement/0.json` on its first step, and `--serve` exits nonzero via `_fail_serve` when the target or origin code is missing instead of prompting on a closed stdin.
- Browser logging: active `console.log`/`debugger` statements were removed from the served templates and application JavaScript; only commented-out debugging references remain, and the vendored third-party Phaser file is untouched.
- Dependencies: scikit-learn, which has no Alpine wheel and would have forced a source build with a compiler chain into the image, was removed. All frozen install artifacts are hash-enforced through `uv export --frozen --no-dev` plus `uv pip install --require-hashes`.
- Documentation hygiene: the README carries the same verified statements, including a warning to rotate the local `.env` secret key that does not disclose the key itself. This record and the README contain no em dashes and claim no production-readiness gate pass; the final external gate remains a separate release step.

### Impact assessment

Readers of this record or the README now see one consistent, verified picture of the deployment: fail-closed environment selection, external PostgreSQL only in production, scoped environment injection, a reproducible image that ships only immutable base seeds, and a database initialized through migrations. The corrected test count (50 Django, 52 backend) matches the suite that CI and `uv run` verification execute today. Anyone who previously used the tracked SQLite files should rotate the affected credentials; the public history has not been rewritten, so the old hashes remain reachable.

### Validation steps

1. Read the settings loader, the dev and production settings modules, `docker-compose.prod.yml`, `Dockerfile`, `.dockerignore`, `.env.example`, `reverie.py`, and the test files to verify every statement against the current code.
2. Counted tests: `environment/frontend_server/translator/tests.py` contains 47 `test_` methods; the backend test files contain 52 (12 embedding-privacy, 26 config hardening, 14 lifecycle).
3. Confirmed the absence of active `console.log`/`debugger` statements in the served templates and application JavaScript; only commented-out references remain, and the vendored `phaser.js` is the only file with third-party console output.
4. Confirmed `scikit-learn` appears in no dependency manifest and that `uv.lock` provides musllinux wheels for numpy and scipy.
5. Confirmed the tracked `db.sqlite3` files remain in Git history (initial commit, July 2023) and are deleted in the working tree, with `*.sqlite3` ignored.
6. Grepped both edited files for em dashes and en dashes (none found) and confirmed the diff touches only the two documentation files.

## Update: 2026-08-01 domain-only host healthcheck correction

### Summary of new work

The production web stack now accepts a strictly domain-only host allowlist while keeping its container healthcheck healthy. Compose appends the internal `web` host to the effective container `DJANGO_ALLOWED_HOSTS`, and the web healthcheck probes `http://web:8000/health/` with the `X-Forwarded-Proto: https` header. An operator can set `DJANGO_ALLOWED_HOSTS=example.com` with `DJANGO_CSRF_TRUSTED_ORIGINS=https://example.com`, and the container stays healthy while unrelated hosts are rejected. Three regression tests were added for the health endpoint host allowlist, bringing the current Django total to 50 while the backend stays at 52. An actual domain-only Compose integration run became healthy.

### Technical reasoning

- Compose appends the internal host: `docker-compose.prod.yml` sets `DJANGO_ALLOWED_HOSTS: "${DJANGO_ALLOWED_HOSTS:-localhost,127.0.0.1},web"`. An operator who sets `DJANGO_ALLOWED_HOSTS=example.com` therefore still allows the container-to-container probe over `Host: web` without widening the allowlist to a public wildcard, so a domain-only configuration cannot break the web service healthcheck.
- The web healthcheck probes `http://web:8000/health/` and attaches the `X-Forwarded-Proto: https` header. This matches the reverse-proxy contract in production settings (`SECURE_PROXY_SSL_HEADER`, plus the `DJANGO_SECURE_SSL_REDIRECT=true` default), so the probe passes while SSL redirect is enabled.
- Operator configuration stays explicit: `DJANGO_ALLOWED_HOSTS=example.com` plus `DJANGO_CSRF_TRUSTED_ORIGINS=https://example.com` lets the public domain through CSRF checks, while Django rejects any unrelated `Host` (for example `evil.example.net`) with `400` before the view runs.
- Three regression tests were added in `HealthHostAllowlistTests`: `test_health_allows_internal_probe_host`, `test_health_allows_external_domain`, and `test_health_rejects_unrelated_host`. With these, `environment/frontend_server/translator/tests.py` contains 50 `test_` methods, up from the 47 recorded in the validation steps of the preceding updates, and the backend suite stays at 52.

### Impact assessment

An operator can now run the production stack with a single-domain allowlist and a matching HTTPS CSRF trusted origin while the internal healthcheck keeps the web service healthy, because Compose preserves the `web` host the probe targets. Any unrelated host is rejected. The three regression tests guard the health endpoint host allowlist so the domain-only configuration cannot silently break container health checks. The current Django suite total is 50 and the backend stays at 52. An actual Compose integration run with a domain-only `DJANGO_ALLOWED_HOSTS` and matching `DJANGO_CSRF_TRUSTED_ORIGINS` became healthy, which closes the gap between the documented domain-only guidance and the container behavior.

### Validation steps

1. Inspected `docker-compose.prod.yml` and confirmed the `,web` suffix appended to `DJANGO_ALLOWED_HOSTS` and the `http://web:8000/health/` probe with the `X-Forwarded-Proto: https` header.
2. Reviewed `HealthHostAllowlistTests` in `environment/frontend_server/translator/tests.py` and confirmed the three regression tests for the internal probe host, the external domain, and unrelated host rejection.
3. Counted `test_` methods in the current suite: 50 in `environment/frontend_server/translator/tests.py` and 52 across the backend test files (12 embedding-privacy, 26 config hardening, 14 lifecycle).
4. Recorded the actual domain-only Compose integration run: with `DJANGO_ALLOWED_HOSTS` set to a single domain and `DJANGO_CSRF_TRUSTED_ORIGINS` set to the matching HTTPS origin, the web service became healthy and unrelated hosts were rejected.

## Update: 2026-08-01 null-safe scratch serialization and fresh-fork SIGTERM persistence

### Summary of new work

`Scratch.save` and `Scratch.load` are now null-safe for `curr_time` and `act_start_time`, so seeded bootstrap scratch files that carry `null` for both no longer crash on save and a null `act_start_time` no longer clears `curr_time` on load. A real SIGTERM delivered to a fresh fork before its first movement now cleanly persists the persona `scratch.json` and the `meta.json` through the real save path rather than a mocked one. Verification rests on six serialization tests in the new `test_scratch_serialization.py` and one new lifecycle test, bringing the current backend total to 59 while Django stays at 50. The README now states the service-mode fail-fast rules precisely: a missing target always fails, while a missing origin fails a new fork but is allowed with `--resume` when the existing target metadata supplies the origin.

### Technical reasoning

- Null-safe save: the seeded bootstrap `scratch.json` files ship with `curr_time` and `act_start_time` set to `null`. `Scratch.save` previously called `strftime` on `None` and crashed; it now writes JSON `null` for either null timestamp while keeping the existing datetime format for non-null values. The six serialization tests cover null save, non-null save, null load, non-null load, and both round-trip directions.
- Load independence: the load branch for `act_start_time` previously set `curr_time = None` in its else clause, which cleared the persona clock whenever `act_start_time` was null. That else clause now sets `act_start_time = None` instead, so a null `act_start_time` leaves `curr_time` intact.
- Real fresh-fork SIGTERM: the earlier lifecycle suite verified SIGTERM save in service mode with a mocked save. A subprocess-based test now delivers a real SIGTERM to a fresh fork before its first movement and runs the real save path, asserting that the persisted `scratch.json` keeps null timestamps and the persisted `meta.json` records `step: 0`. Only the movement step is suppressed so the test stays deterministic and offline.
- Service-mode fail fast: the README now distinguishes the two fail-fast branches. A missing target always exits nonzero in service mode. A missing origin also exits nonzero for a fresh fork, but with `--resume` the origin may be omitted because resuming an existing target loads `fork_sim_code` from the target's saved `meta.json`.

### Impact assessment

The current backend suite totals 59 tests: 12 in `test_embedding_privacy.py`, 26 in `test_llm_config_hardening.py`, 15 in `test_reverie_lifecycle.py`, and 6 in the new `test_scratch_serialization.py`. The lifecycle total grew from the 14 recorded in the preceding sections to 15. The earlier sections of this record that report 52 backend tests describe the suite as it stood at those points in time and are preserved for chronology; the counts in this update and in the README describe the current state. The Django suite remains at 50 tests. The fresh-fork SIGTERM regression means a container stopped before its first movement still persists the simulation with its null timestamps intact. This record and the README contain no em dashes and claim no production-readiness gate pass; the final external quality and security gate for the branch remains a separate release step.

### Validation steps

1. Ran the backend suite with `uv run python -m unittest discover -s reverie/backend_server -p "test_*.py"` and confirmed 59 tests pass (12 embedding-privacy, 26 config hardening, 15 lifecycle, 6 scratch serialization).
2. Ran the Django suite with `uv run python manage.py test` from `environment/frontend_server` and confirmed 50 tests pass.
3. Reviewed `scratch.py` load and save and confirmed null-safe handling for `curr_time` and `act_start_time`, with the load branches independent so a null `act_start_time` cannot clear `curr_time`.
4. Reviewed `FreshForkSigtermPersistenceTests` in `test_reverie_lifecycle.py` and confirmed it uses a real, unmocked save path in a subprocess with a real SIGTERM delivered before the first movement, then asserts null timestamps in the persisted scratch and `step: 0` in the persisted meta.
5. Confirmed `test_scratch_serialization.py` contains six `test_` methods covering null-safe save, load, and round-trip behavior.
6. Counted `test_` methods and confirmed the frontmatter `files_edited` list includes `test_scratch_serialization.py`.
7. Grepped both edited files for em dashes and en dashes (none found) and confirmed the diff touches only the two documentation files.

## Update: 2026-08-01 scratch load resource fix

### Summary of new work

`Scratch` JSON load now reads through a context-managed UTF-8 file handle, so the persona bootstrap file is closed deterministically and no `ResourceWarning` surfaces on load. Two warning-regression tests were added to `test_scratch_serialization.py`, which now holds 8 tests, bringing the current backend suite total to 61 while the Django suite stays at 50. The README records the current backend total as 61.

### Technical reasoning

- `Scratch.__init__` previously loaded a persona's bootstrap scratch with `json.load(open(f_saved))`, leaking the raw file handle. An unclosed file surfaces as a `ResourceWarning` when the `FileIO` object is deallocated, which the former load produced on every persona bootstrap. The load path now uses `with open(f_saved, encoding="utf-8") as f: json.load(f)`, which closes the handle deterministically instead of waiting for garbage collection and pins UTF-8 decoding explicitly.
- The guarantee is regression-tested using the existing warning-capture harness. `test_scratch_serialization.py` gained a `ScratchNoResourceWarningTests` class with `test_load_emits_no_resource_warning` and `test_roundtrip_emits_no_resource_warning`. Its `_load_scratch` helper enables `ResourceWarning` filtering and treats any captured warning as a test failure, and the roundtrip test covers both the load and save paths.

### Impact assessment

The current backend suite totals 61 tests: 12 in `test_embedding_privacy.py`, 26 in `test_llm_config_hardening.py`, 15 in `test_reverie_lifecycle.py`, and 8 in `test_scratch_serialization.py` (up from the six recorded in the preceding update). The earlier sections of this record that report 52 or 59 backend tests describe the suite as it stood at those points in time and are preserved for chronology; the counts in this update and in the README describe the current state. The Django suite remains at 50 tests. Because the load path closes its file handle and pins the encoding, sustained runs no longer accumulate unclosed read handles on persona bootstrap and scratch bytes are decoded as UTF-8 regardless of the platform default encoding. This record and the README contain no em dashes and claim no production-readiness gate pass; the final external quality and security gate for the branch remains a separate release step.

### Validation steps

1. Ran the backend suite with `uv run python -m unittest discover -s reverie/backend_server -p "test_*.py"` and confirmed 61 tests pass (12 embedding-privacy, 26 config hardening, 15 lifecycle, 8 scratch serialization).
2. Ran the Django suite with `uv run python manage.py test` from `environment/frontend_server` and confirmed 50 tests pass.
3. Reviewed the `Scratch` load path in `scratch.py` and confirmed the JSON read uses `with open(f_saved, encoding="utf-8")` in place of the previous `json.load(open(f_saved))`, so the file handle closes deterministically and UTF-8 is explicit.
4. Reviewed `ScratchNoResourceWarningTests` in `test_scratch_serialization.py` and confirmed the two warning-regression tests (`test_load_emits_no_resource_warning` and `test_roundtrip_emits_no_resource_warning`) and the `_load_scratch` helper that fails on any captured `ResourceWarning`.
5. Counted `test_` methods and confirmed the current backend total of 61 and the Django total of 50, and confirmed the frontmatter `files_edited` list already includes `test_scratch_serialization.py`.
6. Grepped both edited files for em dashes and en dashes (none found) and confirmed the diff touches only the two documentation files.

## Update: 2026-08-01 resume environment fallback and post-step SIGTERM regression

### Summary of new work

`ReverieServer` resume initialization now prefers the environment snapshot for the current step when it is valid and otherwise loads the latest valid prior snapshot without decrementing the step. Saved step metadata that is negative, non-integer, or otherwise invalid fails clearly, as does a simulation with no valid environment snapshot at all. A subprocess-based regression test reproduces a real post-step SIGTERM: the service processes environment/0 and movement/0, saves meta with step 1 before environment/1 exists, resumes at step 1 from environment/0, and then processes environment/1 when it arrives. Six lifecycle tests were added (five resume environment fallback tests and one post-step SIGTERM regression test), bringing the current backend total to 67 while Django stays at 50.

### Technical reasoning

- Step validation: `_resolve_environment_snapshot` rejects step metadata that is not a non-negative integer (covering negative numbers, non-integer values such as `"../../etc"`, and the booleans `true`/`false`) with a clear `ValueError` naming the invalid value before any file access, so a malformed `meta.json "step"` can never be used to derive a filesystem path.
- Snapshot resolution: the resolver first tries `environment/{step}.json`. When that file is absent or invalid (unreadable, not valid JSON, not an object, or missing the `x`/`y` tile fields for every persona) and `step > 0`, it walks the prior snapshots from `step-1` down to `0` and loads the latest valid one, leaving `self.step` unchanged.
- Fail-closed on no snapshot: when no candidate snapshot is valid, resume raises `FileNotFoundError` with a message that names the simulation and the searched snapshots, so a partial simulation cannot resume into an undefined spatial state.
- Real post-step SIGTERM regression: after the service processes `environment/0` it increments the step to 1, writes `movement/0.json`, and saves `meta.json` with step 1. If SIGTERM arrives before the frontend writes `environment/1`, a naive resume would look for `environment/1.json` and fail. The regression test delivers a real SIGTERM at that point, resumes with `ReverieServer(None, "target", resume=True)`, asserts the step stays at 1 and persona tiles initialize from `environment/0`, then writes `environment/1`, processes it, and asserts `movement/1.json` is produced with the new coordinates. Persistence is real; only the persona move is faked, keeping the test deterministic and offline.
- Test counts: `test_reverie_lifecycle.py` grew by 6 tests (`ResumeEnvironmentFallbackTests` with five tests and `ResumeAfterSigtermRegressionTests` with one test), from 15 to 21. The backend suite now totals 67 tests (12 embedding privacy, 26 config hardening, 21 lifecycle, 8 scratch serialization) while Django stays at 50.

### Impact assessment

The current backend suite totals 67 tests: 12 in `test_embedding_privacy.py`, 26 in `test_llm_config_hardening.py`, 21 in `test_reverie_lifecycle.py`, and 8 in `test_scratch_serialization.py`. The earlier sections of this record that report 52, 59, and 61 backend tests describe the suite as it stood at those points in time and are preserved for chronology; the counts in this update and in the README describe the current state. The Django suite remains at 50 tests. A container stopped immediately after completing a step now resumes cleanly at the saved step from the latest valid environment snapshot, and a malformed `meta.json` step or a simulation with no usable snapshot fails fast with a clear error instead of failing obscurely or resuming into an undefined world state. This record and the README contain no em dashes and claim no production-readiness gate pass; the final external quality and security gate for the branch remains a separate release step.

### Validation steps

1. Ran the backend suite with `uv run python -m unittest discover -s reverie/backend_server -p "test_*.py"` and confirmed 67 tests pass (12 embedding-privacy, 26 config hardening, 21 lifecycle, 8 scratch serialization).
2. Ran the Django suite with `uv run python manage.py test` from `environment/frontend_server` and confirmed 50 tests pass.
3. Reviewed `_resolve_environment_snapshot` in `reverie.py` and confirmed step validation (non-negative integer only), current-snapshot preference, prior-snapshot fallback without decrementing the step, and the clear failure when no valid snapshot exists.
4. Reviewed `ResumeEnvironmentFallbackTests` (five tests: missing current environment, corrupted current environment, all environments missing, all environments corrupted, and rejected non-integer step) and `ResumeAfterSigtermRegressionTests` (one subprocess test covering the real post-step SIGTERM resume from environment/0 to environment/1).
5. Counted `test_` methods and confirmed the current backend total of 67 and the Django total of 50, and confirmed the frontmatter `files_edited` list already includes `test_reverie_lifecycle.py`.
6. Grepped both edited files for em dashes and en dashes (none found) and confirmed the diff touches only the two documentation files.

## Update: 2026-08-01 durability and checkpoint hardening

### Summary of new work

This update consolidates the production durability hardening wave. The backend now publishes movement, environment, checkpoint, marker, and persona memory files through crash-safe atomic JSON writes; accepts environment snapshots only after shape, type, and bounds validation with a safe prior-snapshot fallback on resume; finishes the current step on SIGTERM or Ctrl+C before writing a checkpoint, with a 6-minute Compose stop grace period; stages fresh forks atomically and seeds the bundled simulations atomically and idempotently with validation and a completion marker; commits simulation state as versioned checkpoint generations behind an atomic pointer while retaining the previous generation and remaining compatible with the legacy layout; prunes uncommitted and future movement and environment artifacts at startup and live during service; reads and writes persona memory over UTF-8 with no `ResourceWarning`; fails fast on a missing provider key before any heartbeat; and is healthchecked through a freshness-marked heartbeat while the web service waits for the backend, the db, and the seed. The live browser post and move polling use in-flight guards and exponential backoff against the runtime-gated POST endpoints. The Django suite now holds 86 tests and the backend suite 148.

### Technical reasoning

- Atomic JSON publication: `durability.py` provides `atomic_write_json` (a same-directory temp file, flushed and fsynced, moved into place with `os.replace`, with best-effort directory fsync and temp cleanup on failure), `install_shutdown_handlers`, and `new_shutdown_event`. `serve_forever` publishes every movement file, the `curr_step.json` marker, and the heartbeat through this helper, and `ReverieServer.save` publishes the checkpoint pointer and legacy meta through it, so readers never observe partially written JSON.
- Cooperative shutdown: SIGTERM and SIGINT are routed to a `threading.Event` that the service loop checks only at safe points. A step in progress completes, then the loop publishes a final heartbeat and writes a checkpoint before exit. The Compose backend service declares `stop_grace_period: 6m` so this cooperative shutdown has time to finish. The `SigtermDuringStepTests` subprocess regression delivers a real SIGTERM mid-move and asserts the movement file, the checkpoint meta step, and a clean exit.
- Environment validation: `_validate_env_snapshot` accepts only a dict that includes every persona with non-bool integer `x`/`y` inside the maze bounds; malformed input is ignored and logged once rather than processed. `_resolve_environment_snapshot` validates the saved step as a non-negative integer, prefers `environment/{step}.json`, and otherwise walks the prior snapshots down to `environment/0.json` without decrementing the step, failing with a clear `FileNotFoundError` when none is valid. The same validation protects both the service loop and resume initialization.
- Validated publication and runtime gating: `process_environment` validates the JSON body and environment payload (non-empty object, per-persona records, non-bool non-negative integer coordinates, maze bounds when the maze is known, required personas) and persists through an atomic temp-file-plus-replace write. It accepts only the backend-published runtime step and simulation code from `temp_storage/curr_step.json` and `curr_sim_code.json`: missing or corrupt markers answer 503, a step or simulation mismatch answers 409, and neither writes a file so the browser can retry. `update_environment` answers 202 pending for absent, in-progress, corrupt, or non-object movement and 200 with data when ready.
- Browser coordination: the live simulator script uses `environmentPostInFlight` and `movementPollInFlight` guards with exponential backoff (`retryBaseDelay` 250 ms, doubling, retried indefinitely while the phase and step remain current, with `retryMaxDelay` capping the delay at 5000 ms and `retryAttemptLimit` capping the exponential attempt counter after five increases), advances phases only when the posted step matches the expected step, and treats movement as ready only when the payload matches the expected step and shape.
- Atomic fresh fork staging: `_fork_fresh` validates the origin, cleans any stale staging, copies to a deterministic `.staging` sibling, rewrites the staging meta atomically, and atomically renames staging into the final target. A target that appears during the fork raises `FileExistsError` and is never overwritten, and a partial target left by an interrupted fork is cleaned and redone on the next start.
- Atomic idempotent seed: `scripts/init_storage.sh` validates each seed (`reverie/meta.json`, a `personas` directory, `environment/0.json`, both as valid JSON objects, and at least one persona directory), stages a copy, validates the staged copy, writes a `.seed-complete` marker, and atomically renames it into the volume. Existing marked seeds are skipped, existing unmarked seeds are validated and marked, and an existing incomplete simulation aborts with a clear error instead of being overwritten.
- Checkpoint generations: `save` builds a complete generation (meta plus every persona's bootstrap memory) under `.staging`, fsyncs it, renames it to a zero-padded `reverie/checkpoints/<NNNNNNNNNN>` directory, and commits by atomically publishing `pointer.json` (version 1). Only after the pointer commit are old generations cleaned, retaining the previous one, and the legacy `reverie/meta.json` is updated atomically as compatibility metadata. `_load_checkpoint` fails closed on a corrupt pointer, a non-object pointer, a path-traversal or non-numeric generation name, an unsupported version, an incomplete generation, or an unsafe persona name. Simulations without a pointer keep the legacy layout unchanged, resume reads the pointed generation, and forking from a checkpointed origin syncs the legacy personas to the pointed state.
- Artifact pruning: `_prune_uncommitted_artifacts` removes movement files at or beyond the committed step and environment files strictly beyond it, using a numeric-only filename regex and never following or removing symlinks, so pruning cannot reach outside the simulation root. It runs on every initialization before the frontend is signaled and after every processed step while the service is live, alongside the `curr_step.json` publication that a restart always overwrites from the committed checkpoint.
- Memory I/O: `scratch.py`, `spatial_memory.py`, and `associative_memory.py` load through context-managed UTF-8 handles and save through `atomic_write_json`; the associative group creates its output directory when missing. `test_memory_durability.py` asserts no `ResourceWarning` on the spatial, associative, and scratch load paths and that an interrupted write preserves prior content.
- Provider fail-fast and heartbeat: `_validate_provider_keys` resolves the effective provider for the global default and every loaded persona and requires the matching key before `serve_forever` publishes any heartbeat; unknown providers fail closed and error messages never expose key values. `serve_forever` publishes `backend_heartbeat.json` at startup and at a throttled interval, and the Compose backend healthcheck treats the file as fresh only when its timestamp is a non-bool number and newer than 15 seconds. The web service depends on the backend being healthy, the db being healthy, and the seed having completed.
- Compose hardening: the web and backend services run with `read_only: true` root filesystems and a writable `/tmp` tmpfs, the db service runs as uid/gid 70 (postgres) with an authenticated `SELECT 1` healthcheck, each service receives only the environment it consumes, and `DJANGO_CSRF_TRUSTED_ORIGINS` and `DJANGO_SECURE_SSL_REDIRECT` are required with no default so a misconfigured `.env` fails fast.
- Offline, no-live-claim contract: the expanded backend suites replace `Maze` and `Persona` with in-memory fakes and confine file I/O to temporary directories, and the subprocess lifecycle tests run the real save path offline. The provider and lifecycle guarantees are asserted with mocked and subprocess behavior only, and no claim is made of a successfully executed live LLM or browser step with real credentials. The Docker import smoke checks likewise perform no live LLM call.
- Fork prerequisite: a custom fork origin must already exist in the storage volume, which the seed provisions only with `base_the_ville_isabella_maria_klaus` and `base_the_ville_n25`. Any previously forked simulation is reusable, and an arbitrary code that is not in the volume fails in the backend with a clear error before anything is written.

### Impact assessment

The current suites total 86 Django tests (83 in `translator/tests.py` and 3 in `translator/test_simulator_scripts.py`) and 148 backend tests (12 embedding privacy, 26 config hardening, 21 lifecycle, 8 scratch serialization, 33 durability, 28 checkpoints, 10 artifact pruning, 10 memory durability). The earlier sections of this record that report 47 or 50 Django tests and 52, 59, 61, or 67 backend tests describe the suites as they stood at those points in time and are preserved for chronology; the counts in this update and in the README describe the current state.

Operationally the stack now survives an interrupted deploy or an in-flight shutdown: a partial fork is never accepted, a crash during a checkpoint cannot produce a mixed generation, a restart prunes stale future artifacts and re-publishes the committed step, and a mid-step signal still produces a coherent checkpoint. The documented SQLite guidance remains history-only: the old credential hashes live only in public Git history, and the local key rotation advisory is retained. The app and db images were scanner-clean at the prior exact and near-exact validations; this update records those measurements and makes no claim of a passing final external quality and security gate, which remains a separate release step.

### Validation steps

1. Ran the Django suite with `uv run python manage.py test` from `environment/frontend_server` and confirmed 86 tests pass.
2. Ran the backend suite with `uv run python -m unittest discover -s reverie/backend_server -p "test_*.py"` and confirmed 148 tests pass (12 embedding privacy, 26 config hardening, 21 lifecycle, 8 scratch serialization, 33 durability, 28 checkpoints, 10 artifact pruning, 10 memory durability).
3. Inspected `durability.py`, `reverie.py`, the three memory-structure modules, `translator/views.py`, `templates/home/main_script.html`, `docker-compose.prod.yml`, `Dockerfile`, `Dockerfile.db`, `scripts/init_storage.sh`, and `.env.example`, and verified every statement in this update against the current code.
4. Counted `test_` methods per backend file and confirmed the frontmatter `files_edited` list now includes `durability.py`, the new test files (`test_artifact_pruning.py`, `test_checkpoints.py`, `test_durability.py`, `test_memory_durability.py`, and `translator/test_simulator_scripts.py`), and the changed memory-structure files `associative_memory.py`, `scratch.py`, and `spatial_memory.py`.
5. Confirmed the README documents the same current state, including the custom fork prerequisite, the runtime-gated POST behavior with 409/503 retries, the browser in-flight guards and backoff, and the no-live-claim and no-final-gate statements.
6. Grepped both edited files for em dashes and en dashes (none found), verified sentence case and heading progression with a single H1, and confirmed the diff touches only the two documentation files.

## Update: 2026-08-01 consolidated current-state documentation

### Summary of new work

This documentation-only update corrects every current-state summary in this record and the README and adds the runtime authority work completed since the durability update. The current suites total 131 Django tests and 207 backend tests. The backend and frontend now coordinate through two persistent lock files in `temp_storage`: `runtime.lock`, a shared runtime authority lock that serializes Django environment and movement operations with backend marker clear, publish, and per-step updates, and `backend_alive.lock`, a liveness lease held by both service and interactive modes after prompt resolution and before any marker mutation, probed by Django before and after operations, released by the kernel on SIGKILL, and acquired lease-first so a second backend can never touch a live owner's markers. Service and interactive markers publish only after init and provider validation and are cleared before lease release. Interactive `run N` now uses strict env validation, atomic movement, per-step marker update, prune, and heartbeat, and dispatches to the instance's own step method. Environment files are immutable and idempotent behind a per-step flock, the browser enters a server-derived process or update phase with retry and one-shot resync behavior, maze bounds are authoritative, and simulation, generation, and persona names are strictly validated. The Docker loopback bind, heartbeat identity checks, read-only root filesystems, authenticated db healthcheck, and atomic seed and checkpoints remain. No live real-credential OpenRouter or browser step was run; tests, mocks, and the no-network stack delimit this. The historical SQLite hash advisory and the ignored local key rotation advisory remain documented, and no final external quality and security gate is claimed.

### Technical reasoning

- Shared runtime lock: `runtime.lock` is a persistent, never-unlinked file opened with `O_CREAT | O_RDWR` plus `O_NOFOLLOW` and locked with an exclusive `fcntl.flock` in both `durability.py` and `translator/views.py`. The backend holds it for marker clear, `curr_step.json` publish, and per-step authority mutations; Django holds it from before the first marker read through validation, the per-step lock, the immutable comparison, the atomic publication, and the response decision. A holder therefore sees a stable runtime view, and a restart clear or prune can never interleave with an in-flight request. Thread and cross-process tests assert the mutual exclusion in both directions (a request holder blocks marker clear, and a marker holder blocks the request until markers settle).
- Liveness lease: `backend_alive.lock` is acquired with a nonblocking exclusive flock at the start of `_run_serve` and `_run_interactive`, before any marker clear or publish. Interactive mode resolves its CLI prompts first; service mode has no prompts. Django probes the lease at the start of `home`, `process_environment`, and `update_environment`, again after an environment write, and again after a movement read, answering 503 or the startup error when no backend holds it. Because the kernel releases the flock when the holding process dies, a SIGKILL frees the lease without cleanup, and a replacement backend acquires it and clears the dead owner's stale markers itself. Tests cover a subprocess SIGKILL auto-release, a second owner being refused, a second backend leaving live owner markers unchanged, and a replacement republishing after owner death.
- Marker lifecycle: `_run_serve` and `_run_interactive` clear stale markers, construct the server, validate provider keys, publish the sim and step markers as one critical section under the runtime lock, and then enter the loop or the interactive prompt. Every exit path inside the lease clears the markers again before the lease is released, so a failed init, missing code, provider failure, or exception leaves no stale markers behind. Tests assert markers are absent after graceful shutdown, unexpected exceptions, provider failure, missing codes, and constructor failure, and that interactive mode publishes before `open_server`.
- Interactive `run N`: `open_server` dispatches the run command to `self.start_server` rather than the module-level global `rs` (regression-tested). `start_server` mirrors the service loop: strict `_validate_env_snapshot` with authoritative maze bounds, atomic movement publication, step advance with `_publish_curr_step` under the runtime lock, `_prune_uncommitted_artifacts`, and `_publish_heartbeat`. Invalid or malformed input is ignored with a single warning and never advances step or marker, and a movement write failure leaves step and markers untouched.
- Immutable environment files: `process_environment` writes each step's environment under a per-step `environment_locks/{step}.lock` flock. An already published step is immutable: an identical payload answers 200 without rewriting, a differing or corrupt file answers 409 and is preserved, and no prior or concurrent environment is ever deleted. Concurrent-writer tests assert exactly one winner.
- Browser phases and resync: `home` and `replay` derive `initial_phase` from whether the environment for the expected step is already published, so a reload resumes in the `process` or `update` phase instead of guessing. The live script uses in-flight guards and exponential backoff for both the environment post and the movement poll, advances from `process` to `update` only when the posted step still matches, treats movement as ready only when the payload matches the expected step and shape, and reloads once (guarded by `resyncInProgress`) when a 409 movement mismatch returns a resync payload carrying the expected step and sim code. Five simulator script tests assert the parse, the server recovery context, the handoff retries, the one-shot resync, and the poll-without-duplicate-execution behavior.
- Strict bounds and names: `_validate_environment` applies authoritative maze bounds from the simulation's checkpoint or legacy meta when known, so an unknown record maze can never bypass the simulation bounds. `_validate_sim_code` restricts sim codes to the Django-safe word/dot/hyphen pattern and rejects traversal, slashes, and control characters; `_safe_persona_name` rejects path components and control characters; checkpoint generation names must be numeric. Tests cover each rejection and acceptance path in both suites.
- Unchanged, verified posture: the compose web port binds to 127.0.0.1 by default (`WEB_BIND_ADDRESS`), the backend healthcheck verifies the heartbeat's numeric timestamp freshness plus the expected sim code and container PID, web and backend run with read-only root filesystems and writable `/tmp`, the db answers an authenticated `SELECT 1`, and seed and checkpoints are atomic and idempotent. The no-live-claim contract is unchanged: all backend tests use in-memory `Maze` and `Persona` fakes and temporary directories, the subprocess lifecycle tests run the real save path offline, and neither suite performs a live LLM call, a real browser step, or any network request.

### Impact assessment

The current suites total 131 Django tests (126 in `translator/tests.py` and 5 in `translator/test_simulator_scripts.py`) and 207 backend tests (12 embedding privacy, 26 config hardening, 34 lifecycle, 8 scratch serialization, 78 durability, 29 checkpoints, 10 artifact pruning, 10 memory durability). The counts reported in the preceding updates (47, 50, or 86 Django and 52, 59, 61, 67, or 148 backend) describe the suites as they stood at those points in time and are preserved for chronology; the counts in this update and in the README describe the current state.

Operationally, exactly one backend can hold the liveness lease at a time in either mode, a replacement backend takes over after a crash including SIGKILL, and the frontend never serves or accepts runtime state from a backend that is not alive or whose markers are mid-transition. Environment files are immutable once published, so a browser retry can never corrupt a committed step, and the interactive run loop carries the same validation, atomicity, and marker discipline as service mode. The README carries the same current statements: 131 and 207 tests, the runtime lock and liveness lease, immutable per-step environment files, the browser process and update phases with retry and resync, authoritative bounds and safe names, the loopback bind and heartbeat identity healthcheck, the unchanged no-live-claim contract, the historical SQLite hash advisory and ignored local key rotation advisories, and the explicit statement that no final external quality and security gate has been claimed; that gate remains a separate release step.

### Validation steps

1. Ran the Django suite with `uv run python manage.py test` from `environment/frontend_server` and confirmed 131 tests pass (126 in `translator/tests.py`, 5 in `translator/test_simulator_scripts.py`).
2. Ran the backend suite with `uv run python -m unittest discover -s reverie/backend_server -p "test_*.py"` and confirmed 207 tests pass (12 embedding privacy, 26 config hardening, 34 lifecycle, 8 scratch serialization, 78 durability, 29 checkpoints, 10 artifact pruning, 10 memory durability).
3. Inspected `durability.py` (runtime lock and liveness lease helpers), `reverie.py` (lease acquisition in `_run_serve` and `_run_interactive`, marker clear and publish ordering, the `start_server` and `serve_forever` step loops), `translator/views.py` (runtime lock, lease probes, per-step flock, immutable environment write, initial-phase derivation), and `templates/home/main_script.html` (in-flight guards, backoff, resync reload) and verified every statement in this update against the current code.
4. Counted `test_` methods per backend file and per Django test module and confirmed the frontmatter `files_edited` list includes `durability.py` and every test file (`test_artifact_pruning.py`, `test_checkpoints.py`, `test_durability.py`, `test_embedding_privacy.py`, `test_llm_config_hardening.py`, `test_memory_durability.py`, `test_reverie_lifecycle.py`, `test_scratch_serialization.py`, `reverie/backend_server/test.py`, `translator/tests.py`, and `translator/test_simulator_scripts.py`).
5. Confirmed the README documents the same current state, including the runtime lock and liveness lease, the marker lifecycle, the interactive run loop, immutable environment files, the browser phases with retry and resync, the loopback bind and heartbeat identity healthcheck, the no-live-claim contract, the historical SQLite and key rotation advisories, and the no-final-gate statement.
6. Grepped both edited files for em dashes and en dashes (none found), verified sentence case and heading progression with a single H1, and confirmed the diff touches only the two documentation files.

## Update: 2026-08-01 LLM provider retry policy and exact container verification

### Summary of new work

The LLM client layer now runs every chat completion and embedding request through one bounded exponential retry policy owned by the application. `LLM_API_RETRIES` defaults to 3 and counts retries after the initial call, the backoff base `LLM_API_RETRY_BASE_SECONDS` defaults to 1 second and doubles per retry, and `LLM_API_TIMEOUT_SECONDS` defaults to 60 seconds. The OpenAI-compatible SDK clients are built with `max_retries=0`, so no second retry layer exists. Malformed, out-of-range, and non-finite values for all three variables fail fast at import with a sanitized error naming only the variable and its range. Exhaustion raises a typed `LLMProviderError` shared across modules whose message names only the provider, the operation, and the attempt count; provider failures never become validation sentinels, never reach cleanup or persona memory, and are never persisted as persona state. Service and interactive runners catch the typed error, print the sanitized message, clear the runtime markers, release the backend lease, and exit with status 1 without a traceback, leaving already-published movement, step, and checkpoint state on disk so the Compose stack may restart from the committed checkpoint. The current suites total 244 backend tests and 131 Django tests. Exact container verification runs are recorded: a no-network resume stayed healthy, an invalid non-placeholder key with `LLM_API_RETRIES=1` failed after exactly two attempts sanitized and uncommitted, and a placeholder key failed before any runtime marker was published. The current app and db images report zero Trivy vulnerabilities, secrets, and misconfigurations, and the filesystem scan reports zero vulnerabilities for `uv.lock` with only the generic image-level health advisory, which the role-specific Compose healthchecks offset. No live valid-provider success or browser end-to-end run is claimed, and the final external quality and security gate remains unclaimed.

### Technical reasoning

- Retry configuration: `utils.py` parses `LLM_API_RETRIES` as an integer at least 1 (default 3), `LLM_API_RETRY_BASE_SECONDS` as a finite float at least 0 (default 1.0), and `LLM_API_TIMEOUT_SECONDS` as a finite float greater than 0 (default 60.0). Non-finite values such as `inf`, `-inf`, and `nan` are rejected with a message that never echoes the raw value, and any malformed value aborts the `utils` import, so a container with a bad override fails fast instead of running with unintended retry behavior.
- Single retry owner: `gpt_structure._get_client` constructs both the OpenRouter and OpenAI clients with `timeout=llm_api_timeout_seconds` and `max_retries=0`. All chat wrappers (`ChatGPT_single_request`, `ChatGPT_request`, `GPT4_request`, `GPT_request`) and `get_embedding` call the same `_call_with_retries` helper, which sleeps `llm_api_retry_base_seconds * (2 ** attempt)` between attempts and re-raises `KeyboardInterrupt` and `SystemExit` immediately without retry.
- Sanitized typed error: after exhaustion `_call_with_retries` raises `LLMProviderError(provider, operation, attempts)`, a single type defined in `utils.py` and re-exported by `gpt_structure.py` and `reverie.py`. The message contains only the provider name, the operation name, and the total attempt count; the causing exception, prompts, API keys, and response bodies are never included. The safe response wrappers re-raise `LLMProviderError` before any validator or cleanup runs, so a provider failure can never be validated as model output, become a sentinel, reach persona memory, or persist into scratch state.
- Controlled exhaustion exit: `_run_serve` catches `LLMProviderError` from `serve_forever` and `_run_interactive` catches it from `open_server`, each printing the sanitized message to stderr and exiting via `sys.exit(1)` without a traceback. The `finally` block clears the runtime markers while the liveness lease is still held, and the lease context releases the flock on exit. A failure mid-step publishes no movement file for that step, advances no step, and triggers no save, while movement and step files already published and the last committed checkpoint generation remain intact, so a Compose restart resumes from the committed checkpoint.
- Current test breakdown: the new `test_llm_provider_retries.py` adds 33 tests (chat and embedding fail-fail-success with backoff schedule, sanitized exhaustion messages, immediate `KeyboardInterrupt`/`SystemExit` propagation, safe wrappers never validating provider output, parse and validator and cleanup retries staying model-output retries, client timeout and zero SDK retries, the shared single error type, and the config contract with defaults 3 / 1.0 / 60.0 and import-time rejection of malformed and non-finite values). `test_durability.py` grew from 78 to 82 with the four `ProviderFailureLifecycleTests` that assert the service and interactive status-1 sanitized exits, no movement publication, no save, markers cleared, lease released, and the committed checkpoint untouched byte for byte.
- Exact container verification: three runs were recorded against the real Compose stack. A no-network resume became healthy: with no external connectivity the backend resumed from a committed checkpoint and its heartbeat healthcheck passed, because provider calls fail only when a step needs one and a failure exits controlled with the checkpoint intact. With an invalid non-placeholder API key and `LLM_API_RETRIES=1`, the backend failed after exactly two attempts with a sanitized message naming only the provider, exited status 1, and committed no checkpoint. With a placeholder API key, provider validation failed before any runtime marker was published.
- Scanner evidence: the current app image and the derived db image each report zero Trivy vulnerabilities, secrets, and misconfigurations, and `trivy fs` reports zero vulnerabilities for `uv.lock`; the only filesystem note remains the generic low-severity image-level healthcheck advisory, offset by the role-specific Compose healthchecks for the web, backend, and db services.

### Impact assessment

The current suites total 244 backend tests (12 embedding privacy, 26 config hardening, 34 lifecycle, 33 LLM provider retries, 8 scratch serialization, 82 durability, 29 checkpoints, 10 artifact pruning, 10 memory durability) and 131 Django tests (126 in `translator/tests.py` and 5 in `translator/test_simulator_scripts.py`). The counts recorded in the preceding updates (52, 59, 61, 67, 148, or 207 backend and 47, 50, 86, or 131 Django) describe the suites as they stood at those points in time and are preserved for chronology; the counts in this update and in the README describe the current state.

Operationally, a provider outage no longer cascades: one bounded policy controls retries, exhaustion surfaces as a sanitized typed error, and both runners exit controlled with markers cleared and the lease released so the Compose stack restarts from the last committed checkpoint. The no-live-claim contract is unchanged: no real valid-provider success and no browser end-to-end run is claimed, and the final external quality and security gate for the branch remains a separate release step, not pre-claimed here. The historical SQLite hash advisory and the ignored local key rotation advisory remain documented.

### Validation steps

1. Ran the backend suite with `uv run python -m unittest discover -s reverie/backend_server -p "test_*.py"` and confirmed 244 tests pass (12 embedding privacy, 26 config hardening, 34 lifecycle, 33 LLM provider retries, 8 scratch serialization, 82 durability, 29 checkpoints, 10 artifact pruning, 10 memory durability).
2. Ran the Django suite with `uv run python manage.py test` from `environment/frontend_server` and confirmed 131 tests pass (126 in `translator/tests.py`, 5 in `translator/test_simulator_scripts.py`).
3. Reviewed `utils.py` retry and timeout parsers with their defaults and finite/range checks, `gpt_structure._call_with_retries` and client construction with `max_retries=0`, the `LLMProviderError` message sanitization, the safe wrapper re-raise paths, and the `_run_serve` and `_run_interactive` catches that exit status 1 without a traceback while the `finally` clears markers and the lease context releases the flock.
4. Reviewed `ProviderFailureLifecycleTests` in `test_durability.py` and `test_llm_provider_retries.py`, counted `test_` methods per backend file and per Django module, and confirmed the frontmatter `files_edited` list includes `test_llm_provider_retries.py`.
5. Recorded the exact container verification runs: no-network resume healthy from a committed checkpoint; invalid non-placeholder key with `LLM_API_RETRIES=1` failing after two attempts sanitized with no checkpoint commit; placeholder key failing before any runtime marker was published; and the current app and db image Trivy results of zero vulnerabilities, secrets, and misconfigurations with `uv.lock` at zero and only the delegated image-level healthcheck note.
6. Grepped both edited files for em dashes and en dashes (none found), verified sentence case and heading progression with a single H1, and confirmed the diff touches only the two documentation files.
