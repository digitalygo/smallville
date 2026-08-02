"""
Focused unit tests for memory-structure durability.

Covers:

  1. Context-managed UTF-8 JSON reads on the spatial, associative, and scratch
     load paths emit no ``ResourceWarning``.
  2. Interrupted atomic writes preserve prior content for representative
     persona state files (``spatial_memory.json``, ``scratch.json``, and the
     ``associative_memory`` group).
  3. All memory save outputs remain loadable and roundtrip, including the
     associative output directory being created when missing.

The associative memory group is three files written sequentially. Each file
is published atomically, but the group as a whole is not atomic: an
interruption between files leaves a mixed group.

These tests never touch the network and confine all file I/O to temporary
directories.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import unittest
import warnings
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import durability  # noqa: E402
from persona.memory_structures.associative_memory import AssociativeMemory  # noqa: E402
from persona.memory_structures.scratch import Scratch  # noqa: E402
from persona.memory_structures.spatial_memory import MemoryTree  # noqa: E402


def _load_no_warning(loader):
    """Run ``loader`` and fail if any ``ResourceWarning`` is emitted.

    An unclosed file surfaces as a ``ResourceWarning`` during deallocation,
    so capture warnings and treat any as a test failure.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        result = loader()
    resource_warnings = [
        w for w in caught if issubclass(w.category, ResourceWarning)
    ]
    if resource_warnings:
        raise AssertionError(
            "unclosed file warning on load: "
            + "; ".join(str(w.message) for w in resource_warnings)
        )
    return result


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _scratch_dict():
    """A full scratch payload mirroring the seeded bootstrap scratch.json."""
    return {
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


def _write_associative(dir_path, embeddings=None, nodes=None, kw_strength=None):
    """Write a minimal associative memory directory."""
    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "embeddings.json"), "w") as f:
        json.dump(embeddings if embeddings is not None else {}, f)
    with open(os.path.join(dir_path, "nodes.json"), "w") as f:
        json.dump(nodes if nodes is not None else {}, f)
    with open(os.path.join(dir_path, "kw_strength.json"), "w") as f:
        json.dump(
            kw_strength
            if kw_strength is not None
            else {"kw_strength_event": {}, "kw_strength_thought": {}},
            f,
        )


def _build_associative(root):
    """Create an AssociativeMemory with one event and one thought node."""
    src = os.path.join(root, "src")
    _write_associative(src)
    mem = _load_no_warning(lambda: AssociativeMemory(src))
    created = datetime.datetime(2023, 2, 13, 8, 0, 0)
    mem.add_event(
        created,
        None,
        "Isabella Rodriguez",
        "is",
        "idle",
        "Isabella Rodriguez is idle",
        {"isabella", "idle"},
        5,
        ("emb_1", [0.1, 0.2]),
        [],
    )
    mem.add_thought(
        created,
        None,
        "Isabella Rodriguez",
        "wants",
        "coffee",
        "Isabella Rodriguez wants coffee",
        {"coffee"},
        7,
        ("emb_2", [0.3, 0.4]),
        [],
    )
    return mem


class NoResourceWarningLoadTests(unittest.TestCase):
    """Spatial, associative, and scratch load paths close their files."""

    def test_spatial_load_no_resource_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spatial_memory.json")
            with open(path, "w") as f:
                json.dump({"the Ville": {"main": ["bed"]}}, f)
            tree = _load_no_warning(lambda: MemoryTree(path))
        self.assertEqual(tree.tree["the Ville"]["main"], ["bed"])

    def test_associative_load_no_resource_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            dir_path = os.path.join(tmp, "associative_memory")
            _write_associative(dir_path)
            mem = _load_no_warning(lambda: AssociativeMemory(dir_path))
        self.assertEqual(mem.id_to_node, {})

    def test_scratch_load_no_resource_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scratch.json")
            with open(path, "w") as f:
                json.dump(_scratch_dict(), f)
            scratch = _load_no_warning(lambda: Scratch(path))
        self.assertEqual(scratch.name, "Isabella Rodriguez")


class SymlinkRefusalLoadTests(unittest.TestCase):
    """Memory loads refuse symlinked inputs through the safe reader.

    A symlink swapped in at any memory file path, dangling or not, raises
    ``OSError`` instead of being followed, and the refused load closes its
    descriptor without a ``ResourceWarning``.
    """

    def _assert_refuses(self, loader):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with self.assertRaises(OSError):
                loader()
        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        if resource_warnings:
            raise AssertionError(
                "unclosed file warning on refused load: "
                + "; ".join(str(w.message) for w in resource_warnings)
            )

    def test_spatial_load_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real_spatial.json")
            with open(real, "w") as f:
                json.dump({"the Ville": {"main": ["bed"]}}, f)
            link = os.path.join(tmp, "spatial_memory.json")
            os.symlink(real, link)
            self._assert_refuses(lambda: MemoryTree(link))

    def test_spatial_load_refuses_swap_to_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spatial_memory.json")
            with open(path, "w") as f:
                json.dump({"the Ville": {"main": ["bed"]}}, f)
            tree = _load_no_warning(lambda: MemoryTree(path))
            self.assertEqual(tree.tree["the Ville"]["main"], ["bed"])
            decoy = os.path.join(tmp, "decoy.json")
            with open(decoy, "w") as f:
                json.dump({"evil": True}, f)
            os.remove(path)
            os.symlink(decoy, path)
            self._assert_refuses(lambda: MemoryTree(path))

    def test_scratch_load_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real_scratch.json")
            with open(real, "w") as f:
                json.dump(_scratch_dict(), f)
            link = os.path.join(tmp, "scratch.json")
            os.symlink(real, link)
            self._assert_refuses(lambda: Scratch(link))

    def test_scratch_load_refuses_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "scratch.json")
            os.symlink(os.path.join(tmp, "missing_scratch.json"), link)
            self._assert_refuses(lambda: Scratch(link))

    def test_associative_load_refuses_symlink_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            dir_path = os.path.join(tmp, "associative_memory")
            _write_associative(dir_path)
            mem = _load_no_warning(lambda: AssociativeMemory(dir_path))
            self.assertEqual(mem.id_to_node, {})
            decoy = os.path.join(tmp, "decoy_nodes.json")
            with open(decoy, "w") as f:
                json.dump({}, f)
            os.remove(os.path.join(dir_path, "nodes.json"))
            os.symlink(decoy, os.path.join(dir_path, "nodes.json"))
            self._assert_refuses(lambda: AssociativeMemory(dir_path))


class MemorySaveRoundtripTests(unittest.TestCase):
    """Every memory save output stays loadable and roundtrips."""

    def test_spatial_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spatial_memory.json")
            tree = MemoryTree("/nonexistent/spatial_memory.json")
            tree.tree = {"the Ville": {"main": {"bedroom": ["bed", "lamp"]}}}
            tree.save(path)
            loaded = _load_no_warning(lambda: MemoryTree(path))
        self.assertEqual(loaded.tree, tree.tree)

    def test_scratch_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scratch.json")
            scratch = Scratch("/nonexistent/scratch.json")
            scratch.curr_time = datetime.datetime(2023, 2, 13, 12, 45, 30)
            scratch.act_start_time = datetime.datetime(2023, 2, 13, 12, 0, 0)
            scratch.save(path)
            loaded = _load_no_warning(lambda: Scratch(path))
        self.assertEqual(
            loaded.curr_time, datetime.datetime(2023, 2, 13, 12, 45, 30)
        )
        self.assertEqual(
            loaded.act_start_time, datetime.datetime(2023, 2, 13, 12, 0, 0)
        )

    def test_associative_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build_associative(tmp)
            out = os.path.join(tmp, "out")
            mem.save(out)
            loaded = _load_no_warning(lambda: AssociativeMemory(out))
        self.assertEqual(len(loaded.id_to_node), 2)
        self.assertEqual(loaded.embeddings["emb_1"], [0.1, 0.2])
        self.assertEqual(loaded.embeddings["emb_2"], [0.3, 0.4])
        self.assertEqual(
            loaded.kw_strength_thought, {"coffee": 1}
        )

    def test_associative_save_creates_output_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = _build_associative(tmp)
            out = os.path.join(tmp, "nested", "associative_memory")
            mem.save(out)
            self.assertTrue(os.path.isfile(os.path.join(out, "nodes.json")))
            self.assertTrue(
                os.path.isfile(os.path.join(out, "embeddings.json"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(out, "kw_strength.json"))
            )


class InterruptedWritePreservesPriorTests(unittest.TestCase):
    """An interrupted atomic write leaves prior persona state readable."""

    def _interrupt(self):
        return mock.patch.object(
            durability.os, "replace", side_effect=OSError("boom")
        )

    def test_spatial_save_interrupted_preserves_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "spatial_memory.json")
            prior = {"the Ville": {"main": {"bedroom": ["bed"]}}}
            with open(path, "w") as f:
                json.dump(prior, f)
            tree = MemoryTree(path)
            tree.tree = {"the Ville": {"main": {"kitchen": ["stove"]}}}
            with self._interrupt():
                with self.assertRaises(OSError):
                    tree.save(path)
            self.assertEqual(_read_json(path), prior)

    def test_scratch_save_interrupted_preserves_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scratch.json")
            prior = _scratch_dict()
            with open(path, "w") as f:
                json.dump(prior, f)
            scratch = Scratch(path)
            scratch.name = "Changed Name"
            with self._interrupt():
                with self.assertRaises(OSError):
                    scratch.save(path)
            self.assertEqual(_read_json(path), prior)

    def test_associative_save_interrupted_preserves_prior_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "associative_memory")
            mem = _build_associative(tmp)
            mem.save(out)
            prior_nodes = _read_json(os.path.join(out, "nodes.json"))
            prior_embeddings = _read_json(os.path.join(out, "embeddings.json"))
            prior_kw = _read_json(os.path.join(out, "kw_strength.json"))

            loaded = _load_no_warning(lambda: AssociativeMemory(out))
            loaded.embeddings["emb_new"] = [9.9]
            with self._interrupt():
                with self.assertRaises(OSError):
                    loaded.save(out)

            self.assertEqual(
                _read_json(os.path.join(out, "nodes.json")), prior_nodes
            )
            self.assertEqual(
                _read_json(os.path.join(out, "embeddings.json")),
                prior_embeddings,
            )
            self.assertEqual(
                _read_json(os.path.join(out, "kw_strength.json")), prior_kw
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
