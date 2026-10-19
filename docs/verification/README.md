# WebUI and Hetzner verification

Verification ran in an isolated checkout of `digitalygo/smallville`, starting at `a77ef3284acfb28608046982b8fb593192cc7e6e`, on 2026-10-18, then extended for the selected Gemini embedding route on 2026-10-19. No deployment, production data or existing operator configuration was changed.

## Reproduced failures

The original live/replay renderer used an external Misa atlas for every resident. The browser stopped at step 0 with `Cannot read properties of undefined (reading 'duration')` while playing that animation; activity remained empty. The canvas was 1500px wide in a 1440px viewport. Its movement setting also moved one complete 32px tile in one update, making motion effectively instantaneous when assets did load. See [original desktop screenshot](webui-before-desktop.png).

An unknown destination in the actual `execute()` path left `target_tiles` unset, raising instead of producing movement. The regression test exercises the real path finder and consumes successive route tiles, as well as unknown/empty destinations that now stay at the current tile.

The documented development command also produced 404s for local static files when `DJANGO_DEBUG` was unset. The README now explicitly enables it for local `runserver`; production still uses its existing static collection and serving configuration.

## Browser and backend evidence

Run from the repository root after `uv sync --frozen`:

```bash
npm_config_cache=/tmp/smallville-npm-cache npm install --prefix /tmp/smallville-browser playwright
uv run python scripts/browser_fixture.py --playwright-dir /tmp/smallville-browser/node_modules
```

Chromium must be installed; set `CHROMIUM_PATH` if its executable is not `/usr/bin/chromium`. The fixture uses a disposable temporary SQLite database, a local user with no usable password and temporary simulation storage. It removes them on exit. Nothing is written into a live simulation. A separate HTTP port is selected automatically.

The fixture mocks perception, retrieval, reflection and planning with synthetic OpenAI-compatible chat and embedding clients. It still runs the actual `ReverieServer.serve_forever()`, `Persona.move()`, real maze/path execution, immutable environment snapshots, movement output, step/time advancement, checkpoint saves, liveness lease, heartbeat, CSRF, Django endpoints and Phaser renderer. The synthetic planner sends only a fictional resident and destination tile through the normal Hetzner request wrapper. It embeds synthetic event/query text through the real embedding wrapper, stores 3072-dimension Gemini provenance, computes actual relevance, and verifies the resulting checkpoint on resume. No API network request is made; the fake clients accept only the expected Hetzner chat/catalog and OpenRouter Gemini embedding routes. This proves the state and rendering pipeline; it does not prove the full cognitive prompt chain or Qwen behavior.

The checked-in [browser evidence](browser-evidence.json) records initial tiles, unchanged logical tiles during interpolation, committed tiles at step 2, activity text, a successful real movement endpoint response, working pause and viewports of 1440px, 390px and 320px. There were zero browser exceptions, failed application responses or external asset requests. The [backend evidence](browser-backend-evidence.json) records three completed steps, 30 simulated seconds, three movement files, ten synthetic Hetzner attempts and twelve synthetic OpenRouter embedding attempts. Minimum measured spacing exceeded 6.1 seconds, using the actual production gate interval. The browser pauses the view at step 2; already-started backend planning finishes step 3 during cooperative shutdown.

| State | Screenshot |
| --- | --- |
| Live planning and shared provider queue | [Queued](webui-live-queued.png) |
| Following a resident during a step, paused | [Follow](webui-live-follow.png) |
| Committed state and whole-town view | [Desktop](webui-live-desktop.png) |
| Responsive resident activity and map controls | [Mobile](webui-live-mobile.png) |

## Automated checks

```bash
uv run python -m unittest discover -s reverie/backend_server -p 'test_*.py'
cd environment/frontend_server
uv run python manage.py test --noinput
```

The backend suite covers real route progression; exact Hetzner endpoint/model routing; explicit embedding separation; startup catalog refusal; bounded retries; malformed responses; shared numeric/date `Retry-After`; conservative 60-second cooldown on 429 without a usable header; no burst after idle; key isolation; real concurrent processes; corrupt/non-regular gate refusal; and the existing lifecycle, persistence, grounding, privacy and configuration checks. Django/Node checks cover actual interpolation, pause/resume, atomic multi-resident tile commits before the next environment POST, pending/wrong-step refusal, reduced motion, expired-session recovery, terminal-step safety and authenticated progress field filtering. The complete suites passed: **598 backend tests** and **332 Django tests**. The 19 targeted Django/Node status and movement checks also passed after the final presentation adjustments. Ruff passed on the new Python modules and scripts, and `git diff --check` passed.

## Live verification still required

`OPENROUTER_API_KEY` was present and `HETZNER_API_KEY` was absent from the execution environment. No token was retrieved from chat, copied into files or logs, or sent to another service. Authenticated model availability and a real Qwen completion remain unverified. The [official model list](https://docs.hetzner.com/general/company-and-policy/experiments/inference/) documents `Qwen3.8-27B`; an authenticated `/models` response is definitive.

After the operator explicitly approves provider use and securely provisions `HETZNER_API_KEY` for `inference.hetzner.com`, this opt-in probe uses the same persistent limiter and prints only sanitized verification metadata:

```bash
uv run python scripts/check_hetzner.py          # catalog request only
uv run python scripts/check_hetzner.py --chat   # catalog plus one synthetic chat request
```

The embedding selection is now explicit: OpenRouter `google/gemini-embedding-2`, 3072 dimensions; `.env.example` configures it alongside Hetzner Qwen chat. The selected OpenRouter model passed a **real synthetic embedding probe** using the existing environment key: 3072 finite float values, norm 1.0, elapsed 2.832 seconds. [Recorded live result](live-gemini-embedding-evidence.json). No vectors, key or prompt contents are printed. This probe’s individual HTTP attempt count was not recorded; the repeatable script now includes it. This result does not verify a real Qwen completion or combined simulation.

The mock browser test now exercises embedding requests, event storage, cosine retrieval, metadata persistence and checkpoint resume. Its ten Hetzner attempts and twelve OpenRouter attempts are **mocked**, with real local dispatch timing; they must not be described as live provider throughput. The Hetzner shared 10 RPM gate is unchanged and applies only to Hetzner-key requests.

A complete real-provider simulation still needs the securely configured `HETZNER_API_KEY`. No fallback chat provider is enabled. Existing nonempty untagged memories require verified `LLM_LEGACY_EMBEDDING_SPACE` before use; same-length vectors from another model are refused. Empty bundled seeds are safe. The new versioned sidecar preserves exact provider/model/dimensions through saves, resumes and forks. Loading/probing never rewrites source memories, and no automatic re-embedding, deletion, padding or truncation occurs. See the [memory compatibility procedure](../../README.md#gemini-embedding-2-and-saved-memory-compatibility).

The request gate covers this app's threads/processes sharing one persistent gate directory. Compose uses the storage volume. Separate directories, unrelated applications, or machines without shared `flock` semantics and synchronized clocks cannot share the same guarantee. The token limits are still provider-enforced, and experimental performance/availability can vary. First-day planning still uses the original long prompt chain and may take tens of minutes or longer at 10 RPM.
