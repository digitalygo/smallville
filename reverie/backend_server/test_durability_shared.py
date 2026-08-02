"""
Shared helpers for the backend durability test modules.

This module defines no test cases. It owns the backend sys.path setup, the
canonical runtime marker names, the small in-memory fakes for ``Maze`` and
``Persona``, the minimal meta/environment fixtures, and the simulation
folder writers used across the split durability test modules.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import collections
import json
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import durability  # noqa: E402
import reverie  # noqa: E402
from persona.prompt_template import gpt_structure  # noqa: E402

_MARKER_NAMES = (
    "backend_heartbeat.json",
    "curr_step.json",
    "curr_sim_code.json",
)


def _write_stale_markers(temp_storage):
    """Write stale runtime markers into ``temp_storage``."""
    for name in _MARKER_NAMES:
        with open(os.path.join(temp_storage, name), "w") as f:
            json.dump({"stale": True}, f)


class _FakeScratch:
    """Minimal scratch stand-in exposing only what ``__init__`` touches."""

    def get_curr_event_and_desc(self):
        return ("event", "desc")


class _FakePersona:
    """Minimal persona stand-in: only ``name`` and ``scratch`` are used."""

    def __init__(self, name, folder):
        self.name = name
        self.scratch = _FakeScratch()


class _StepScratch:
    """Scratch stand-in exposing the fields the serve loop reads per step."""

    def __init__(self):
        self.chat = None
        self.planned_path = None

    def get_curr_event_and_desc(self):
        return ("event", "desc")

    def get_curr_obj_event_and_desc(self):
        return ("obj_event", "obj_desc")


class _StepPersona:
    """Persona fake that can move and exposes the scratch fields."""

    def __init__(self, name, folder):
        self.name = name
        self.scratch = _StepScratch()

    def move(self, maze, personas, tile, curr_time):
        return (tile, "pronunciatio", "description")


class _FakeMaze:
    """Minimal maze stand-in with real bounds for validation."""

    def __init__(self, maze_name):
        self.maze_name = maze_name
        self.sq_tile_size = 1
        self.maze_width = 70
        self.maze_height = 40
        self.tiles = collections.defaultdict(
            lambda: collections.defaultdict(lambda: {"events": set()})
        )

    def turn_event_from_tile_idle(self, key, val):
        pass

    def remove_subject_events_from_tile(self, name, tile):
        pass

    def add_event_from_tile(self, event, tile):
        pass

    def remove_event_from_tile(self, event, tile):
        pass


_META = {
    "fork_sim_code": "origin",
    "start_date": "February 13, 2023",
    "curr_time": "February 13, 2023, 00:00:00",
    "sec_per_step": 10,
    "maze_name": "the_ville",
    "persona_names": ["Isabella Rodriguez"],
    "step": 0,
}

_ENV_0 = {"Isabella Rodriguez": {"x": 0, "y": 0}}


def _write_sim(root, sim_code, meta=None, env=None):
    """Create a minimal simulation folder (meta + env + complete personas)."""
    meta = meta if meta is not None else _META
    sim_dir = os.path.join(root, sim_code)
    os.makedirs(os.path.join(sim_dir, "reverie"))
    os.makedirs(os.path.join(sim_dir, "environment"))
    with open(os.path.join(sim_dir, "reverie", "meta.json"), "w") as f:
        json.dump(meta, f)
    with open(os.path.join(sim_dir, "environment", "0.json"), "w") as f:
        json.dump(env if env is not None else _ENV_0, f)
    for name in meta["persona_names"]:
        _write_seeded_persona(sim_dir, name)
    return sim_dir


def _write_env(root, sim_code, step, env):
    """Write a single environment snapshot for ``sim_code`` at ``step``."""
    env_dir = os.path.join(root, sim_code, "environment")
    os.makedirs(env_dir, exist_ok=True)
    with open(os.path.join(env_dir, f"{step}.json"), "w") as f:
        json.dump(env, f)


_SEEDED_SCRATCH = {
    "vision_r": 8,
    "att_bandwidth": 8,
    "retention": 8,
    "curr_time": None,
    "curr_tile": None,
    "daily_plan_req": "Isabella Rodriguez opens Hobbs Cafe at 8am.",
    "name": "Isabella Rodriguez",
    "first_name": "Isabella",
    "last_name": "Rodriguez",
    "age": 34,
    "innate": "friendly, outgoing, hospitable",
    "learned": "Isabella Rodriguez is a cafe owner of Hobbs Cafe.",
    "currently": "Isabella Rodriguez is planning a party.",
    "lifestyle": "Isabella Rodriguez goes to bed around 11pm.",
    "living_area": "the Ville:Isabella Rodriguez's apartment:main room",
    "concept_forget": 100,
    "daily_reflection_time": 180,
    "daily_reflection_size": 5,
    "overlap_reflect_th": 4,
    "kw_strg_event_reflect_th": 10,
    "kw_strg_thought_reflect_th": 9,
    "recency_w": 1,
    "relevance_w": 1,
    "importance_w": 1,
    "recency_decay": 0.995,
    "importance_trigger_max": 150,
    "importance_trigger_curr": 150,
    "importance_ele_n": 0,
    "thought_count": 5,
    "daily_req": [],
    "f_daily_schedule": [],
    "f_daily_schedule_hourly_org": [],
    "act_address": None,
    "act_start_time": None,
    "act_duration": None,
    "act_description": None,
    "act_pronunciatio": None,
    "act_event": ["Isabella Rodriguez", None, None],
    "act_obj_description": None,
    "act_obj_pronunciatio": None,
    "act_obj_event": [None, None, None],
    "chatting_with": None,
    "chat": None,
    "chatting_with_buffer": {},
    "chatting_end_time": None,
    "act_path_set": False,
    "planned_path": [],
}


def _write_seeded_persona(sim_dir, name):
    """Write a realistic persona bootstrap with null scratch timestamps."""
    persona_dir = os.path.join(sim_dir, "personas", name, "bootstrap_memory")
    os.makedirs(os.path.join(persona_dir, "associative_memory"), exist_ok=True)
    with open(os.path.join(persona_dir, "scratch.json"), "w") as f:
        json.dump(_SEEDED_SCRATCH, f)
    with open(os.path.join(persona_dir, "spatial_memory.json"), "w") as f:
        json.dump({}, f)
    with open(
        os.path.join(persona_dir, "associative_memory", "embeddings.json"), "w"
    ) as f:
        json.dump({}, f)
    with open(
        os.path.join(persona_dir, "associative_memory", "nodes.json"), "w"
    ) as f:
        json.dump({}, f)
    with open(
        os.path.join(persona_dir, "associative_memory", "kw_strength.json"), "w"
    ) as f:
        json.dump({"kw_strength_event": {}, "kw_strength_thought": {}}, f)


