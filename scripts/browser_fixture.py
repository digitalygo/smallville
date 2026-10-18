#!/usr/bin/env python3
"""Run the real local WebUI/backend against an isolated synthetic provider.

No provider network access or real credentials are used. Perception,
retrieval, reflection and planning are controlled fixtures; Persona.move,
path execution, the real maze, runtime locks, snapshots, heartbeat, CSRF,
Django endpoints and the browser renderer run normally.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from wsgiref.simple_server import WSGIRequestHandler, make_server

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "environment/frontend_server"
BACKEND = ROOT / "reverie/backend_server"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--playwright-dir",
        required=True,
        help="Directory containing the playwright Node package",
    )
    parser.add_argument("--screenshots", default=str(ROOT / "docs/verification"))
    args = parser.parse_args()
    screenshots = Path(args.screenshots).resolve()
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smallville-browser-") as directory:
        work = Path(directory)
        os.environ.update(
            DJANGO_SETTINGS_MODULE="frontend_server.settings",
            DJANGO_ENV="dev",
            DJANGO_DEBUG="true",
            DATABASE_URL="sqlite:///" + str(work / "browser.sqlite3"),
        )
        sys.path[:0] = [str(FRONTEND), str(BACKEND)]
        os.chdir(work)
        import django

        django.setup()
        from django.contrib.auth import get_user_model
        from django.contrib.staticfiles.handlers import StaticFilesHandler
        from django.core.management import call_command
        from django.core.wsgi import get_wsgi_application
        from django.test import Client
        from durability import liveness_lease
        from persona.persona import Persona
        from persona.prompt_template import gpt_structure as gpt

        import reverie

        call_command("migrate", verbosity=0, interactive=False)
        user = get_user_model().objects.create_user(
            username="disposable-browser-fixture"
        )
        client = Client()
        client.force_login(user)
        storage, runtime = work / "storage", work / "temp_storage"
        origin = "base_the_ville_isabella_maria_klaus"
        shutil.copytree(FRONTEND / "storage" / origin, storage / origin)
        env_file = storage / origin / "environment/0.json"
        env = json.loads(env_file.read_text())
        for index, record in enumerate(env.values()):
            record.update(x=72 + index * 2, y=35)
        env_file.write_text(json.dumps(env))
        stop = threading.Event()
        backend_errors = []
        request_starts = []

        def fake_completion(**kwargs):
            request_starts.append(time.time())
            prompt = json.loads(kwargs["messages"][-1]["content"])
            x, y = prompt["destination"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=f"<waiting> {x} {y}")
                    )
                ]
            )

        def fake_catalog():
            request_starts.append(time.time())
            return SimpleNamespace(data=[SimpleNamespace(id="Qwen3.8-27B")])

        fake_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=fake_completion)),
            models=SimpleNamespace(list=fake_catalog),
        )

        def fixture_client(provider):
            assert provider == "hetzner", "Unexpected provider fallback"
            return fake_client

        def synthetic_plan(persona, maze, personas, new_day, retrieved):
            x, y = persona.scratch.curr_tile
            options = [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
            target = next(
                (
                    tile
                    for tile in options
                    if 0 <= tile[0] < maze.maze_width
                    and 0 <= tile[1] < maze.maze_height
                    and maze.collision_maze[tile[1]][tile[0]]
                    != reverie.collision_block_id
                ),
                (x, y),
            )
            plan = gpt.ChatGPT_request(
                json.dumps({"resident": "synthetic", "destination": target}),
                persona=persona,
            )
            persona.scratch.act_path_set = False
            persona.scratch.act_address = plan
            persona.scratch.act_description = "walking through the village"
            persona.scratch.act_pronunciatio = "🚶"
            return plan

        class QuietHandler(WSGIRequestHandler):
            def log_message(self, *unused):
                pass

        with ExitStack() as stack:
            for target, field, value in (
                (reverie, "fs_storage", str(storage)),
                (reverie, "fs_temp_storage", str(runtime)),
                (reverie, "install_shutdown_handlers", lambda event: None),
                (reverie, "new_shutdown_event", lambda: stop),
                (reverie, "max_steps_per_second", 100),
                (Persona, "perceive", lambda *args: []),
                (Persona, "retrieve", lambda *args: {}),
                (Persona, "reflect", lambda *args: None),
                (Persona, "plan", synthetic_plan),
                (gpt, "_get_client", fixture_client),
                (gpt, "llm_provider", "hetzner"),
                (gpt, "llm_model", "Qwen3.8-27B"),
                (gpt, "llm_persona_models", {}),
                (gpt, "llm_reasoning_effort", None),
                (gpt, "hetzner_api_key", "synthetic-browser-only"),
                (gpt, "hetzner_rate_limit_dir", str(work / "limits")),
                (gpt, "hetzner_requests_per_minute", 10),
            ):
                stack.enter_context(patch.object(target, field, value))
            stack.enter_context(liveness_lease(str(runtime / "backend_alive.lock")))
            server = reverie.ReverieServer(origin, "browser-smoke")
            server.server_sleep = 0.02
            gpt.verify_hetzner_models({"Qwen3.8-27B"})
            server.publish_runtime_markers()

            def run_backend():
                try:
                    server.serve_forever(autosave_interval=2, heartbeat_interval=1)
                except Exception as error:  # noqa: BLE001
                    backend_errors.append(error)
                    stop.set()

            backend_thread = threading.Thread(target=run_backend, daemon=True)
            backend_thread.start()
            http = make_server(
                "127.0.0.1",
                0,
                StaticFilesHandler(get_wsgi_application()),
                handler_class=QuietHandler,
            )
            http_thread = threading.Thread(target=http.serve_forever, daemon=True)
            http_thread.start()
            config = {
                "url": f"http://127.0.0.1:{http.server_port}",
                "screenshots": str(screenshots),
                "cookies": [
                    {
                        "name": c.key,
                        "value": c.value,
                        "domain": "127.0.0.1",
                        "path": "/",
                    }
                    for c in client.cookies.values()
                ],
            }
            config_file = work / "browser.json"
            config_file.write_text(json.dumps(config))
            config_file.chmod(0o600)
            try:
                result = subprocess.run(
                    [
                        "node",
                        str(ROOT / "scripts/browser_smoke.cjs"),
                        str(config_file),
                        str(Path(args.playwright_dir).resolve()),
                    ],
                    check=False,
                    timeout=100,
                )
            finally:
                stop.set()
                backend_thread.join(30)
                http.shutdown()
                http.server_close()
                http_thread.join(5)
            if backend_thread.is_alive():
                raise RuntimeError("Fixture backend failed to stop")
            if backend_errors:
                raise RuntimeError("Fixture backend failed") from backend_errors[0]
            if result.returncode:
                raise SystemExit(result.returncode)
            report = {
                "backend_step": server.step,
                "simulation_time": str(server.curr_time),
                "movement_files": len(
                    list((storage / "browser-smoke/movement").glob("*.json"))
                ),
                "provider_activity": gpt.request_activity(),
                "provider_network_used": False,
                "minimum_request_spacing_seconds": min(
                    b - a for a, b in pairwise(request_starts)
                ),
                "request_starts": request_starts,
            }
            (screenshots / "browser-backend-evidence.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            assert server.step >= 2, report
            assert report["minimum_request_spacing_seconds"] >= 6.1, report
            print(json.dumps(report))


if __name__ == "__main__":
    main()
