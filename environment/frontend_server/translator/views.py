"""
Smallville frontend server views.

Production-hardened: CSRF enabled, operator auth required for simulation
control surfaces, input validation on sim_code/step.
"""

import datetime
import json
import os
import re
from os import listdir

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from global_methods import *


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

_VALID_SIM_CODE_RE = re.compile(r"^[\w._-]{1,256}$")
_VALID_STEP_RE = re.compile(r"^\d{1,10}$")
_VALID_PLAY_SPEED_RE = re.compile(r"^[1-6]$")

# Hard cap on demo step to prevent CPU-exhaustion DoS.  Even an extreme
# simulation won't exceed this many steps; the movement file will cap lower.
_MAX_DEMO_STEP = 100_000


def _validate_sim_code(sim_code: str) -> None:
    """Raise ValueError if sim_code contains path traversal or invalid chars."""
    if not _VALID_SIM_CODE_RE.match(sim_code) or ".." in sim_code:
        raise ValueError(f"Invalid sim_code: {sim_code!r}")


def _validate_step(step: str, max_step: int | None = None) -> int:
    """Validate and convert step to int. Raise ValueError on failure.

    If *max_step* is provided the step must not exceed it.
    """
    if not _VALID_STEP_RE.match(str(step)):
        raise ValueError(f"Invalid step: {step!r}")
    value = int(step)
    if max_step is not None and value > max_step:
        raise ValueError(f"Step {value} exceeds maximum {max_step}")
    return value


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def landing(request):
    context = {}
    template = "landing/landing.html"
    return render(request, template, context)


def demo(request, sim_code, step, play_speed="2"):
    _validate_sim_code(sim_code)
    step = _validate_step(step, max_step=_MAX_DEMO_STEP)
    if not _VALID_PLAY_SPEED_RE.match(str(play_speed)):
        play_speed = 2
    else:
        play_speed = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16, 6: 32}[int(play_speed)]

    move_file = f"compressed_storage/{sim_code}/master_movement.json"
    meta_file = f"compressed_storage/{sim_code}/meta.json"

    # Loading the basic meta information about the simulation.
    meta = {}
    with open(meta_file) as json_file:
        meta = json.load(json_file)

    # Loading the movement file
    raw_all_movement = {}
    with open(move_file) as json_file:
        raw_all_movement = json.load(json_file)

    # Clamp step to available movement keys so a huge user-supplied step
    # cannot spin a near-infinite loop before the frontend render.
    max_available = len(raw_all_movement) - 1
    if step > max_available:
        step = max_available

    sec_per_step = meta["sec_per_step"]
    start_datetime = datetime.datetime.strptime(
        meta["start_date"] + " 00:00:00", "%B %d, %Y %H:%M:%S"
    )
    for i in range(step):
        start_datetime += datetime.timedelta(seconds=sec_per_step)
    start_datetime = start_datetime.strftime("%Y-%m-%dT%H:%M:%S")

    # Loading all names of the personas
    persona_names = []
    persona_names_set = set()
    for p in list(raw_all_movement["0"].keys()):
        persona_names += [
            {
                "original": p,
                "underscore": p.replace(" ", "_"),
                "initial": p[0] + p.split(" ")[-1][0],
            }
        ]
        persona_names_set.add(p)

    all_movement = {}

    init_prep = {}
    for int_key in range(step + 1):
        key = str(int_key)
        val = raw_all_movement[key]
        for p in persona_names_set:
            if p in val:
                init_prep[p] = val[p]
    persona_init_pos = {}
    for p in persona_names_set:
        persona_init_pos[p.replace(" ", "_")] = init_prep[p]["movement"]
    all_movement[step] = init_prep

    for int_key in range(step + 1, len(raw_all_movement.keys())):
        all_movement[int_key] = raw_all_movement[str(int_key)]

    context = {
        "sim_code": sim_code,
        "step": step,
        "persona_names": persona_names,
        "persona_init_pos": persona_init_pos,
        "all_movement": all_movement,
        "start_datetime": start_datetime,
        "sec_per_step": sec_per_step,
        "play_speed": play_speed,
        "mode": "demo",
    }
    template = "demo/demo.html"
    return render(request, template, context)


def UIST_Demo(request):
    return demo(request, "March20_the_ville_n25_UIST_RUN-step-1-141", 2160, play_speed="3")


@login_required
def home(request):
    f_curr_sim_code = "temp_storage/curr_sim_code.json"
    f_curr_step = "temp_storage/curr_step.json"

    if not check_if_file_exists(f_curr_step):
        context = {}
        template = "home/error_start_backend.html"
        return render(request, template, context)

    with open(f_curr_sim_code) as json_file:
        sim_code = json.load(json_file)["sim_code"]

    with open(f_curr_step) as json_file:
        step = json.load(json_file)["step"]

    os.remove(f_curr_step)

    persona_names = []
    persona_names_set = set()
    for i in find_filenames(f"storage/{sim_code}/personas", ""):
        x = i.split("/")[-1].strip()
        if x[0] != ".":
            persona_names += [[x, x.replace(" ", "_")]]
            persona_names_set.add(x)

    persona_init_pos = []
    file_count = []
    for i in find_filenames(f"storage/{sim_code}/environment", ".json"):
        x = i.split("/")[-1].strip()
        if x[0] != ".":
            file_count += [int(x.split(".")[0])]
    curr_json = f"storage/{sim_code}/environment/{str(max(file_count))}.json"
    with open(curr_json) as json_file:
        persona_init_pos_dict = json.load(json_file)
        for key, val in persona_init_pos_dict.items():
            if key in persona_names_set:
                persona_init_pos += [[key, val["x"], val["y"]]]

    context = {
        "sim_code": sim_code,
        "step": step,
        "persona_names": persona_names,
        "persona_init_pos": persona_init_pos,
        "mode": "simulate",
    }
    template = "home/home.html"
    return render(request, template, context)


@login_required
def replay(request, sim_code, step):
    _validate_sim_code(sim_code)
    step = _validate_step(step)

    persona_names = []
    persona_names_set = set()
    for i in find_filenames(f"storage/{sim_code}/personas", ""):
        x = i.split("/")[-1].strip()
        if x[0] != ".":
            persona_names += [[x, x.replace(" ", "_")]]
            persona_names_set.add(x)

    persona_init_pos = []
    file_count = []
    for i in find_filenames(f"storage/{sim_code}/environment", ".json"):
        x = i.split("/")[-1].strip()
        if x[0] != ".":
            file_count += [int(x.split(".")[0])]
    curr_json = f"storage/{sim_code}/environment/{str(max(file_count))}.json"
    with open(curr_json) as json_file:
        persona_init_pos_dict = json.load(json_file)
        for key, val in persona_init_pos_dict.items():
            if key in persona_names_set:
                persona_init_pos += [[key, val["x"], val["y"]]]

    context = {
        "sim_code": sim_code,
        "step": step,
        "persona_names": persona_names,
        "persona_init_pos": persona_init_pos,
        "mode": "replay",
    }
    template = "home/home.html"
    return render(request, template, context)


@login_required
def replay_persona_state(request, sim_code, step, persona_name):
    _validate_sim_code(sim_code)
    step = _validate_step(step)

    persona_name_underscore = persona_name
    persona_name = " ".join(persona_name.split("_"))
    memory = f"storage/{sim_code}/personas/{persona_name}/bootstrap_memory"
    if not os.path.exists(memory):
        memory = (
            f"compressed_storage/{sim_code}/personas/{persona_name}/bootstrap_memory"
        )

    with open(memory + "/scratch.json") as json_file:
        scratch = json.load(json_file)

    with open(memory + "/spatial_memory.json") as json_file:
        spatial = json.load(json_file)

    with open(memory + "/associative_memory/nodes.json") as json_file:
        associative = json.load(json_file)

    a_mem_event = []
    a_mem_chat = []
    a_mem_thought = []

    for count in range(len(associative.keys()), 0, -1):
        node_id = f"node_{str(count)}"
        node_details = associative[node_id]

        if node_details["type"] == "event":
            a_mem_event += [node_details]
        elif node_details["type"] == "chat":
            a_mem_chat += [node_details]
        elif node_details["type"] == "thought":
            a_mem_thought += [node_details]

    context = {
        "sim_code": sim_code,
        "step": step,
        "persona_name": persona_name,
        "persona_name_underscore": persona_name_underscore,
        "scratch": scratch,
        "spatial": spatial,
        "a_mem_event": a_mem_event,
        "a_mem_chat": a_mem_chat,
        "a_mem_thought": a_mem_thought,
    }
    template = "persona_state/persona_state.html"
    return render(request, template, context)


@login_required
def path_tester(request):
    context = {}
    template = "path_tester/path_tester.html"
    return render(request, template, context)


@login_required
@require_POST
@csrf_protect
def process_environment(request):
    """
    <FRONTEND to BACKEND>
    Sends the frontend visual world information to the backend server.

    Requires operator authentication for production safety.
    """
    data = json.loads(request.body)
    step = data.get("step")
    sim_code = data.get("sim_code")
    environment = data.get("environment")

    if step is None or sim_code is None or environment is None:
        return JsonResponse({"error": "Missing required fields"}, status=400)

    _validate_sim_code(str(sim_code))
    _validate_step(str(step))

    dir_path = f"storage/{sim_code}/environment"
    os.makedirs(dir_path, exist_ok=True)

    file_path = f"{dir_path}/{step}.json"
    # Extra guard: ensure the resolved path stays within the storage tree
    if not os.path.realpath(file_path).startswith(
        os.path.realpath(f"storage/{sim_code}")
    ):
        return JsonResponse({"error": "Invalid path"}, status=400)

    with open(file_path, "w") as outfile:
        outfile.write(json.dumps(environment, indent=2))

    return HttpResponse("received")


@login_required
@require_POST
@csrf_protect
def update_environment(request):
    """
    <BACKEND to FRONTEND>
    Sends the backend computation of persona behavior to the frontend.
    """
    data = json.loads(request.body)
    step = data.get("step")
    sim_code = data.get("sim_code")

    if step is None or sim_code is None:
        return JsonResponse({"error": "Missing required fields"}, status=400)

    _validate_sim_code(str(sim_code))
    step_int = _validate_step(str(step))

    response_data = {"<step>": -1}
    movement_path = f"storage/{sim_code}/movement/{step_int}.json"
    if check_if_file_exists(movement_path):
        with open(movement_path) as json_file:
            response_data = json.load(json_file)
            response_data["<step>"] = step_int

    return JsonResponse(response_data)


@login_required
@require_POST
@csrf_protect
def path_tester_update(request):
    """
    Processing the path and saving it to path_tester_env.json temp storage.
    """
    data = json.loads(request.body)
    camera = data.get("camera")

    if camera is None:
        return JsonResponse({"error": "Missing camera data"}, status=400)

    # Sanitize camera coordinates
    if not isinstance(camera, dict) or "x" not in camera or "y" not in camera:
        return JsonResponse({"error": "Invalid camera format"}, status=400)

    try:
        int(camera["x"])
        int(camera["y"])
    except (ValueError, TypeError):
        return JsonResponse({"error": "Invalid camera coordinates"}, status=400)

    os.makedirs("temp_storage", exist_ok=True)
    with open("temp_storage/path_tester_env.json", "w") as outfile:
        outfile.write(json.dumps(camera, indent=2))

    return HttpResponse("received")


@require_GET
def health(request):
    """Simple health check endpoint."""
    return JsonResponse({"status": "ok"})
