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

The dev settings keep local conveniences: `ALLOWED_HOSTS=["*"]`, DEBUG may be enabled, and the database defaults to a local SQLite file created on first use (gitignored, never tracked).

The production settings are fail-closed. Startup is refused unless every requirement below holds:

- `DJANGO_SECRET_KEY` is a strong, unique value of at least 50 characters that is not a public placeholder
- `DJANGO_DEBUG` is not enabled
- an external database is configured either through `DATABASE_URL` or through the `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, and `DB_PORT` fields; SQLite is rejected outright

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Klaus_Mueller.png" alt="Generative Klaus">   LLM provider configuration

Smallville supports two providers: **OpenRouter** (recommended) and **OpenAI**.

### Provider selection

Set `LLM_PROVIDER` in your `.env`:

```env
LLM_PROVIDER=openrouter    # or "openai"
```

When `LLM_PROVIDER` is unset, the code falls back to `openai`.

### API keys

- **OpenRouter**: set `OPENROUTER_API_KEY`
- **OpenAI**: set `OPENAI_API_KEY`

You only need the key for the provider you select.

### Default model

Set `LLM_MODEL` to override the provider-specific default:

| Provider | Default model |
|----------|---------------|
| OpenAI | `gpt-4o` |
| OpenRouter | `openai/gpt-4o` |

Any model slug your provider supports is valid (e.g., `anthropic/claude-3.5-sonnet` on OpenRouter).

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

Prefix a value with `openrouter:` or `openai:` to set both provider and model. A bare model name (e.g., `"gpt-4o"`) uses the current provider. If no override is set for a persona, the global defaults above apply.

### Embedding models

Embeddings use a separate model from chat completions. `LLM_MODEL` is never used for embedding requests. Two `.env` variables control the embedding model:

- `LLM_EMBEDDING_MODEL`: a global embedding model name (optional; a provider-appropriate default is used if unset).
- `LLM_EMBEDDING_MODELS`: a per-provider JSON map, keyed by provider name.

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

### Retry and timeout policy

Every chat completion and embedding request runs through a single bounded exponential retry policy owned by the application. The OpenAI SDK's own retries are disabled (`max_retries=0`), so no second retry layer can multiply attempts. Three `.env` variables tune the policy:

- `LLM_API_RETRIES`: retries after the initial call, integer at least 1, default 3
- `LLM_API_RETRY_BASE_SECONDS`: base delay for exponential backoff, finite number at least 0, default 1
- `LLM_API_TIMEOUT_SECONDS`: per-request timeout, finite number greater than 0, default 60

The backoff doubles per retry starting from the base delay. Malformed, out-of-range, or non-finite values (including `inf`, `-inf`, and `nan`) abort startup with a clear error that names only the variable and its valid range, so a typo can never silently change retry behavior. When every attempt fails, the call raises a typed `LLMProviderError` whose message names only the provider, the operation, and the attempt count; prompts, API keys, and response bodies are never included. Provider failures are never treated as model output: they cannot become validation sentinels, reach persona memory or scratch state, or be persisted as persona state.

On provider exhaustion during a run, both service and interactive modes exit controlled with status 1 and no traceback. The runtime markers are cleared and the backend lease is released, so the Compose stack may restart the backend from the last committed checkpoint; movement, step, and checkpoint state that was already published before the failing call remains on disk.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Klaus_Mueller.png" alt="Generative Klaus">   Operator authentication

Simulation control pages (`/simulator_home`, `/path_tester/`) and POST endpoints require authentication. Create a Django superuser account:

```bash
cd environment/frontend_server
uv run python manage.py createsuperuser
```

Then log in at `/accounts/login/` before accessing simulation controls. This is enforced in all environments (dev and production).

Local development creates the SQLite database on first use, and the file is gitignored. The container stack instead runs `migrate` at startup against PostgreSQL, so no database file is committed or shipped in the image.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Klaus_Mueller.png" alt="Generative Klaus">   Running a simulation

To run a new simulation, you will need to concurrently start two servers: the environment server and the agent simulation server.

### Step 1. Starting the environment server

Again, the environment is implemented as a Django project, and as such, you will need to start the Django server. To do this, first navigate to `environment/frontend_server` (this is where `manage.py` is located) in your command line. Then run the following command:

```bash
uv run python manage.py runserver
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

Service mode never reads stdin, so a misconfigured container fails fast instead of hanging. A missing target (`REVERIE_SIM_CODE` / `--target`) always exits with a nonzero status, and a missing origin (`REVERIE_FORK_SIM_CODE` / `--origin`) exits nonzero for a new fork. With `--resume` the origin may be omitted, because resuming an existing target derives the fork code from the target's saved metadata. Runtime folders (`movement/`, `environment/`, and `temp_storage/`) are created on first run when missing, and a fresh fork writes its first movement step without any extra setup.

Backend and frontend coordinate through two persistent lock files in `temp_storage`. `backend_alive.lock` is a liveness lease: service and interactive modes acquire it after any interactive prompts are resolved and before any runtime marker is cleared, published, or mutated, and hold it for the whole run. A second backend that finds the lease held fails fast with a clear error before touching the live owner's heartbeat or markers, and the kernel releases the lease automatically when the holding process dies, even on SIGKILL, so a replacement backend can acquire it and clear the dead owner's stale markers itself. The frontend probes the lease on every request (before the marker gate, after an environment write, and after a movement read) and answers 503 or the startup error when no backend holds it. `runtime.lock` serializes the marker traffic itself: the backend holds it for marker clear, marker publish, and per-step updates, and the frontend holds it from before the first marker read through validation, the per-step lock, the immutable comparison, and the atomic write, so a restart or prune can never interleave with an in-flight request. Runtime markers are published only after server initialization and provider-key validation succeed, and are cleared again, while the lease is still held, on every exit path.

On resume, persona spatial state initializes from the current step's environment snapshot when it is valid, otherwise from the latest valid prior snapshot without decrementing the step. A saved step that is negative, non-integer, or otherwise invalid, or a simulation with no valid environment snapshot at all, fails resume with a clear error instead of starting in an undefined world state.

The backend persists progress as versioned checkpoint generations rather than overwriting a single save. Each save stages a complete generation (meta plus every persona's bootstrap memory), fsyncs it, atomically renames it into place, and commits it by atomically publishing a small `reverie/checkpoints/pointer.json`. The pointer is the single commit marker, so an interruption between persona files can never leave a mixed generation authoritative, and an interruption before the pointer commit leaves the prior generation loadable. The previous generation is retained for recovery, and simulations without a pointer keep using the legacy layout unchanged. Resume and fresh forks read the pointed generation, so state never mixes across generations.

A fresh fork is staged and committed the same way: the origin is validated, copied into a deterministic `.staging` sibling, its meta is rewritten atomically, and the staging directory is atomically renamed into the final target, so a partial target is never accepted as complete and a concurrently created target is never overwritten. On any server initialization, resume, or fork, stale uncommitted artifacts are pruned before the frontend is signaled: movement files at or beyond the committed step are removed because the backend recomputes them, and environment files beyond the committed step are removed so stale future input can never be consumed after a replay. Live pruning runs again after every processed step while the service is running. The origin must already exist in the storage volume, which is provisioned only with the two bundled base simulations; any previously forked simulation is reusable, and an arbitrary code that is not in the volume fails with a clear error.

Signals request a cooperative shutdown instead of interrupting mid-transaction. SIGTERM and Ctrl+C set a shutdown flag that the service loop checks only at safe points, so the current step finishes, the backend publishes a final heartbeat, and a checkpoint is written before the process exits. The Compose stack grants the backend a 6-minute stop grace period for this. Before the loop starts, service mode validates that every provider in use has its API key and fails fast with a clear error naming only the provider, never the key, so a misconfigured container cannot start, publish a heartbeat, and then stall on a missing key. Provider exhaustion mid-run is likewise a controlled exit: the process prints the sanitized `LLMProviderError`, exits with status 1 without a traceback, clears the runtime markers, and releases the backend lease, so the Compose stack restarts it from the last committed checkpoint.

### Step 3. Running and saving the simulation

On your browser, navigate to [http://localhost:8000/simulator_home](http://localhost:8000/simulator_home). You should see the map of Smallville, along with a list of active agents on the map. You can move around the map using your keyboard arrows. Please keep this tab open. To run the simulation, type the following command in your simulation server in response to the prompt, "Enter option":

```bash
run <step-count>
```

Note that you will want to replace `<step-count>` above with an integer indicating the number of game steps you want to simulate. For instance, if you want to simulate 100 game steps, you should input `run 100`. One game step represents 10 seconds in the game.

Your simulation should be running, and you will see the agents moving on the map in your browser. Once the simulation finishes running, the "Enter option" prompt will re-appear. At this point, you can simulate more steps by re-entering the run command with your desired game steps, exit the simulation without saving by typing `exit`, or save and exit by typing `fin`.

Interactive `run N` follows the same hardening as service mode. Each step's environment snapshot must pass the same strict validation (non-bool non-negative integer coordinates inside the maze bounds for every persona), movement is published atomically, the step marker is updated under the runtime lock, stale artifacts are pruned, and the heartbeat is refreshed; the run command dispatches to the server instance's own step loop rather than any module-level state. Invalid or malformed input is ignored with a warning instead of crashing or advancing the step, and a failed movement write leaves the step and markers untouched.

The saved simulation can be accessed the next time you run the simulation server by providing the name of your simulation as the forked simulation. This will allow you to restart your simulation from the point where you left off.

The live simulator coordinates with the backend through a runtime handshake. The home view enters the `process` phase when the environment for the expected step is not yet published and the `update` phase when it already is, so a reload resumes where the browser left off. The frontend posts the environment for exactly the step the backend expects, as published in `temp_storage/curr_step.json` and `temp_storage/curr_sim_code.json`. A stale step, a future step, or a simulation mismatch is answered 409, and a missing or corrupt marker is answered 503, both without writing any file so the browser can retry. Each environment file is written under its own per-step lock and is immutable once published: an identical retry answers 200 without rewriting, a differing or corrupt payload answers 409 and the existing file is preserved, and no prior or concurrent environment is ever deleted. The browser guards against overlapping requests with in-flight flags and exponential backoff (base delay 250 ms, doubling, retrying indefinitely while the phase and step remain current, with the delay capped at 5 seconds and the exponential attempt counter capped after five increases), advances from `process` to `update` only when the posted step still matches the expected step, treats movement as ready only when the payload matches the expected step and shape, polling with the same backoff otherwise, and reloads once, guarded by a resync flag, when a movement mismatch returns a resync payload carrying the expected step and simulation code.

### Step 4. Replaying a simulation

You can replay a simulation that you have already run simply by having your environment server running and navigating to the following address in your browser: `http://localhost:8000/replay/<simulation-name>/<starting-time-step>`. Please make sure to replace `<simulation-name>` with the name of the simulation you want to replay, and `<starting-time-step>` with the integer time-step from which you wish to start the replay.

For instance, by visiting the following link, you will initiate a pre-simulated example, starting at time-step 1:
[http://localhost:8000/replay/July1_the_ville_isabella_maria_klaus-step-3-20/1/](http://localhost:8000/replay/July1_the_ville_isabella_maria_klaus-step-3-20/1/)

### Step 5. Demoing a simulation

You may have noticed that all character sprites in the replay look identical. We would like to clarify that the replay function is primarily intended for debugging purposes and does not prioritize optimizing the size of the simulation folder or the visuals. To properly demonstrate a simulation with appropriate character sprites, you will need to compress the simulation first. To do this, open the `compress_sim_storage.py` file located in the `reverie` directory using a text editor. Then, execute the `compress` function with the name of the target simulation as its input. By doing so, the simulation file will be compressed, making it ready for demonstration.

To start the demo, go to the following address on your browser: `http://localhost:8000/demo/<simulation-name>/<starting-time-step>/<simulation-speed>`. Note that `<simulation-name>` and `<starting-time-step>` denote the same things as mentioned above. `<simulation-speed>` can be set to control the demo speed, where 1 is the slowest, and 5 is the fastest. For instance, visiting the following link will start a pre-simulated example, beginning at time-step 1, with a medium demo speed:
[http://localhost:8000/demo/July1_the_ville_isabella_maria_klaus-step-3-20/1/3/](http://localhost:8000/demo/July1_the_ville_isabella_maria_klaus-step-3-20/1/3/)

### Tips

We've noticed that OpenAI's API can hang when it reaches the hourly rate limit. When this happens, you may need to restart your simulation. For now, we recommend saving your simulation often as you progress to ensure that you lose as little of the simulation as possible when you do need to stop and rerun it. Running these simulations, at least as of early 2023, could be somewhat costly, especially when there are many agents in the environment.

## Testing

The repository ships two test suites, both offline, key-free, and network-isolated:

- Django: run `uv run python manage.py test` from `environment/frontend_server`. 131 tests cover auth redirects and login flow, CSRF and method enforcement, malformed JSON and input validation, environment payload validation (non-empty object, per-persona records, non-bool non-negative integer coordinates, authoritative maze bounds from the simulation's maze metadata with no bypass through an unknown record maze, required personas), atomic environment persistence that preserves prior content when a replace fails, the runtime marker gate (the exact backend-published step and simulation code are accepted; a stale or future step or a simulation mismatch is rejected with 409 without writing; and a missing, corrupt, boolean, string, negative, or non-object step marker answers 503), retryable movement responses (202 pending for absent, in-progress, corrupt, or non-object movement, 200 with data when ready), shared runtime-lock serialization (a request holder blocks marker clear, and a marker holder blocks the request until markers settle), immutable environment writes (an identical retry answers 200 without rewriting, a differing or corrupt file answers 409 and is preserved, a pre-existing environment is never deleted, and exactly one concurrent writer wins behind the per-step flock), backend-alive lease probes (a free lease reports unavailable and creates the lock file without ever unlinking it, thread-held and subprocess-held leases report alive, SIGKILL releases the lease, a symlink at the lease path is refused, and the lease gates process, update, and home), simulator saved-step selection that never consumes future snapshots, demo and replay limits, the health endpoint and its host allowlist, production settings policy (env selection, secret key strength, DEBUG and SQLite rejection, database fields, stdout-safe logging, secure cookies), reverie test-module import safety, and five simulator script tests that parse the active scripts and assert the server recovery context, the process-to-update handoff with retries, the one-shot resync reload on conflict, and movement polling that retries without duplicate execution. The parse check runs through Node and skips cleanly when Node is not installed.
- Backend: run `uv run python -m unittest discover -s reverie/backend_server -p "test_*.py"`. 244 tests (12 embedding privacy, 26 LLM config hardening, 34 service lifecycle, 33 LLM provider retries, 8 scratch serialization, 82 durability, 29 checkpoints, 10 artifact pruning, 10 memory durability) cover embedding privacy; LLM config hardening (malformed JSON fail-fast, model selection, provider attribution); LLM provider retry policy (one bounded exponential retry helper shared by every chat and embedding wrapper with fail-fail-success and backoff assertions, sanitized exhaustion errors that never leak prompts, keys, or response bodies, immediate propagation of `KeyboardInterrupt` and `SystemExit`, provider failures re-raised before validators or cleanup can consume them, clients built with an explicit timeout and zero SDK retries, and the retry config contract with defaults 3 / 1.0 / 60.0 plus import-time rejection of malformed, out-of-range, and non-finite values); service lifecycle (fresh fork, first movement step, runtime directory creation, resume, SIGTERM save, missing-code fail-fast, fresh-fork SIGTERM persistence, resume environment fallback and a real post-step SIGTERM regression, simulation-code validation, safe persona names); null-safe and warning-free scratch serialization; atomic crash-safe JSON publication; environment shape and bounds validation that never produces movement output; cooperative SIGTERM mid-step completion with a coherent checkpoint; fresh-fork staging atomicity and concurrent-target refusal; heartbeat startup and throttling; service-mode provider key fail-fast without key exposure; versioned checkpoint generations with an atomic pointer and previous-generation retention; artifact pruning of stale movement and future environment files; no-`ResourceWarning` UTF-8 persona memory I/O; runtime-lock mutual exclusion across marker clear, publish, and per-step updates in threads and processes; the backend liveness lease (held during markers and heartbeat, released on provider failure, exceptions, and SIGKILL, a second owner refused, a second backend leaving live owner markers unchanged, a replacement republishing after owner death, markers cleared before lease release in both service and interactive modes, interactive prompts preceding lease acquisition, and interactive publishing before `open_server`); provider exhaustion exiting both runners with status 1, a sanitized message, no traceback, no movement publication or save for the failing step, markers cleared, and the lease released so a Compose restart resumes from the committed checkpoint; and the interactive run loop (atomic movement publication with marker and heartbeat advance, strict validation that never advances step or marker on invalid input, a movement-write failure leaving state untouched, and the run command dispatching to `self.start_server` instead of the module-level global).

All backend tests run against in-memory fakes for `Maze` and `Persona` and confine file I/O to temporary directories; the subprocess lifecycle tests execute the real save path offline. Neither suite performs a live LLM call, a real browser step, or any network request, and this README makes no claim that a simulation was successfully executed end to end with live credentials. The provider and lifecycle guarantees are asserted with mocked and subprocess behavior only.

## <img src="https://joonsungpark.s3.amazonaws.com:443/static/assets/characters/profile/Isabella_Rodriguez.png" alt="Generative Isabella">   Production deployment (Docker Compose)

The repo includes a containerized production stack (`seed` + `web` + `backend` + `db`) built from a single `smallville:latest` image.

### Prerequisites

- Docker Engine and Compose Plugin v2 installed.
- A `.env` file at the repo root (copy from `.env.example` first), with at minimum these variables. Compose fails fast when `POSTGRES_PASSWORD` or `REVERIE_SIM_CODE` is missing:
  - `DJANGO_SECRET_KEY`: a random 50+ character string
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

This builds the `smallville:latest` image once, then starts four services:

| Service | What it runs |
|---------|--------------|
| `seed` | one-shot init: validates each bundled base simulation (`reverie/meta.json`, personas, `environment/0.json`, valid JSON, at least one persona), stages a validated copy, writes a `.seed-complete` marker, and atomically renames it into the `smallville_storage` volume. Idempotent: never overwrites existing simulations, and an incomplete existing simulation aborts with a clear error instead of being overwritten |
| `web` | gunicorn (`frontend_server.wsgi`) with Django production settings, bound to `:8000`; runs `migrate` then `collectstatic` before serving |
| `backend` | `reverie.py --serve --resume` (autosaves every 10 steps) |
| `db` | PostgreSQL 16 (hardened derived image, digest-pinned); the web service connects through the separate `DB_*` fields above |

The web service is exposed on `${WEB_PORT:-8000}`. The backend and db communicate on an internal bridge network. The `seed` service runs before `web` and `backend` start, so the storage volume always contains `base_the_ville_isabella_maria_klaus` (and `base_the_ville_n25`) before the backend forks. All other runtime storage (the rest of `storage/`, `temp_storage/`, `static_root/`, `media_root/`, and any `*.sqlite3`) is excluded from the image and lives in volumes or is generated at startup, so the image stays reproducible.

The image also ships the pre-compressed demo simulation used by the public `/demo/` route (for example `July1_the_ville_isabella_maria_klaus-step-3-20` in `compressed_storage`), so demos are available without seeding.

Compose injects only the variables each service needs: the web service gets Django and database fields, the backend gets LLM provider and simulation fields, and the db service gets `POSTGRES_*` only. There is no broad `.env` passthrough, so secrets stay scoped to the service that uses them.

Every Python install artifact is hash-enforced: the Dockerfile exports the frozen lockfile and installs with `--require-hashes`, so nothing can resolve outside `uv.lock`. The scikit-learn dependency was removed because it has no Alpine wheel and would have forced a source build with a compiler chain into the image; the remaining numeric stack (numpy and scipy) installs from prebuilt wheels.

The stack hardens the runtime boundary. The web and backend services run with read-only root filesystems and a writable `/tmp` tmpfs, and the db service runs as uid/gid 70 (postgres) with an authenticated `SELECT 1` healthcheck. Each service carries an explicit healthcheck: the db answers a real query, the backend answers only when `backend_heartbeat.json` is fresh (numeric timestamp no older than 15 seconds) and carries the expected simulation code and container process identity, and the web probes `/health/` over the internal host. The web port binds to the loopback address by default (`WEB_BIND_ADDRESS=127.0.0.1`), so the published port is reachable only from the host or a reverse proxy unless an operator deliberately sets `WEB_BIND_ADDRESS=0.0.0.0`. The web service starts only after the db is healthy, the backend is healthy, and the seed completed successfully. The backend is granted a 6-minute stop grace period, which gives its cooperative SIGTERM handling time to finish the current step and write a checkpoint.

The compose file requires explicit values for `DJANGO_CSRF_TRUSTED_ORIGINS` and `DJANGO_SECURE_SSL_REDIRECT` (no defaults), so a misconfigured `.env` fails fast instead of booting the web service with an unintended CSRF origin or an unintended SSL policy. The current app and db images report zero Trivy vulnerabilities, secrets, and misconfigurations, and the filesystem scan reports zero vulnerabilities for `uv.lock`, with the only note being the generic image-level health advisory: the single application image serves the one-shot `seed` job and the long-running `web` and `backend` roles, each covered by a role-specific Compose healthcheck. Exact container verification runs are recorded: a Compose stack with no external network access resumed from a committed checkpoint and became healthy; an invalid non-placeholder API key with `LLM_API_RETRIES=1` failed after exactly two attempts with a sanitized message and committed no checkpoint; and a placeholder API key failed provider validation before any runtime marker was published. These runs exercised failure paths only and used no live credentials, so this README does not claim a successful live LLM or browser session and does not claim a passing final external quality and security gate; that gate remains a separate release step.

### First-login and superuser account

Navigate to `http://localhost:8000/accounts/login/`, then create a superuser inside the running container:

```bash
docker compose -f docker-compose.prod.yml exec web python manage.py createsuperuser
```

Log in with those credentials to access simulation controls (`/simulator_home`, `/path_tester/`).

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

In addition, we thank Lindsay Popowski, Philip Guo, Michael Terry, and the Center for Advanced Study in the Behavioral Sciences (CASBS) community for their insights, discussions, and support. Lastly, all locations featured in Smallville are inspired by real-world locations that Joon has frequented as an undergraduate and graduate student---he thanks everyone there for feeding and supporting him all these years.
