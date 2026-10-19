# Generative Agents: Interactive Simulacra of Human Behavior

<p align="center" width="100%">
<img src="cover.png" alt="Smallville" style="width: 80%; min-width: 300px; display: block; margin: auto;">
</p>

This repository accompanies our research paper titled "[Generative Agents: Interactive Simulacra of Human Behavior](https://arxiv.org/abs/2304.03442)." It contains our core simulation module for generative agents (computational agents that simulate believable human behaviors) and their game environment. Below, we document the steps for setting up the simulation environment on your local machine and for replaying the simulation as a demo animation.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Isabella_Rodriguez.png" alt="Generative Isabella">   Setting up the environment

This project uses [uv](https://docs.astral.sh/uv/) for dependency management and requires **Python 3.12+**.

### Step 1. Install dependencies

```bash
uv sync
```

This installs all packages declared in `pyproject.toml` (with locked versions from `uv.lock`) into a virtual environment managed by uv. `pyproject.toml` and `uv.lock` are the only authoritative dependency manifests.

### Step 2. Configure environment variables

```bash
cp .env.example .env
```

Then edit `.env` with your API keys and preferences. The configuration file (`reverie/backend_server/utils.py`) loads all settings from `.env` at runtime, so no code changes are needed.

The example file ships with a placeholder `DJANGO_SECRET_KEY` that is public knowledge. If your `.env` was copied from it, generate a fresh key and replace the placeholder before using the app against anything shared:

```bash
uv run python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
```

See the next section for provider and model options.

## Environment selection (DJANGO_ENV)

`DJANGO_ENV` selects the Django settings module and accepts exactly two values: `dev` (the default, for local development) and `production`. Any other value aborts startup with a clear error instead of silently falling back, so a typo can never boot the wrong settings.

The dev settings keep local conveniences: `ALLOWED_HOSTS=["*"]`, DEBUG may be enabled, and the database defaults to a gitignored local SQLite file whose schema the operator must create or update by running `uv run python manage.py migrate` before first use, as described under [Operator authentication](#operator-authentication).

The production settings are fail-closed. Startup is refused unless every requirement below holds:

- `DJANGO_SECRET_KEY` is a cryptographically strong, unique value of at least 50 characters that is not a known placeholder or common weak term, contains no repeated character or pattern, and has an estimated Shannon entropy of at least 200 bits
- `DJANGO_DEBUG` is not enabled
- an external database is configured either through `DATABASE_URL` or through the `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT` fields; SQLite is rejected outright

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Klaus_Mueller.png" alt="Generative Klaus">   LLM provider configuration

Smallville supports **OpenRouter**, **OpenAI**, and the experimental **Hetzner Inference API**.

### Provider selection

Set `LLM_PROVIDER` in your `.env`:

```env
LLM_PROVIDER=openrouter    # or "openai" or "hetzner"
```

When `LLM_PROVIDER` is unset, the code falls back to `openai`.

### API keys

- **OpenRouter**: set `OPENROUTER_API_KEY`
- **OpenAI**: set `OPENAI_API_KEY`
- **Hetzner**: set `HETZNER_API_KEY` securely in the backend environment

You need keys for each explicitly selected chat and embedding provider. Keys are backend-only; never put them in browser code or commit them.

### Default model

Set `LLM_MODEL` to override the provider-specific default:

| Provider | Default model |
|----------|---------------|
| OpenAI | `gpt-4o` |
| OpenRouter | `openai/gpt-4o` |
| Hetzner | `Qwen3.8-27B` |

Any model slug your provider supports is valid (e.g., `anthropic/claude-3.5-sonnet` on OpenRouter).

### Hetzner experimental inference

The [official Hetzner documentation](https://docs.hetzner.com/general/company-and-policy/experiments/inference/) lists the exact model ID `Qwen3.8-27B` and base URL `https://inference.hetzner.com/api/v1`. The app uses Bearer authentication through the OpenAI SDK. On backend startup it queries `/models` through the same request gate and refuses an unavailable configured model; it never substitutes another model. The catalog response is authoritative because experimental model availability can change.

```env
LLM_PROVIDER=hetzner
LLM_MODEL=Qwen3.8-27B
HETZNER_REQUESTS_PER_MINUTE=10
LLM_REASONING_EFFORT=
# Supply HETZNER_API_KEY securely in the backend environment.
# OpenRouter embedding route selected for this profile:
LLM_EMBEDDING_PROVIDER=openrouter
LLM_EMBEDDING_MODEL=google/gemini-embedding-2
LLM_EMBEDDING_DIMENSIONS=3072
```

The application serializes **all Hetzner attempts**, including retries and catalog probes, by API-key fingerprint. At 10 RPM it waits at least **6.1 seconds after each response** before dispatching another request, so idle time never accumulates burst permits. Lower limits from 1–10 RPM are configurable; larger values fail closed. A persistent `flock` gate in `storage/.provider_limits` coordinates threads, processes, restarts and containers sharing that directory. Compose puts it on the existing persistent storage volume. Set `HETZNER_RATE_LIMIT_DIR` to the same absolute persistent path for every worker using the same key, with filesystem support for cross-worker `flock`; do not delete active gate files. Separate machines with separate directories and unrelated clients sharing the API key cannot share this budget. Use one gate directory for them or a dedicated key. This is a conservative shared queue with concurrency one, so slow responses further reduce throughput.

HTTP 429 and transient failures retry through the same gate. Numeric and HTTP-date `Retry-After` values are respected and persisted for other workers, including on final retry exhaustion; missing or malformed values use capped exponential backoff with small jitter. A 429 without a usable `Retry-After` also blocks the shared key for 60 seconds. Authentication and other permanent HTTP 4xx errors stop after one attempt. The SDK has no independent retries. Explicit reasoning effort is refused for Hetzner because its support is not established; leave it blank. Provider failures are sanitized and there is no live-provider fallback.

Hetzner currently documents 10 requests, 4M input tokens and 100k output tokens per 60 seconds per key. The app gate enforces the request ceiling; token ceilings remain provider-enforced. The service is free while experimental, has no availability guarantee, and is unsuitable for production. Configure the backend with its provider key before starting a simulation.

### OpenRouter attribution

When using OpenRouter, the following `.env` variables are sent as attribution headers:

```env
OPENROUTER_REFERER=https://github.com/digitalygo/smallville
OPENROUTER_TITLE=smallville
OPENROUTER_CATEGORIES=game,roleplay
```

### Per-persona model assignment

You can assign different models to individual personas. Two mechanisms exist, applied in priority order:

1. **scratch.json fields**: add optional `"llm_provider"` and `"llm_model"` fields to a persona's bootstrap `scratch.json` file.
2. **`LLM_PERSONA_MODELS` in `.env`**: a JSON map keyed by persona name.

Example using `LLM_PERSONA_MODELS`:

```env
LLM_PERSONA_MODELS='{"Isabella Rodriguez":"openrouter:anthropic/claude-3.5-sonnet","Maria Lopez":"openai:gpt-4o"}'
```

Prefix a value with `openrouter:`, `openai:` or `hetzner:` to set both provider and model. A bare model name (e.g., `"gpt-4o"`) uses the current provider. If no override is set for a persona, the global defaults above apply.

### Embedding models

Embeddings use a separate model from chat completions. `LLM_MODEL` is never used for embedding requests. Set `LLM_EMBEDDING_PROVIDER=openai` or `openrouter` to choose an explicit embedding route. When unset, embeddings follow the chat provider for compatibility. Hetzner advertises no embeddings endpoint: selecting it for chat requires an explicit embedding provider and that provider’s key. Startup fails before publishing runtime markers if this is missing; no paid provider is selected automatically. Keep the same embedding model for an existing memory index, since changing its dimensions or embedding space makes saved vectors incompatible. The approved profile in `.env.example` pairs Hetzner `Qwen3.8-27B` chat with OpenRouter `google/gemini-embedding-2` embeddings at 3072 dimensions. It does not switch an existing deployment. The model settings are:

- `LLM_EMBEDDING_MODEL`: a global embedding model name (optional; a provider-appropriate default is used if unset).
- `LLM_EMBEDDING_MODELS`: a per-provider JSON map, keyed by provider name.
- `LLM_EMBEDDING_DIMENSIONS`: an optional positive integer sent as `dimensions`; Gemini Embedding 2 defaults to 3072 when unset and accepts 128–3072. Changing it requires a compatible empty or separately re-embedded index.

| Provider | Default embedding model |
|----------|------------------------|
| OpenAI | `text-embedding-ada-002` |
| OpenRouter | `openai/text-embedding-ada-002` |

Example:

```env
LLM_EMBEDDING_MODEL=text-embedding-3-small
LLM_EMBEDDING_MODELS='{"openai":"text-embedding-3-small","openrouter":"openai/text-embedding-3-small"}'
```

Resolution precedence: an explicit per-request model, then `LLM_EMBEDDING_MODELS` for the active provider, then `LLM_EMBEDDING_MODEL`, then the provider default.

`LLM_PERSONA_MODELS` and `LLM_EMBEDDING_MODELS` must be valid JSON objects mapping strings to strings. Malformed JSON aborts startup with a clear error instead of silently falling back to defaults.

The current operator configuration uses `google/gemini-embedding-2` through OpenRouter (`LLM_EMBEDDING_MODEL=google/gemini-embedding-2`). OpenRouter embedding requests always ask for `encoding_format=float`, and a live probe of `google/gemini-embedding-2` returned a 3072-dimension float vector. Embeddings are stored only in persona associative-memory JSON (`associative_memory/embeddings.json`); the web database schema stores no embeddings, so PostgreSQL never holds vectors. The bundled 3-person base seed ships empty `embeddings.json` files, so no preexisting vectors and no mixed embedding index exist for it. Only the `EnvaiSimulation` target was reset during verification; the PostgreSQL data, the admin superuser, and the two bundled base seeds were preserved.

### Gemini Embedding 2 and saved memory compatibility

The [OpenRouter model page](https://openrouter.ai/google/gemini-embedding-2) lists the exact ID `google/gemini-embedding-2`, 8192 input tokens, and 128–3072 output dimensions. Smallville calls the [OpenRouter embeddings API](https://openrouter.ai/docs/api/api-reference/embeddings/submit-an-embedding-request) at `https://openrouter.ai/api/v1/embeddings`, using `encoding_format=float` and an explicit 3072-dimension request in the approved profile. It sends no Gemini `task_type` parameter. [Google’s migration documentation](https://ai.google.dev/gemini-api/docs/embeddings#migration-from-gemini-embedding-001) confirms that Gemini Embedding 001 and 2 use incompatible spaces; equal vector length does not make different models compatible.

New persona checkpoints save `associative_memory/embedding_space.json` alongside the unchanged vector-map format. Version 1 records provider, exact model and dimensions. Startup checks all saved vectors against that provenance before a catalog or embedding request; requests, retrieval queries and new memory inserts retain the same route. Model/provider changes, wrong or mixed dimensions, non-finite/zero vectors and malformed metadata fail closed. API responses are validated within bounded retries. Vectors are never padded, truncated, cleared or automatically re-embedded. A save cannot inherit a different destination’s provenance. Normal checkpoint commit/pointer semantics include the new sidecar, and fork/resume preserve it.

Both bundled seeds have empty embedding caches and can start with the approved profile. A **nonempty legacy cache without metadata is refused**, even if every vector has 3072 values. Preserve the existing simulation. If its original model differs or cannot be verified, start a separate target from an empty bundled seed, or explicitly re-embed its texts into a separate simulation outside the automatic startup path. This change performs neither data deletion nor a paid bulk migration.

Only when the original provider, model and dimensions of **every untagged cache being loaded** are known and match the active route, the operator may explicitly attest them:

```env
LLM_LEGACY_EMBEDDING_SPACE='{"provider":"openrouter","model":"google/gemini-embedding-2","dimensions":3072}'
```

This is a provenance declaration, not a converter. A wrong model or dimension still fails. It tags the next normally committed checkpoint without changing existing vector values or deleting prior history; source files are not rewritten merely by loading or probing. Leave this setting unset for fresh seeds and tagged checkpoints. Never infer it from vector length alone.

To check the configured embedding API with a synthetic input, supply `OPENROUTER_API_KEY` securely in the environment and run:

```bash
uv run python scripts/check_embeddings.py
```

### Retry and timeout policy

Every chat completion and embedding request runs through a single bounded exponential retry policy owned by the application. The OpenAI SDK's own retries are disabled (`max_retries=0`), so no second retry layer can multiply attempts. Three `.env` variables tune the policy:

- `LLM_API_RETRIES`: retries after the initial call, integer at least 1, default 3
- `LLM_API_RETRY_BASE_SECONDS`: base delay for exponential backoff, finite number at least 0, default 1
- `LLM_API_TIMEOUT_SECONDS`: per-request timeout, finite number greater than 0, default 60

The backoff doubles per retry starting from the base delay. Malformed, out-of-range, or non-finite values (including `inf`, `-inf`, and `nan`) abort startup with a clear error that names only the variable and its valid range, so a typo can never silently change retry behavior. When every attempt fails, the call raises a typed `LLMProviderError` whose message names only the provider, the operation, and the attempt count; prompts, API keys, and response bodies are never included. Provider failures are never treated as model output: they cannot become validation sentinels, reach persona memory or scratch state, or be persisted as persona state.

On provider exhaustion during a run, both service and interactive modes exit controlled with status 1 and no traceback. The runtime markers are cleared and the backend lease is released, so the Compose stack may restart the backend from the last committed checkpoint; movement, step, and checkpoint state that was already published before the failing call remains on disk.

### Reasoning effort (optional)

`LLM_REASONING_EFFORT` optionally sets the reasoning effort sent with every chat request. When it is blank or unset, the app sends no reasoning parameter at all and the provider or model default applies. The allowed values are `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`; any other value aborts startup with a sanitized error that names the variable and the allowed values, never the raw input. Support is model-specific: the configured model must accept the requested effort or the provider API rejects the request, and reasoning tokens are still billed by the provider. OpenAI and OpenRouter differ at the request level: OpenRouter receives the nested `extra_body` reasoning block (`{"reasoning": {"effort": "low"}}`), while OpenAI receives the top-level `reasoning_effort` parameter. The recommended sample is a blank value, which keeps the provider default:

```env
LLM_REASONING_EFFORT=
```

### Request limits and output controls

Smallville imposes no chat input token, character, or byte cap of its own and sends each assembled prompt whole; semantic retrieval still selects memories for relevance, but no application-level truncation trims a prompt before it is sent. The chat wrappers also send no `max_tokens`, no `max_completion_tokens`, and no `stop` sequence, so the provider or model context and output ceilings and their billing apply as configured on the provider side. The bounded retry and timeout policy above applies to every chat and embedding call.

### Model output hardening

Model output is hardened before it becomes persona state. A chat response whose content is missing, `None`, non-string, or whitespace-only is treated as malformed and retried within the bounded policy, so validators and cleanup only ever see validated nonempty string content. Model-generated spatial choices are grounded to the persona's actual memory tree: generated sectors, arenas, and game objects are resolved through canonical matching against the accessible lists from `spatial_memory`, nothing is ever invented, and an unrecognized game object falls back to the `<random>` placeholder while an invalid sector or arena falls back to a real accessible location.

### Current operator configuration

The current operator configuration uses OpenRouter with model `deepseek/deepseek-v4-flash-0731` and no fallback: `LLM_PROVIDER=openrouter`, `LLM_MODEL=deepseek/deepseek-v4-flash-0731`, and neither an `LLM_PERSONA_MODELS` map nor a persona scratch override is set. The historical `openai/gpt-4o-mini` single-request probe recorded in the testing section was a prior one-off verification and is not the current routing.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Klaus_Mueller.png" alt="Generative Klaus">   Operator authentication

Simulation control pages (`/simulator_home`, `/path_tester/`) and POST endpoints require authentication. On a fresh local checkout, run the database migration first, then create a Django superuser account:

```bash
cd environment/frontend_server
uv run python manage.py migrate
uv run python manage.py createsuperuser
```

`migrate` creates or updates the gitignored local SQLite schema (`db.sqlite3`) and is safe and required before the account exists: `createsuperuser` writes the account into that schema, and first access to the app alone does not create the tables. Run these once before the first `runserver` (see [Starting the environment server](#step-1-starting-the-environment-server)).

Then log in at `/accounts/login/` before accessing simulation controls. This is enforced in all environments (dev and production).

Login POSTs are protected against brute force by a database-backed throttle shared by every Gunicorn worker. `POST /accounts/login/` and `/admin/login/` are gated before the view runs, so password hashing never happens for throttled attempts. Bucket keys are HMAC-SHA256 digests of opaque values, never raw IP addresses or usernames, and state lives in the `LoginThrottleBucket` table created by migration 0006. Two bucket scopes exist: a source bucket per client and a pair bucket per client and normalized account, so there is no account-wide lockout and a victim account can never be locked out from another client. Once a bucket passes its threshold, further attempts answer 429 with a generic body and a `Retry-After` header whose delay doubles per extra failure up to the configured cap. A successful login resets only the pair bucket for that client and account; source buckets keep counting. Expired windows reset bucket counts and stale rows are swept in bounded batches. GET requests and the health endpoint are never gated. An already authenticated session posting to either login path is answered a generic 403 before any password hashing, with the session preserved and no throttle bucket row created or updated, so a user must log out before switching credentials; GET requests to the login pages remain unaffected. Production always enables throttling and always fails closed: a throttle database failure answers a generic 503 for login POSTs from unauthenticated sessions while health checks and GET requests stay unaffected. `X-Forwarded-For` is honored only when `REMOTE_ADDR` belongs to a configured trusted proxy CIDR; the chain is then parsed right to left and the first untrusted valid address wins, while an untrusted `REMOTE_ADDR` causes the header to be ignored entirely. All throttle variables are documented in `.env.example` and passed to the web service by Compose.

Local development stores the database in the gitignored SQLite file created by `migrate`. The container stack instead runs `migrate` at startup against PostgreSQL, so no database file is committed or shipped in the image.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Klaus_Mueller.png" alt="Generative Klaus">   Running a simulation

To run a new simulation, you will need to concurrently start two servers: the environment server and the agent simulation server.

### Step 1. Starting the environment server

Again, the environment is implemented as a Django project, and as such, you will need to start the Django server. To do this, first navigate to `environment/frontend_server` (this is where `manage.py` is located) in your command line. First-time users must complete the migration and account setup described under [Operator authentication](#operator-authentication) before the first `runserver`. Then run the following command:

```bash
DJANGO_DEBUG=true uv run python manage.py runserver
```

> **Note:** The Django stack has been production-hardened with proper CSRF protection, operator authentication, input validation, and WhiteNoise static file serving. For production deployment, run gunicorn with `DJANGO_ENV=production` (see the included `Procfile`). Run `uv run python manage.py collectstatic --no-input` first to populate the WhiteNoise static cache.

Then, on your favorite browser, go to [http://localhost:8000/](http://localhost:8000/). If you see a message that says, "Your environment server is up and running," your server is running properly. Ensure that the environment server continues to run while you are running the simulation, so keep this command-line tab open! (Note: I recommend using either Chrome or Safari. Firefox might produce some frontend glitches, although it should not interfere with the actual simulation.)

### Step 2. Starting the simulation server

Open up another command line (the one you used in Step 1 should still be running the environment server, so leave that as it is). Navigate to `reverie/backend_server` and run `reverie.py`.

**Interactive mode** (original, with CLI prompts):

```bash
uv run python reverie.py
```

This will start the simulation server. A command-line prompt will appear, asking the following: "Enter the name of the forked simulation: ". To start a 3-agent simulation with Isabella Rodriguez, Maria Lopez, and Klaus Mueller, type the following:

```bash
base_the_ville_isabella_maria_klaus
```

The prompt will then ask, "Enter the name of the new simulation: ". Type any name to denote your current simulation (e.g., just "test-simulation" will do for now).

```bash
test-simulation
```

Keep the simulator server running. At this stage, it will display the following prompt: "Enter option: "

**Service mode** (non-interactive, for production deployment):

```bash
uv run python reverie.py --serve \
    --origin base_the_ville_isabella_maria_klaus \
    --target MySimulation
```

This starts the backend in a non-interactive loop that processes simulation steps as the frontend writes environment files. The service autosaves every 10 steps and exits cleanly on `Ctrl+C`. To resume a previously saved simulation without re-forking:

```bash
uv run python reverie.py --serve --resume \
    --target MySimulation
```

Service mode never reads stdin, so a misconfigured container fails fast instead of hanging. A missing target (`REVERIE_SIM_CODE` / `--target`) always exits with a nonzero status, and a missing origin (`REVERIE_FORK_SIM_CODE` / `--origin`) exits nonzero for a new fork. With `--resume` the origin may be omitted, because resuming an existing target derives the fork code from the target's saved metadata. Runtime folders (`movement/`, `environment/`, and `temp_storage/`) are created on first run when missing, and a fresh fork writes its first movement step without any extra setup. Resuming an existing target preserves the saved origin lineage: the authoritative `fork_sim_code` recorded in the target's saved metadata wins over any Compose or supplied default origin, a supplied origin applies only when the target does not exist and a fresh fork is created, and every save after resume retains the lineage in both the legacy meta and the pointed generation meta.

Service mode paces valid step processing through `REVERIE_MAX_STEPS_PER_SECOND`, a positive finite float that defaults to 0.4 and caps the rate of successful step completions per wall-clock second. The first valid step runs immediately; every later valid step waits, interruptibly, until at least `1 / REVERIE_MAX_STEPS_PER_SECOND` wall-clock seconds have elapsed since the previous successful step completed, so a shutdown signal still interrupts the wait and the normal final save runs. Positive finite subnormal rates remain accepted: the reciprocal interval, even when infinite, is applied in finite interruptible wait chunks, so no `OverflowError` and no busy loop is possible. Missing or invalid environment input keeps the short idle polling and never consumes a rate window, and slow LLM or provider work naturally lowers the actual rate below the cap. The variable tunes wall-clock pacing only: it never changes the simulation clock, and `sec_per_step=10` is unchanged. One simulation day is 8640 steps, so the 0.4 cap allows 24 steps per minute, 1440 per hour, and a theoretical full simulation day in 6 hours when the LLM never lags. In the actual production measurement, the intervals between successful steps had a minimum of 2.5249 seconds, the average rate was 0.3888 steps per second (23.3 to 23.5 steps per minute), and no interval fell under 2.5 seconds, which matches the 0.4 cap with the LLM nearly keeping pace.

Backend and frontend coordinate through two persistent lock files in `temp_storage`. `backend_alive.lock` is a liveness lease: service and interactive modes acquire it after any interactive prompts are resolved and before any runtime marker is cleared, published, or mutated, and hold it for the whole run. A second backend that finds the lease held fails fast with a clear error before touching the live owner's heartbeat or markers, and the kernel releases the lease automatically when the holding process dies, even on SIGKILL, so a replacement backend can acquire it and clear the dead owner's stale markers itself. The frontend probes the lease on every request (before the marker gate, after an environment write, and after a movement read) and answers 503 or the startup error when no backend holds it. `runtime.lock` serializes the marker traffic itself: the backend holds it for marker clear, marker publish, and per-step updates, and the frontend holds it from before the first marker read through validation, the per-step lock, the immutable comparison, and the atomic write, so a restart or prune can never interleave with an in-flight request. Runtime markers are published only after server initialization and provider-key validation succeed, and are cleared again, while the lease is still held, on every exit path.

On resume, persona spatial state initializes from the current step's environment snapshot when it is valid, otherwise from the latest valid prior snapshot without decrementing the step. A saved step that is negative, non-integer, or otherwise invalid, or a simulation with no valid environment snapshot at all, fails resume with a clear error instead of starting in an undefined world state. The same snapshot discipline protects backend startup and both live loops: only canonical regular numeric names of at most 16 digits, the canonical cross-stack bound matching `Number.MAX_SAFE_INTEGER`, are considered, reads open through an `O_NOFOLLOW` descriptor, and symlink, swap, FIFO, and directory entries are ignored, so a stale or hostile environment entry can never be followed. Both live loops stop safely at the terminal step: processing `Number.MAX_SAFE_INTEGER - 1` advances exactly once to the terminal step with a valid marker and save, and at the terminal step interactive mode returns without publishing movement while service mode keeps its heartbeat without moving.

The backend persists progress as versioned checkpoint generations rather than overwriting a single save. Each save stages a complete generation (meta plus every persona's bootstrap memory), fsyncs it, atomically renames it into place, and commits it by atomically publishing a small `reverie/checkpoints/pointer.json`. The pointer is the single commit marker, so an interruption between persona files can never leave a mixed generation authoritative, and an interruption before the pointer commit leaves the prior generation loadable. The previous generation is retained for recovery, and simulations without a pointer keep using the legacy layout unchanged. Resume and fresh forks read the pointed generation, so state never mixes across generations. Every checkpoint, legacy, and staged meta object is schema-validated (required fields, safe origin code, parseable dates, positive finite `sec_per_step`, safe maze name, unique safe persona names, bounded integer step) before any field is indexed or any path is constructed, and every JSON read beneath the simulation root is descriptor-relative: the root opens with `O_DIRECTORY | O_NOFOLLOW`, each parent opens with `dir_fd` from the previous descriptor, and the final file opens with `O_RDONLY | O_NOFOLLOW`, fstats as a regular file before parsing, and is closed on every path. A symlinked ancestor, a symlinked pointer or persona memory file, an unsafe generation name, or an incomplete pointed generation fails startup with a clear controlled error instead of falling back to legacy state. The legacy layout is selected only when the pointer file is genuinely absent: a pointer that exists but parses to JSON null, a scalar, a list, or malformed content fails closed on the backend exactly as it does across the frontend home, replay, persona-state, and process paths.

Checkpoint compatibility with plan thoughts: the backend's daily plan path persists a thought node whose `filling` is JSON null, so a committed checkpoint legitimately carries null-filling thoughts. Thought nodes require the `filling` key, whose value may be an explicit JSON null or a list; omitting the key remains invalid. The frontend node-shape validation now accepts JSON null or a list for a thought's `filling` while a chat's `filling` must remain a list, which resolves the `process_environment` 500 that appeared after the first checkpoint containing such a thought and keeps the persona-state page rendering those thoughts.

A fresh fork is staged and committed the same way: the origin is validated as a real directory and its device and inode identity is recorded, it is copied into an unpredictable unique `.staging-<uuid>` sibling with symlinks preserved as symlinks (never dereferenced into the staged tree), its meta is rewritten atomically, and the staged tree is fully validated (meta schema and dates, bounded step, loadable maze, safe unique persona names, a complete pointed checkpoint or legacy bootstrap memory, descriptor-safe persona loads, and an in-bounds environment snapshot), fsynced file by file and directory by directory, then atomically renamed into the final target with the Linux `renameat2(RENAME_NOREPLACE)` syscall so the kernel itself refuses to replace a concurrently appearing target. The origin identity is verified again after the copy, so an origin swapped mid-copy publishes nothing. Any failure removes only this invocation's staging, a leftover deterministic `.staging` sibling from an interrupted old-version fork is cleaned first, and an existing incomplete final target is never deleted automatically: resume refuses it with a clear error and a fresh fork refuses to overwrite it. When the origin has a committed checkpoint, the fork copies the pointed latest state, syncs the legacy personas to it, and records the immediate origin code in both the staging legacy `reverie/meta.json` and the pointed generation meta, so a resume reads the immediate origin lineage from the authoritative checkpoint. On any server initialization, resume, or fork, stale uncommitted artifacts are pruned before the frontend is signaled: movement files at or beyond the committed step are removed because the backend recomputes them, and environment files beyond the committed step are removed so stale future input can never be consumed after a replay. Live pruning runs again after every processed step while the service is running. The origin must already exist in the storage volume, which is provisioned only with the two bundled base simulations; any previously forked simulation is reusable, and an arbitrary code that is not in the volume fails with a clear error.

Signals request a cooperative shutdown instead of interrupting mid-transaction. SIGTERM and Ctrl+C set a shutdown flag that the service loop checks only at safe points, so the current step finishes, the backend publishes a final heartbeat, and a checkpoint is written before the process exits. The Compose stack grants the backend a 6-minute stop grace period for this. Before the loop starts, service mode validates that every provider in use has its API key and fails fast with a clear error naming only the provider, never the key, so a misconfigured container cannot start, publish a heartbeat, and then stall on a missing key. Provider exhaustion mid-run is likewise a controlled exit: the process prints the sanitized `LLMProviderError`, exits with status 1 without a traceback, clears the runtime markers, and releases the backend lease, so the Compose stack restarts it from the last committed checkpoint.

### Step 3. Running and saving the simulation

On your browser, navigate to [http://localhost:8000/simulator_home](http://localhost:8000/simulator_home). You should see the map of Smallville, along with a list of active agents on the map. You can move around the map using your keyboard arrows. Please keep this tab open. To run the simulation, type the following command in your simulation server in response to the prompt, "Enter option":

```bash
run <step-count>
```

Note that you will want to replace `<step-count>` above with an integer indicating the number of game steps you want to simulate. For instance, if you want to simulate 100 game steps, you should input `run 100`. One game step represents 10 seconds in the game.

Your simulation should be running, and you will see the agents moving on the map in your browser. Once the simulation finishes running, the "Enter option" prompt will re-appear. At this point, you can simulate more steps by re-entering the run command with your desired game steps, exit the simulation without saving by typing `exit`, or save and exit by typing `fin`.

> **Note:** A fresh simulation's first step is long by architecture, not by accident. The original planning chain runs many sequential hourly LLM calls per persona (the wake-up hour and daily plan prompts plus up to three diversity passes over the 24 hourly slots, followed by per-step decomposition and action prompts), and with provider-default reasoning and no application output cap a first step can take hours by explicit operator choice. This is processing, not necessarily failure. While step 0 is being planned, the dashboard shows explicit waiting text, elapsed time, backend connectivity and provider request/queue activity. The environment update endpoint answers `202 pending`, because `movement/0` is not produced until the step completes. At Hetzner’s 10 RPM ceiling the original daily-planning chain can take tens of minutes or longer; request progress is not a completed simulation step. Check progress with `docker compose -f docker-compose.prod.yml ps`, follow the logs with `docker compose -f docker-compose.prod.yml logs -f`, and inspect the heartbeat timestamp in `backend_heartbeat.json`. The background heartbeat publisher refreshes every 5 seconds even while a step blocks on the LLM, so the backend should remain healthy throughout.

Interactive `run N` follows the same hardening as service mode. Each step's environment snapshot must pass the same strict validation (non-bool non-negative integer coordinates inside the maze bounds for every persona), movement is published atomically, the step marker is updated under the runtime lock, stale artifacts are pruned, and the heartbeat is refreshed; the run command dispatches to the server instance's own step loop rather than any module-level state. Invalid or malformed input is ignored with a warning instead of crashing or advancing the step, and a failed movement write leaves the step and markers untouched. The `run` syntax captures exactly 1 to 5 digits and accepts counts from 0 to 10000; malformed counts, extra arguments, negative or oversized values, and huge inputs such as a 100k-digit count are rejected with a warning before any integer conversion, so no traceback is possible.

The saved simulation can be accessed the next time you run the simulation server by providing the name of your simulation as the forked simulation. This will allow you to restart your simulation from the point where you left off.

The live simulator coordinates with the backend through a runtime handshake. The home view enters the `process` phase when the environment for the expected step is not yet published and the `update` phase when it already is, so a reload resumes where the browser left off. The frontend posts the environment for exactly the step the backend expects, as published in `temp_storage/curr_step.json` and `temp_storage/curr_sim_code.json`. A stale step, a future step, or a simulation mismatch is answered 409, and a missing or corrupt marker is answered 503, both without writing any file so the browser can retry. Each environment file is written under its own per-step lock and is immutable once published: an identical retry answers 200 without rewriting, a differing or corrupt payload answers 409 and the existing file is preserved, and no prior or concurrent environment is ever deleted. The browser guards against overlapping requests with in-flight flags and exponential backoff (base delay 250 ms, doubling, retrying indefinitely while the phase and step remain current, with the delay capped at 5 seconds and the exponential attempt counter capped after five increases), advances from `process` to `update` only when the posted step still matches the expected step, treats movement as ready only when the payload matches the expected step and shape, polling with the same backoff otherwise, and reloads once, guarded by a resync flag, when a movement mismatch returns a resync payload carrying the expected step and simulation code.

The live and replay views use local resident sprites and portraits, a responsive map, whole-town/zoom/follow controls, drag panning and visible pause/resume buttons. Movement interpolates over 650 ms using elapsed frame time, clamps after a suspended browser tab, and commits integer tile positions only after every resident finishes. The next environment snapshot uses those logical tiles rather than rounded physics coordinates. Reduced-motion users get the same committed step without interpolation. Pending planning, unavailable backend, malformed movement and request failures have distinct status messages; client requests time out after 30 seconds and retry without duplicating step execution. The authenticated, uncached `/runtime_status/` endpoint forwards only fresh heartbeat state and allowlisted provider counters, never prompts, responses or keys.

`SIMULATOR_TARGET_FPS` sets the Phaser render and update target for the live and replay views. It is a positive integer that defaults to 30, is parsed in the Django base settings, is passed through the home and replay view contexts, and is injected into the browser as a `json_script` value that the simulator reads with a fallback of 30 and applies as `fps: { target: ... }` in the Phaser game config. It affects only the client-side render and update loop: it never changes the simulation clock, the step count, or the action pace, `sec_per_step=10` is untouched, and the actual browser frame rate may sit below the target when the client or the browser cannot keep up. The production browser was confirmed to run with the configured loop target of 30.

### Step 4. Replaying a simulation

You can replay a simulation that you have already run simply by having your environment server running and navigating to the following address in your browser: `http://localhost:8000/replay/<simulation-name>/<starting-time-step>`. Please make sure to replace `<simulation-name>` with the name of the simulation you want to replay, and `<starting-time-step>` with the integer time-step from which you wish to start the replay.

For instance, by visiting the following link, you will initiate a pre-simulated example, starting at time-step 1:
[http://localhost:8000/replay/July1_the_ville_isabella_maria_klaus-step-3-20/1/](http://localhost:8000/replay/July1_the_ville_isabella_maria_klaus-step-3-20/1/)

Replay resolves the environment from the actual canonical numeric snapshot files on disk, so resolution cost is bounded by the number of stored snapshots, never by the requested step value. A requested step above the simulation's available maximum is rejected with 400 or 404 instead of falling back, and symlink, non-regular, ambiguous, and oversized names are ignored, so an oversized or sparse step cannot trigger a huge range allocation or an out-of-directory read. Movement polling during replay uses a dedicated authenticated, CSRF-protected, read-only `replay_movement` endpoint: it posts no environment, never probes the backend lease, answers 202 pending until the movement file for the requested step exists, and answers `complete` when the requested step is beyond the stored movements, so a replay finishes cleanly without a live backend.

The home view, replay, the persona-state page, and the environment post all treat a committed checkpoint as authoritative. When a checkpoint pointer exists, the pointed generation must be complete (real directories, every declared persona with complete bootstrap memory files whose JSON shapes validate fully), and its meta supplies the persona names and maze bounds; a corrupt pointer, an incomplete generation, a symlinked ancestor, or malformed persona JSON answers a controlled error or startup error instead of ever falling back to stale legacy state, and a partial live tree never falls back to compressed storage. Legacy selection is reserved for a genuinely missing pointer file: a pointer that exists but parses to JSON null, a scalar, a list, or malformed content fails closed across home, replay, persona-state, and process instead of falling back to legacy state. The compressed persona fallback runs only when the live simulation root is genuinely absent: an incomplete live tree (missing personas, a missing persona, or a missing or partial bootstrap memory) answers 404 with no compressed decoy read. Only simulations without a pointer use the legacy layout. The browser shares the cross-stack step contract: it declares `terminalStep = Number.MAX_SAFE_INTEGER`, accepts the initial step only when `Number.isSafeInteger` holds, advances from the last executable step to the terminal step, and enters a `complete` phase at the terminal step or on an unsafe initial step with no further environment posts or movement polls.

### Step 5. Demoing a simulation

You may have noticed that all character sprites in the replay look identical. We would like to clarify that the replay function is primarily intended for debugging purposes and does not prioritize optimizing the size of the simulation folder or the visuals. To properly demonstrate a simulation with appropriate character sprites, you will need to compress the simulation first. To do this, open the `compress_sim_storage.py` file located in the `reverie` directory using a text editor. Then, execute the `compress` function with the name of the target simulation as its input. By doing so, the simulation file will be compressed, making it ready for demonstration.

To start the demo, go to the following address on your browser: `http://localhost:8000/demo/<simulation-name>/<starting-time-step>/<simulation-speed>`. Note that `<simulation-name>` and `<starting-time-step>` denote the same things as mentioned above. `<simulation-speed>` can be set to control the demo speed, where 1 is the slowest, and 5 is the fastest. For instance, visiting the following link will start a pre-simulated example, beginning at time-step 1, with a medium demo speed:
[http://localhost:8000/demo/July1_the_ville_isabella_maria_klaus-step-3-20/1/3/](http://localhost:8000/demo/July1_the_ville_isabella_maria_klaus-step-3-20/1/3/)

### Tips

We've noticed that OpenAI's API can hang when it reaches the hourly rate limit. When this happens, you may need to restart your simulation. For now, we recommend saving your simulation often as you progress to ensure that you lose as little of the simulation as possible when you do need to stop and rerun it. Running these simulations, at least as of early 2023, could be somewhat costly, especially when there are many agents in the environment.

## Testing

The repository ships two test suites, both offline, key-free, and network-isolated:

For the optional automated browser regression test, install Playwright outside the checkout and direct its generated output to a local directory outside the repository:

```bash
npm_config_cache=/tmp/smallville-npm-cache npm install --prefix /tmp/smallville-browser playwright
uv run python scripts/browser_fixture.py --playwright-dir /tmp/smallville-browser/node_modules --screenshots /tmp/smallville-browser-results
```

This test uses mocked providers and disposable simulation storage. It requires Chromium (`CHROMIUM_PATH` can select its executable). Generated screenshots and run output are local test artifacts and should not be committed. The harness's default output directory is also gitignored.


- Django: run `uv run python manage.py test` from `environment/frontend_server`. 332 tests split across `test_auth_settings_routes.py` (53), `test_environment_input.py` (52), `test_runtime_coordination.py` (83), `test_replay_persona.py` (64), `test_filesystem_symlinks.py` (22), `test_simulator_scripts.py` (15), `test_runtime_status.py` (4), and `test_login_throttle.py` (39), with shared helpers in `test_helpers.py` and `tests.py` retained as a stub entry point, cover the checkpoint pointer fail-closed contract (legacy selection only for a genuinely missing pointer file, while a pointer that parses to JSON null, a scalar, a list, or malformed content fails controlled across home, replay, persona-state, and process), the compressed persona fallback scope (compressed storage read only when the live simulation root is genuinely absent, with incomplete live trees answering 404 and never reading compressed storage), auth redirects and login flow, CSRF and method enforcement, login throttling (DB-backed source and source-and-account buckets shared across workers with opaque HMAC keys, threshold gating before any password hash, progressive backoff answered 429 with a `Retry-After` cap, a generic body that leaks no username, window expiry reset, success resetting only the pair bucket, no global account lockout across sources, admin login coverage, four regressions for already authenticated sessions (generic 403 on both accounts and admin login posts before any password hash or `authenticate` call, no bucket changes, preserved session, unaffected GET, unchanged anonymous path), GET and health exemption, CSRF-invalid posts never counted or hashed, trusted-proxy `X-Forwarded-For` right-to-left semantics with untrusted XFF ignored, malformed and all-trusted chains falling back to a stable opaque source, fail-closed 503 on a throttle database failure with health and GET unaffected, production forcing throttling on and fail-open off, strict settings parsing that rejects invalid integers, booleans, and CIDRs, concurrent-race hash bounding with no lost updates, and the bounded stale bucket sweep), malformed JSON and input validation, environment payload validation (non-empty object, per-persona records, non-bool non-negative integer coordinates, authoritative maze bounds from the simulation's maze metadata with no bypass through an unknown record maze, required personas), atomic environment persistence that preserves prior content when a replace fails, the runtime marker gate (the exact backend-published step and simulation code are accepted; a stale or future step or a simulation mismatch is rejected with 409 without writing; and a missing, corrupt, boolean, string, negative, or non-object step marker answers 503), retryable movement responses (the live marker names the next expected environment step, so movement N answers 202 pending while the marker still names N and becomes readable only after the marker advances to N+1, and absent, in-progress, corrupt, or non-object movement answers 202 pending with 200 only when data is ready), the authenticated read-only replay movement endpoint (CSRF-protected, posts no environment, never probes the backend lease, answers 202 pending until the movement file exists, and answers complete when the requested step is beyond the stored movements), shared runtime-lock serialization (a request holder blocks marker clear, and a marker holder blocks the request until markers settle), immutable environment writes (an identical retry answers 200 without rewriting and keeps the inode, an absent file is distinguished from an existing file whose content parses to null, false, zero, a list, or corrupt bytes so each existing case answers 409 with the existing bytes and inode preserved, a pre-existing environment is never deleted, and exactly one concurrent writer wins behind the per-step flock), backend-alive lease probes (a free lease reports unavailable and creates the lock file without ever unlinking it, thread-held and subprocess-held leases report alive, SIGKILL releases the lease, a symlink at the lease path is refused, and the lease gates process, update, and home), simulator saved-step selection that never consumes future snapshots, checkpoint-aware bounds, complete checkpoint UI authority (a corrupt or incomplete pointer never falls back to stale legacy state, checkpoint persona names supersede stale legacy names, the pointed meta supplies authoritative maze bounds, process environment validates against checkpoint personas, and a partial live tree never falls back to compressed storage), full persona JSON shape validation for the persona-state page (scratch, spatial, embeddings, kw strength, and chat/thought node JSON with noncontiguous key, unsupported type, missing-field, and filling-shape rejection: a thought's `filling` may be JSON null or a list because the backend persists plan thoughts with null filling, while a chat's `filling` must be a list, so the first checkpoint containing a plan thought no longer answers 500 through `process_environment` or the persona-state page), demo and replay limits, public demo reads of the compressed `compressed_storage` JSON through descriptor-relative traversal (a symlinked or swapped compressed root, simulation directory, or file answers 404 without reading outside), the persona-state compressed fallback that walks the `compressed_storage` root, simulation, personas, persona, and bootstrap_memory descriptors with `O_NOFOLLOW` so a symlinked or swapped ancestor answers 404 without reading outside while the valid fallback still renders, with root and ancestor symlink tests, ancestor swap-to-symlink tests, and a no-file-descriptor-leak assertion, descriptor-relative runtime marker reads (a symlinked or swapped `temp_storage` root or marker answers 503 instead of reading outside the runtime directory), canonical numeric snapshot enumeration that is bounded by the actual on-disk entries, ignores symlinks, non-regular, ambiguous, and oversized names, and rejects numeric stems above the cross-stack `Number.MAX_SAFE_INTEGER` even when the stem is exactly 16 digits, strict-max replay rejection of a requested step above the available maximum without any huge range allocation, the authenticated oversized-replay DoS regression (a sparse huge step answers 400 or 404 quickly with a single directory scan), canonical step validation that accepts exactly the canonical 16-digit form and the cross-stack `Number.MAX_SAFE_INTEGER` maximum while rejecting leading zeros, signs, non-decimal values, and 17-digit inputs before any integer conversion or directory scan across process, update, replay movement, and replay, huge-integer JSON regression control (a huge integer in an environment or movement payload answers a controlled 400), symlink ancestor rejection and descriptor-relative traversal swap fail-closed tests, the health endpoint and its host allowlist, production settings policy (env selection, secret key policy: minimum length, known and common weak terms, repeated character and pattern rejection, and a Shannon entropy estimate of at least 200 bits with a generated Django key and a strong 64-character random hex key accepted while short, placeholder, repeated, and predictable keys fail, DEBUG and SQLite rejection, database fields, stdout-safe logging, secure cookies), reverie test-module import safety, `SimulatorTargetFpsSettingsTests` (unset, blank, and whitespace overrides default to 30 while malformed, zero, and negative values are rejected) plus the `simulator_target_fps` home and replay context tests, and eleven simulator script tests that parse the active scripts through Node (skipping cleanly when Node is not installed) and assert the server recovery context, the process-to-update handoff with retries, the one-shot resync reload on conflict, movement polling that retries without duplicate execution, the safe-integer terminal marker contract (`Number.MAX_SAFE_INTEGER` with `Number.isSafeInteger` validation of the initial step), the terminal phase that stops both simulate and replay modes with no environment posts or movement polls, the unsafe-initial-step resync that reloads once with no network requests, the `simulator_target_fps` template bridge (the `json_script` element, the `getJsonScriptValue` read with a fallback of 30, and the `fps: { target: simulatorTargetFps }` Phaser config), and the game config that uses the injected setting and falls back to 30.
- Backend: run `uv run python -m unittest discover -s reverie/backend_server -p "test_*.py"`. 598 tests (18 embedding-space/provenance, 16 Hetzner provider/gate, 2 real path progression, 14 embedding privacy, 37 LLM config hardening, 41 service lifecycle, 77 LLM provider retries, 8 scratch serialization, 239 durability, 61 checkpoints, 16 artifact pruning, 15 memory durability, 2 maze, 7 utils paths, 45 plan grounding) cover embedding privacy; LLM config hardening (malformed JSON fail-fast, model selection, provider attribution); LLM provider retry policy (one bounded exponential retry helper shared by every chat and embedding wrapper with fail-fail-success and backoff assertions, sanitized exhaustion errors that never leak prompts, keys, or response bodies, immediate propagation of `KeyboardInterrupt` and `SystemExit`, provider failures re-raised before validators or cleanup can consume them, clients built with an explicit timeout and zero SDK retries, and the retry config contract with defaults 3 / 1.0 / 60.0 plus import-time rejection of malformed, out-of-range, and non-finite values whose error messages never echo the raw invalid value); service lifecycle (fresh fork, first movement step, runtime directory creation, resume, SIGTERM save, missing-code fail-fast, fresh-fork SIGTERM persistence, resume environment fallback and a real post-step SIGTERM regression, simulation-code validation, safe persona names, and resume origin lineage where the saved `fork_sim_code` wins over a supplied or Compose default origin, the supplied origin applies only to a fresh target, an invalid saved origin fails closed, and save retains the lineage in both the legacy and pointed meta); null-safe and warning-free scratch serialization; atomic crash-safe JSON publication; environment shape and bounds validation that never produces movement output; snapshot resolution that enumerates actual canonical regular numeric files, stays bounded by the on-disk entries instead of the committed step with no huge range allocation, ignores symlinks, non-numeric, and oversized names, and rejects numeric stems above `Number.MAX_SAFE_INTEGER` even at 16 digits; backend startup and both live loops read snapshots only as canonical regular files of at most 16 digits (the cross-stack `Number.MAX_SAFE_INTEGER` bound) through an `O_NOFOLLOW` descriptor, so symlink, swap, FIFO, and directory entries are ignored; cooperative SIGTERM mid-step completion with a coherent checkpoint; fresh-fork staging atomicity and concurrent-target refusal; heartbeat startup and throttling; service-mode provider key fail-fast without key exposure; versioned checkpoint generations with an atomic pointer and previous-generation retention; artifact pruning of stale movement and future environment files through descriptor-relative directory opens and `dir_fd` unlinks that never follow or remove symlinks, non-regular entries, or beyond-maximum names; no-`ResourceWarning` UTF-8 persona memory I/O; runtime-lock mutual exclusion across marker clear, publish, and per-step updates in threads and processes, with markers unlinked through `dir_fd` against a verified temp root and published through `atomic_write_json_beneath` so no marker path is ever followed; the backend liveness lease (held during markers and heartbeat, released on provider failure, exceptions, and SIGKILL, a second owner refused, a second backend leaving live owner markers unchanged, a replacement republishing after owner death, markers cleared before lease release in both service and interactive modes, interactive prompts preceding lease acquisition, and interactive publishing before `open_server`); provider exhaustion exiting both runners with status 1, a sanitized message, no traceback, no movement publication or save for the failing step, markers cleared, and the lease released so a Compose restart resumes from the committed checkpoint; and the interactive run loop (atomic movement publication with marker and heartbeat advance, strict validation that never advances step or marker on invalid input, a movement-write failure leaving state untouched, the run command dispatching to `self.start_server` instead of the module-level global, and a `run <count>` syntax that captures exactly 1 to 5 digits and accepts 0 to 10000 while malformed, negative, oversized, extra-argument, and 100k-digit counts are rejected with a warning before any integer conversion). Legacy `persona_names` metadata is validated before any path construction or fork copy, with traversal, duplicate, non-string, and control-character names rejected; maze metadata reads through a context-managed UTF-8 handle with no `ResourceWarning`. The current durability and checkpoint coverage completes the picture: descriptor-relative JSON traversal (`read_json_beneath` opens the root with `O_DIRECTORY | O_NOFOLLOW`, walks parents with `dir_fd`, opens the final file with `O_RDONLY | O_NOFOLLOW`, fstats it as regular, and closes every descriptor on every path, and the legacy persona memory readers route through `read_json_checked`, which descriptor-walks every ancestor from the verified root so a symlinked parent or an ancestor swapped after the root open is refused instead of followed); complete reverie meta schema validation (required fields, safe origin code, parseable dates, positive finite `sec_per_step`, safe maze name, unique safe persona names, bounded integer step) before any indexing; staged-fork persona shape failures converted to sanitized `ValueError`s that publish nothing; controlled runner startup errors for construction, maze, and missing-origin failures in both modes; cross-stack terminal step safety (`_MAX_STEP` equals `Number.MAX_SAFE_INTEGER` at 16 digits, interactive returns without movement at the terminal step, service keeps its heartbeat without moving, `_MAX_STEP - 1` advances exactly once with a valid marker and save, and a terminal save and restart round-trips); unique per-invocation fork staging that cleans only its own staging on copy, checkpoint-sync, meta-write, rename-collision, and concurrent-winner failures; fresh-fork fsync of every copied file and directory before rename plus a checked parent-directory fsync, with any fsync failure publishing nothing; origin copy that preserves symlinks without dereferencing while symlinks in the origin itself are rejected before copy and staged symlinks are rejected before publish; fork origin device and inode identity recorded before copy and verified after it, so a swapped or symlinked mid-copy origin publishes no target; descriptor-relative history CSV loading with suffix validation, `dir_fd` traversal, final regular-file fstat, and full descriptor closure; the interactive `call -- load history` command with controlled unsafe-suffix and symlink-escape refusal that never calls the legacy reader; architecture-neutral `renameat2` resolution (main program namespace first, then the libc soname) with fail-closed fallback and real no-replace use; checkpoint fail-closed tests for a dangling or non-dangling pointer symlink, checkpoint meta, scratch, and associative-directory symlinks, symlinked simulation root, reverie, or checkpoints ancestors, ancestor swaps to symlinks after the root open, and pointer, generation, and meta swaps after the root open; checkpoint and legacy meta field fail-closed validation; checkpoint load with no `ResourceWarning`; legacy seeds, stale checkpoint staging cleanup on save and start, and fork lineage tests that record the immediate origin in both the pointed and legacy meta so resume reads the origin lineage from the authoritative checkpoint; and persona memory loaders that refuse symlinks, dangling symlinks, and swap-to-symlink races across spatial, scratch, and associative memory. The new `test_utils_paths.py` asserts the configured project paths (`fs_storage`, `fs_temp_storage`, `maze_assets_loc`, `env_matrix`, and `env_visuals`) are normalized absolute paths derived from the backend module location, independent of the process working directory, and free of `..` components, that importing `utils` from the repo root and from the backend working directory yields identical paths matching the documented layout, and that every bundled seed persona memory file (scratch, spatial, and associative) loads through the configured absolute storage path without `ValueError` or `ResourceWarning`. The durability suite is split across `test_durability_atomic.py` (36), `test_durability_snapshot.py` (46), `test_durability_locks.py` (21), `test_durability_service.py` (41), `test_durability_fork.py` (44), and `test_durability_interactive.py` (51), sharing helpers in `test_durability_shared.py` and the `_service_loop_harness.py` harness, and the old monolithic `test_durability.py` is removed. The new `test_plan_grounding.py` adds 45 offline tests for the centralized canonical-choice helper, grounded sector, arena, and game-object generation with mocked prompt functions, hardened `MemoryTree` accessors, and the grounded action address stored by `_determine_action`. The reasoning-effort contract is pinned in `test_llm_config_hardening.py` (`ReasoningEffortRequestTests`: every chat wrapper receives the provider-specific reasoning kwargs only when configured, with the OpenRouter nested `extra_body` reasoning block, the OpenAI top-level `reasoning_effort`, and nothing sent when unset) and in `test_llm_provider_retries.py` (`ReasoningEffortParsingTests`, `ReasoningEffortKwargsTests`, and `ReasoningEffortImportTests`: blank and whitespace-only values parse to `None`, the allowed set `none, minimal, low, medium, high, xhigh, max` is enforced with sanitized errors that never echo the raw value, and import-time rejection covers invalid values). `test_embedding_privacy.py` pins the OpenRouter `encoding_format=float` request and the absence of OpenRouter-only fields on the OpenAI path, `test_llm_config_hardening.py` pins the OpenRouter Gemini embedding request contract, and the `HeartbeatTests` in `test_durability_service.py` pin the background heartbeat publisher: startup publication overwriting stale files, freshness maintained while a move is blocked on the LLM, coherent step and time payloads, thread stop on interrupt, exception, and cooperative shutdown, sanitized failure propagation at the safe boundary, and valid JSON output. The same file's `MaxStepRatePacingTests` pin the service-mode step pacing: the first valid step runs immediately, later valid steps wait at least `1 / REVERIE_MAX_STEPS_PER_SECOND` after the previous success, the pacing window scales with the configured rate, a signal interrupts the wait and still final-saves, missing and invalid environment input keep the idle poll without consuming a rate window, and the autosave cadence and the heartbeat continue during pacing. `RetryConfigParsingTests` (including the max-steps-per-second parser tests) and `MaxStepsPerSecondImportTests` in `test_llm_provider_retries.py` pin the parser and import contract: blank yields the 0.4 default, valid finite values parse, and malformed, zero, negative, and non-finite values fail fast with sanitized errors that never echo the raw value; a heartbeat publication failure exits the service loop controlled through `HeartbeatPublicationError`.

All backend tests run against in-memory fakes for `Maze` and `Persona` and confine file I/O to temporary directories; the subprocess lifecycle tests execute the real save path offline. Neither suite performs a live LLM call, a real browser step, or any network request, and the provider and lifecycle guarantees are asserted with mocked and subprocess behavior only. The 7 utils path tests require the checked-in storage, maze assets, matrix, and visuals trees to exist and assert that every configured backend path is absolute, normalized, and cwd-independent, while the ignored generated `environment/frontend_server/temp_storage` may be absent on a clean checkout because `ReverieServer` creates it at runtime and the tests validate its existing parent and expected layout instead. Separately from the suites, on 2026-08-01 exactly one live OpenRouter chat completion succeeded through the project's `ChatGPT_single_request` wrapper with a temporary configuration: provider `openrouter`, model `openai/gpt-4o-mini`, `LLM_API_RETRIES=1`, `LLM_API_RETRY_BASE_SECONDS=0`, and `LLM_API_TIMEOUT_SECONDS=30`. The prompt requested exactly `OK`; the request succeeded once and returned a nonempty response of approximately `OK` in 2.54 seconds, with no retry, no embedding call, and no simulation step. The API key value and the response body were not logged, a leak scan stayed clean, and `.env` remains ignored. The standing key rotation advisory remains because of the prior tool exposure risk. That single request is not a browser end-to-end test and not a full multi-persona simulation end-to-end run, so this README still makes no claim that a simulation was successfully executed end to end with live credentials. That probe was a prior one-off and is not the current routing: the current configuration is OpenRouter with `deepseek/deepseek-v4-flash-0731` and no fallback or persona override, as described under [LLM provider configuration](#llm-provider-configuration).

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Isabella_Rodriguez.png" alt="Generative Isabella">   Production deployment (Docker Compose)

The repo includes a containerized production stack (`seed` + `web` + `backend` + `db`) built from a single `smallville:latest` image.

### Prerequisites

- Docker Engine and Compose Plugin v2 installed.
- A `.env` file at the repo root (copy from `.env.example` first), with at minimum these variables. Compose fails fast when `POSTGRES_PASSWORD` or `REVERIE_SIM_CODE` is missing:
  - `DJANGO_SECRET_KEY`: a strong random value of at least 50 characters that passes the production entropy and pattern checks
  - `POSTGRES_PASSWORD`: a strong database password (required, no default)
  - `REVERIE_SIM_CODE`: name for the simulation (e.g. `MySimulation`)
  - `OPENROUTER_API_KEY` (or `OPENAI_API_KEY`): for the LLM provider you use
- Optional: `REVERIE_FORK_SIM_CODE`: base fork name, defaults to `base_the_ville_isabella_maria_klaus` (seeded automatically on first run). A custom fork origin must already exist in the storage volume: the seed provisions only the two bundled base simulations (`base_the_ville_isabella_maria_klaus` and `base_the_ville_n25`), any previously forked simulation is reusable, and an arbitrary code that is not in the volume fails in the backend with a clear error before anything is written.

The stack runs with `DJANGO_ENV=production` and `DJANGO_DEBUG=false`. Production settings fail closed: startup is refused on a weak or known-placeholder secret key, on an enabled DEBUG, on SQLite, or without an external database.

The web service never assembles a connection URL. It receives `DB_HOST`, `DB_NAME`, `DB_PASSWORD`, `DB_PORT`, and `DB_USER` as separate fields, so database passwords containing URL-significant characters such as `@`, `:`, `/`, or `%` work without escaping.

```bash
cp .env.example .env
# then edit .env with your values
```

### Build and start

```bash
docker compose -f docker-compose.prod.yml up --build -d
```

This builds the `smallville:latest` image once, then starts four services. The stack otherwise boots autonomously: PostgreSQL starts, the `seed` service validates and provisions the two bundled base simulations idempotently, the `backend` starts or resumes the configured simulation, and the `web` service runs `python manage.py migrate --noinput` and then `collectstatic` before Gunicorn starts.

| Service | What it runs |
|---------|--------------|
| `seed` | one-shot init: validates each bundled base simulation (`reverie/meta.json`, personas, `environment/0.json`, valid JSON, at least one persona), stages a validated copy, writes a `.seed-complete` marker, and atomically renames it into the `smallville_storage` volume. Idempotent: never overwrites existing simulations, and an incomplete existing simulation aborts with a clear error instead of being overwritten |
| `web` | gunicorn (`frontend_server.wsgi`) with Django production settings, bound to `:8000`; runs `migrate` then `collectstatic` before serving |
| `backend` | `reverie.py --serve --resume` (autosaves every 10 steps) |
| `db` | PostgreSQL 16 (hardened derived image, digest-pinned); the web service connects through the separate `DB_*` fields above |

Docker does not create a superuser automatically by design, and it does not generate credentials or `.env` values. You still copy and edit `.env` with the mandatory values described under [Prerequisites](#prerequisites): a strong `DJANGO_SECRET_KEY`, `POSTGRES_PASSWORD`, `REVERIE_SIM_CODE`, your provider API key, and `DJANGO_CSRF_TRUSTED_ORIGINS` and `DJANGO_SECURE_SSL_REDIRECT`. Once the services are healthy, create the superuser manually as described under [First-login and superuser account](#first-login-and-superuser-account). If the stack does not become healthy, inspect it with `docker compose -f docker-compose.prod.yml ps` and follow the logs with `docker compose -f docker-compose.prod.yml logs -f`.

The backend command is declared as exec-form argv in the compose file (a YAML list of `python reverie.py --serve --resume --autosave-interval <n>`), never a shell string, so the autosave interval and flags cannot inject shell commands.

The web service is exposed on `${WEB_PORT:-8000}`. The backend and db communicate on an internal bridge network. The `seed` service runs before `web` and `backend` start, so the storage volume always contains `base_the_ville_isabella_maria_klaus` (and `base_the_ville_n25`) before the backend forks. All other runtime storage (the rest of `storage/`, `temp_storage/`, `static_root/`, `media_root/`, and any `*.sqlite3`) is excluded from the image and lives in volumes or is generated at startup, so the image stays reproducible.

The image also ships the pre-compressed demo simulation used by the public `/demo/` route (for example `July1_the_ville_isabella_maria_klaus-step-3-20` in `compressed_storage`), so demos are available without seeding.

Compose injects only the variables each service needs: the web service gets Django, database, and `LOGIN_THROTTLE_*` fields, the backend gets LLM provider and simulation fields, and the db service gets `POSTGRES_*` only. There is no broad `.env` passthrough, so secrets stay scoped to the service that uses them.

Every Python install artifact is hash-enforced: the Dockerfile exports the frozen lockfile and installs with `--require-hashes`, so nothing can resolve outside `uv.lock`. The scikit-learn dependency was removed because it has no Alpine wheel and would have forced a source build with a compiler chain into the image; the remaining numeric stack (numpy and scipy) installs from prebuilt wheels.

The stack hardens the runtime boundary. The web and backend services run with read-only root filesystems and a writable `/tmp` tmpfs, and the db service runs as uid/gid 70 (postgres) with an authenticated `SELECT 1` healthcheck. Each service carries an explicit healthcheck: the db answers a real query, the backend answers only when `backend_heartbeat.json` is fresh (numeric timestamp no older than 15 seconds) and carries the expected simulation code and container process identity, and the web probes `/health/` over the internal host. The web port binds to the loopback address by default (`WEB_BIND_ADDRESS=127.0.0.1`), so the published port is reachable only from the host or a reverse proxy unless an operator deliberately sets `WEB_BIND_ADDRESS=0.0.0.0`. The web service starts only after the db is healthy, the backend is healthy, and the seed completed successfully. The backend is granted a 6-minute stop grace period, which gives its cooperative SIGTERM handling time to finish the current step and write a checkpoint.

The compose file requires explicit values for `DJANGO_CSRF_TRUSTED_ORIGINS` and `DJANGO_SECURE_SSL_REDIRECT` (no defaults), so a misconfigured `.env` fails fast instead of booting the web service with an unintended CSRF origin or an unintended SSL policy. The Django dependency floor is now `>=5.2.16,<6` with `uv.lock` pinned to 5.2.16, because three Trivy CVEs affecting the previously locked 5.2.15 are fixed only in 5.2.16; the current app and db images report zero Trivy vulnerabilities, secrets, and misconfigurations, and the filesystem scan reports zero vulnerabilities for `uv.lock`, with the only note being the generic image-level health advisory: the single application image serves the one-shot `seed` job and the long-running `web` and `backend` roles, each covered by a role-specific Compose healthcheck. Exact container verification runs are recorded: a Compose stack with no external network access resumed from a committed checkpoint and became healthy; an invalid non-placeholder API key with `LLM_API_RETRIES=1` failed after exactly two attempts with a sanitized message and committed no checkpoint; and a placeholder API key failed provider validation before any runtime marker was published. These runs exercised failure paths only. The single live provider success is the one OpenRouter chat completion of 2026-08-01 described in the testing section above; this README does not claim a browser end-to-end session or a full multi-persona simulation end-to-end run. The latest quality and security gate runs failed on the production secret-key policy, the replay and environment snapshot range enumeration, and the snapshot symlink and name handling; those findings are corrected and regression-tested as recorded above, but no passing final external quality and security gate is claimed yet, and that gate remains a separate release step. The stack is currently fully down by explicit user instruction: all five Compose volumes (`smallville_storage`, `smallville_temp_storage`, `smallville_static`, `smallville_media`, and `smallville_pgdata`) and all app, db, and base images are preserved. The final `EnvaiSimulation` state is checkpoint generation 78 at step 765, dated Feb 13, 02:07:30, with contiguous environment and movement snapshots 0 to 764; a plain `docker compose -f docker-compose.prod.yml up --build -d` resume recreates the stack from that checkpoint. Both QA accounts and their sessions were deleted; the single admin superuser remains, and no credentials are recorded in this repository.

Login throttling was verified against the real Postgres stack with gunicorn running four workers. The recorded evidence: crossing the source or account threshold answered 429 with the expected `Retry-After`; a bucket written by one worker was observed by another, confirming the database is the shared throttle store; a `POSTGRES_PASSWORD` containing URL-significant characters connected without escaping through the separate `DB_*` fields; the startup `migrate` applied migration 0006 and created the `LoginThrottleBucket` table; the table contained only 64-character hex digests with no raw IP address or username; CSRF-invalid login POSTs answered 403 and created no bucket rows and hashed no password; with the db service stopped, login POSTs answered the generic 503 while `/health/` stayed 200; and the bounded sweep removed only its batch of stale rows. The run performed no live LLM call and no browser session.

### First-login and superuser account

Docker does not create a superuser automatically by design. Once the services are healthy, create one inside the running container:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```

Then visit `http://localhost:8000/accounts/login/` and log in with those credentials to access simulation controls (`/simulator_home`, `/path_tester/`). The current stack holds exactly one admin superuser; both QA accounts and their sessions were deleted, and no credentials are recorded in this repository.

### Shared simulation storage

The `web` and `backend` services share the same simulation storage through Docker volumes: specifically `smallville_storage` (simulations) and `smallville_temp_storage` (runtime state). The frontend writes environment files that the backend reads each step, then writes its output back for the frontend to render.

### Historical SQLite databases

Commits from July 2023 tracked local `db.sqlite3` files. Those files were removed from the tree and `*.sqlite3` is now ignored, but the old blobs, including any Django credential hashes they contained, remain in the public Git history. Treat any account or credential created against those files as compromised: rotate the credentials and delete or disable the affected accounts. The public history has not been rewritten; maintainers of private forks who want to purge the old blobs can rewrite their own history, then rotate every secret that ever touched it.

### HTTPS / reverse proxy

The stack enables `DJANGO_SECURE_SSL_REDIRECT=true` by default (secure). When running without a TLS-terminating reverse proxy, override in `.env`:

```bash
DJANGO_SECURE_SSL_REDIRECT=false
```

For production expose, place a reverse proxy (nginx, Caddy, Traefik) in front of the `web` service to handle TLS termination and keep the default SSL redirect on. The proxy must forward `X-Forwarded-Proto: https` (Django's `SECURE_PROXY_SSL_HEADER`); the container healthcheck already sends this header when probing `/health/`.

Set `DJANGO_ALLOWED_HOSTS` to your public domain only (for example `example.com`). The compose file appends the internal `web` host automatically, so the healthcheck can probe `/health/` without relying on `localhost` being allowed. Set `DJANGO_CSRF_TRUSTED_ORIGINS` to the matching HTTPS origin (for example `https://example.com`). The healthcheck probes `http://web:8000/health/` with `X-Forwarded-Proto: https`.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Maria_Lopez.png" alt="Generative Maria">   Simulation storage location

All simulations that you save will be located in `environment/frontend_server/storage`, and all compressed demos will be located in `environment/frontend_server/compressed_storage`.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Sam_Moore.png" alt="Generative Sam">   Customization

There are two ways to optionally customize your simulations.

### Author and load agent history

First is to initialize agents with unique history at the start of the simulation. To do this, you would want to 1) start your simulation using one of the base simulations, and 2) author and load agent history. More specifically, here are the steps:

#### Step 1. Starting up a base simulation

There are two base simulations included in the repository: `base_the_ville_n25` with 25 agents, and `base_the_ville_isabella_maria_klaus` with 3 agents. Load one of the base simulations by following the steps until step 2 above.

#### Step 2. Loading a history file

Then, when prompted with "Enter option: ", you should load the agent history by responding with the following command:

```bash
call -- load history the_ville/<history_file_name>.csv
```

Note that you will need to replace `<history_file_name>` with the name of an existing history file. There are two history files included in the repo as examples: `agent_history_init_n25.csv` for `base_the_ville_n25` and `agent_history_init_n3.csv` for `base_the_ville_isabella_maria_klaus`. These files include semicolon-separated lists of memory records for each of the agents; loading them will insert the memory records into the agents' memory stream.

#### Step 3. Further customization

To customize the initialization by authoring your own history file, place your file in the following folder: `environment/frontend_server/static_dirs/assets/the_ville`. The column format for your custom history file will have to match the example history files included. Therefore, we recommend starting the process by copying and pasting the ones that are already in the repository.

### Create new base simulations

For a more involved customization, you will need to author your own base simulation files. The most straightforward approach would be to copy and paste an existing base simulation folder, renaming and editing it according to your requirements. This process will be simpler if you decide to keep the agent names unchanged. However, if you wish to change their names or increase the number of agents that the Smallville map can accommodate, you might need to directly edit the map using the [Tiled](https://www.mapeditor.org/) map editor.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Eddy_Lin.png" alt="Generative Eddy">   Authors and citation

**Authors:** Joon Sung Park, Joseph C. O'Brien, Carrie J. Cai, Meredith Ringel Morris, Percy Liang, Michael S. Bernstein

Please cite our paper if you use the code or data in this repository.

```text
@inproceedings{Park2023GenerativeAgents,
author = {Park, Joon Sung and O'Brien, Joseph C. and Cai, Carrie J. and Morris, Meredith Ringel and Liang, Percy and Bernstein, Michael S.},
title = {Generative Agents: Interactive Simulacra of Human Behavior},
year = {2023},
publisher = {Association for Computing Machinery},
address = {New York, NY, USA},
booktitle = {In the 36th Annual ACM Symposium on User Interface Software and Technology (UIST '23)},
keywords = {Human-AI interaction, agents, generative AI, large language models},
location = {San Francisco, CA, USA},
series = {UIST '23}
}
```

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Wolfgang_Schulz.png" alt="Generative Wolfgang">   Acknowledgements

We encourage you to support the following three amazing artists who have designed the game assets for this project, especially if you are planning to use the assets included here for your own project:

- Background art: [PixyMoon (@_PixyMoon\_)](https://twitter.com/_PixyMoon_)
- Furniture/interior design: [LimeZu (@lime_px)](https://twitter.com/lime_px)
- Character design: [ぴぽ (@pipohi)](https://twitter.com/pipohi)

In addition, we thank Lindsay Popowski, Philip Guo, Michael Terry, and the Center for Advanced Study in the Behavioral Sciences (CASBS) community for their insights, discussions, and support. Lastly, all locations featured in Smallville are inspired by real-world locations that Joon has frequented as an undergraduate and graduate student: he thanks everyone there for feeding and supporting him all these years.
