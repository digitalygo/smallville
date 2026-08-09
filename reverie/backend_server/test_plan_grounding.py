"""
Offline unit tests for spatial grounding of model-generated location choices.

Covers the centralized canonical-choice helper, grounded sector, arena,
and game object generation with mocked LLM prompt functions, hardened
MemoryTree accessors, and the grounded action address stored by
``_determine_action``. No network is used.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import datetime
import os
import sys
import unittest
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from persona.cognitive_modules import plan  # noqa: E402
from persona.memory_structures.scratch import Scratch  # noqa: E402
from persona.memory_structures.spatial_memory import MemoryTree  # noqa: E402

_FALLBACK = "<FALLBACK>"

_TREE = {
    "the Ville": {
        "Hobbs Cafe": {
            "cafe": ["refrigerator", "cafe customer seating", "piano"],
        },
        "Isabella Rodriguez's apartment": {
            "main room": ["bed", "desk", "shelf"],
            "kitchen": ["stove", "sink"],
        },
        "The Rose and Crown Pub": {
            "pub": ["microphone", "bar customer seating"],
        },
    },
}


def _memory_tree():
    tree = MemoryTree("")
    tree.tree = _TREE
    return tree


class _FakeMaze:
    def __init__(self, tile):
        self._tile = tile

    def access_tile(self, tile):
        return self._tile


class _FakeScratchTile:
    def __init__(self, tile):
        self.curr_tile = tile


class _FakePersona:
    def __init__(self, scratch, s_mem):
        self.scratch = scratch
        self.s_mem = s_mem


class CanonicalChoiceTests(unittest.TestCase):
    CHOICES = ["main room", "kitchen", "cafe", "cafe customer seating"]

    def test_exact_match(self):
        self.assertEqual(
            plan.canonical_choice("main room", self.CHOICES, _FALLBACK),
            "main room")

    def test_case_insensitive_match(self):
        self.assertEqual(
            plan.canonical_choice("MAIN ROOM", self.CHOICES, _FALLBACK),
            "main room")
        self.assertEqual(
            plan.canonical_choice("Main Room", self.CHOICES, _FALLBACK),
            "main room")

    def test_braces_and_quotes_trimmed(self):
        self.assertEqual(
            plan.canonical_choice("'{main room", self.CHOICES, _FALLBACK),
            "main room")
        self.assertEqual(
            plan.canonical_choice('"main room"', self.CHOICES, _FALLBACK),
            "main room")
        self.assertEqual(
            plan.canonical_choice("`main room`", self.CHOICES, _FALLBACK),
            "main room")
        self.assertEqual(
            plan.canonical_choice("[main room]", self.CHOICES, _FALLBACK),
            "main room")

    def test_punctuation_trimmed(self):
        self.assertEqual(
            plan.canonical_choice("main room,", self.CHOICES, _FALLBACK),
            "main room")
        self.assertEqual(
            plan.canonical_choice("main room.", self.CHOICES, _FALLBACK),
            "main room")
        self.assertEqual(
            plan.canonical_choice("main room!", self.CHOICES, _FALLBACK),
            "main room")

    def test_label_suffix_and_prefix(self):
        self.assertEqual(
            plan.canonical_choice("main room (sector)", self.CHOICES, _FALLBACK),
            "main room")
        self.assertEqual(
            plan.canonical_choice("Sector: main room", self.CHOICES, _FALLBACK),
            "main room")

    def test_extra_colon_path_components(self):
        self.assertEqual(
            plan.canonical_choice(
                "the Ville:Isabella Rodriguez's apartment:main room",
                self.CHOICES, _FALLBACK),
            "main room")
        self.assertEqual(
            plan.canonical_choice(
                "Hobbs Cafe:cafe",
                ["Hobbs Cafe", "The Rose and Crown Pub"], _FALLBACK),
            "Hobbs Cafe")

    def test_unique_contained_choice_accepted(self):
        self.assertEqual(
            plan.canonical_choice("go to the cafe area", self.CHOICES, _FALLBACK),
            "cafe")

    def test_ambiguous_contained_choices_return_fallback(self):
        self.assertEqual(
            plan.canonical_choice("cafe kitchen", ["cafe", "kitchen"], _FALLBACK),
            _FALLBACK)

    def test_unknown_returns_fallback(self):
        self.assertEqual(
            plan.canonical_choice("moon base", self.CHOICES, _FALLBACK),
            _FALLBACK)

    def test_none_and_non_string_candidate(self):
        self.assertEqual(
            plan.canonical_choice(None, self.CHOICES, _FALLBACK), _FALLBACK)
        self.assertEqual(
            plan.canonical_choice(42, self.CHOICES, _FALLBACK), _FALLBACK)

    def test_empty_or_malformed_choices(self):
        self.assertEqual(
            plan.canonical_choice("main room", [], _FALLBACK), _FALLBACK)
        self.assertEqual(
            plan.canonical_choice("main room", None, _FALLBACK), _FALLBACK)
        self.assertEqual(
            plan.canonical_choice("main room", ["main room", 42], _FALLBACK),
            _FALLBACK)

    def test_exact_wins_over_contained(self):
        self.assertEqual(
            plan.canonical_choice(
                "cafe", ["cafe", "cafe customer seating"], _FALLBACK),
            "cafe")
        self.assertEqual(
            plan.canonical_choice(
                "cafe customer seating", ["cafe", "cafe customer seating"],
                _FALLBACK),
            "cafe customer seating")

    def test_deterministic_fallback_passthrough(self):
        self.assertEqual(plan.canonical_choice("junk", ["a"], "x"), "x")


class SectorGroundingTests(unittest.TestCase):
    def setUp(self):
        self.persona = _FakePersona(
            _FakeScratchTile((1, 1)), _memory_tree())
        self.maze = _FakeMaze({
            "world": "the Ville",
            "sector": "Isabella Rodriguez's apartment",
            "arena": "main room",
            "game_object": "bed",
        })

    def test_model_sector_resolves_to_real_sector(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_sector",
                               return_value=("Hobbs Cafe", [])):
            self.assertEqual(
                plan.generate_action_sector("sleeping", self.persona, self.maze),
                "Hobbs Cafe")

    def test_invalid_model_sector_falls_back_to_current_tile_sector(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_sector",
                               return_value=("moon base", [])):
            self.assertEqual(
                plan.generate_action_sector("sleeping", self.persona, self.maze),
                "Isabella Rodriguez's apartment")

    def test_non_string_model_sector_uses_fallback(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_sector",
                               return_value=(None, [])):
            self.assertEqual(
                plan.generate_action_sector("sleeping", self.persona, self.maze),
                "Isabella Rodriguez's apartment")

    def test_invalid_tile_sector_uses_first_deterministic_sector(self):
        maze = _FakeMaze({
            "world": "the Ville",
            "sector": "nowhere",
            "arena": "main room",
            "game_object": "bed",
        })
        with mock.patch.object(plan, "run_gpt_prompt_action_sector",
                               return_value=("moon base", [])):
            self.assertEqual(
                plan.generate_action_sector("sleeping", self.persona, maze),
                "Hobbs Cafe")

    def test_model_sector_from_other_world_is_not_accepted(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_sector",
                               return_value=("Other World", [])):
            self.assertEqual(
                plan.generate_action_sector("sleeping", self.persona, self.maze),
                "Isabella Rodriguez's apartment")

    def test_empty_world_returns_empty_string_without_raising(self):
        maze = _FakeMaze({
            "world": "nowhere",
            "sector": "nothing",
            "arena": "void",
            "game_object": None,
        })
        with mock.patch.object(plan, "run_gpt_prompt_action_sector",
                               return_value=("moon base", [])):
            self.assertEqual(
                plan.generate_action_sector("sleeping", self.persona, maze), "")


class ArenaGroundingTests(unittest.TestCase):
    def setUp(self):
        self.persona = _FakePersona(
            _FakeScratchTile((1, 1)), _memory_tree())
        self.maze = _FakeMaze({
            "world": "the Ville",
            "sector": "Isabella Rodriguez's apartment",
            "arena": "main room",
            "game_object": "bed",
        })

    def test_model_arena_resolves_under_canonical_world_sector(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_arena",
                               return_value=("cafe", [])):
            self.assertEqual(
                plan.generate_action_arena(
                    "sleeping", self.persona, self.maze, "the Ville", "Hobbs Cafe"),
                "cafe")

    def test_model_arena_case_and_braces_canonicalized(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_arena",
                               return_value=("'{CAFE'", [])):
            self.assertEqual(
                plan.generate_action_arena(
                    "sleeping", self.persona, self.maze, "the Ville", "Hobbs Cafe"),
                "cafe")

    def test_invalid_model_arena_falls_back_to_current_arena_in_same_world_sector(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_arena",
                               return_value=("moon base", [])):
            self.assertEqual(
                plan.generate_action_arena(
                    "sleeping", self.persona, self.maze,
                    "the Ville", "Isabella Rodriguez's apartment"),
                "main room")

    def test_invalid_model_arena_uses_first_deterministic_arena_elsewhere(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_arena",
                               return_value=("moon base", [])):
            self.assertEqual(
                plan.generate_action_arena(
                    "sleeping", self.persona, self.maze, "the Ville", "Hobbs Cafe"),
                "cafe")

    def test_model_arena_from_other_sector_is_not_accepted(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_arena",
                               return_value=("kitchen", [])):
            self.assertEqual(
                plan.generate_action_arena(
                    "sleeping", self.persona, self.maze, "the Ville", "Hobbs Cafe"),
                "cafe")

    def test_unknown_world_sector_grounds_to_current_tile_pair(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_arena",
                               return_value=("moon base", [])):
            self.assertEqual(
                plan.generate_action_arena(
                    "sleeping", self.persona, self.maze, "nowhere", "nothing"),
                "main room")

    def test_fully_unknown_world_returns_empty_string_without_raising(self):
        maze = _FakeMaze({
            "world": "nowhere",
            "sector": "nothing",
            "arena": "void",
            "game_object": None,
        })
        with mock.patch.object(plan, "run_gpt_prompt_action_arena",
                               return_value=("moon base", [])):
            self.assertEqual(
                plan.generate_action_arena(
                    "sleeping", self.persona, maze, "nowhere", "nothing"),
                "")


class GameObjectGroundingTests(unittest.TestCase):
    def setUp(self):
        self.persona = _FakePersona(
            _FakeScratchTile((1, 1)), _memory_tree())
        self.maze = _FakeMaze({
            "world": "the Ville",
            "sector": "Isabella Rodriguez's apartment",
            "arena": "main room",
            "game_object": "bed",
        })

    def test_malformed_address_returns_random_without_llm(self):
        for address in (None, "", "a:b", "a:b:c:d", 42):
            with mock.patch.object(
                    plan, "run_gpt_prompt_action_game_object") as mocked:
                self.assertEqual(
                    plan.generate_action_game_object(
                        "sleeping", address, self.persona, self.maze),
                    "<random>")
                mocked.assert_not_called()

    def test_unknown_arena_returns_random_without_llm(self):
        with mock.patch.object(
                plan, "run_gpt_prompt_action_game_object") as mocked:
            self.assertEqual(
                plan.generate_action_game_object(
                    "sleeping", "the Ville:Hobbs Cafe:nowhere",
                    self.persona, self.maze),
                "<random>")
            mocked.assert_not_called()

    def test_valid_model_object_resolves(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_game_object",
                               return_value=("bed", [])):
            self.assertEqual(
                plan.generate_action_game_object(
                    "sleeping", "the Ville:Isabella Rodriguez's apartment:main room",
                    self.persona, self.maze),
                "bed")

    def test_braced_quoted_model_object_canonicalizes(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_game_object",
                               return_value=("'{bed'", [])):
            self.assertEqual(
                plan.generate_action_game_object(
                    "sleeping", "the Ville:Isabella Rodriguez's apartment:main room",
                    self.persona, self.maze),
                "bed")

    def test_invalid_model_object_returns_random(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_game_object",
                               return_value=("moon base", [])):
            self.assertEqual(
                plan.generate_action_game_object(
                    "sleeping", "the Ville:Isabella Rodriguez's apartment:main room",
                    self.persona, self.maze),
                "<random>")

    def test_never_returns_raw_model_text(self):
        with mock.patch.object(plan, "run_gpt_prompt_action_game_object",
                               return_value=("quantum telescope", [])):
            self.assertEqual(
                plan.generate_action_game_object(
                    "sleeping", "the Ville:Isabella Rodriguez's apartment:main room",
                    self.persona, self.maze),
                "<random>")


class MemoryTreeHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tree = _memory_tree()

    def test_str_accessible_sectors_valid(self):
        self.assertEqual(
            self.tree.get_str_accessible_sectors("the Ville"),
            "Hobbs Cafe, Isabella Rodriguez's apartment, The Rose and Crown Pub")

    def test_str_accessible_sectors_malformed(self):
        for world in (None, 42, [], "nowhere"):
            self.assertEqual(self.tree.get_str_accessible_sectors(world), "")

    def test_str_accessible_sector_arenas_valid(self):
        self.assertEqual(
            self.tree.get_str_accessible_sector_arenas("the Ville:Hobbs Cafe"),
            "cafe")
        self.assertEqual(
            self.tree.get_str_accessible_sector_arenas(
                "the Ville:Isabella Rodriguez's apartment"),
            "main room, kitchen")

    def test_str_accessible_sector_arenas_malformed(self):
        for sector in (None, 42, "a", "a:b:c", "nowhere:x", "the Ville:nowhere"):
            self.assertEqual(
                self.tree.get_str_accessible_sector_arenas(sector), "")

    def test_str_accessible_arena_game_objects_valid(self):
        self.assertEqual(
            self.tree.get_str_accessible_arena_game_objects(
                "the Ville:Hobbs Cafe:cafe"),
            "refrigerator, cafe customer seating, piano")

    def test_str_accessible_arena_game_objects_malformed(self):
        for arena in (None, 42, "a:b", "a:b:c:d", "the Ville:Hobbs Cafe:nowhere",
                      "nowhere:x:y"):
            self.assertEqual(
                self.tree.get_str_accessible_arena_game_objects(arena), "")

    def test_no_lowercase_indexing_fallback(self):
        self.assertEqual(
            self.tree.get_str_accessible_arena_game_objects(
                "the ville:Hobbs Cafe:cafe"),
            "")

    def test_list_accessors(self):
        self.assertEqual(self.tree.get_world_list(), ["the Ville"])
        self.assertEqual(
            self.tree.get_accessible_sector_list("the Ville"),
            ["Hobbs Cafe", "Isabella Rodriguez's apartment",
             "The Rose and Crown Pub"])
        self.assertEqual(self.tree.get_accessible_sector_list("nowhere"), [])
        self.assertEqual(
            self.tree.get_accessible_arena_list("the Ville", "Hobbs Cafe"),
            ["cafe"])
        self.assertEqual(
            self.tree.get_accessible_arena_list(None, "x"), [])
        self.assertEqual(
            self.tree.get_accessible_game_object_list(
                "the Ville", "Hobbs Cafe", "cafe"),
            ["refrigerator", "cafe customer seating", "piano"])
        self.assertEqual(
            self.tree.get_accessible_game_object_list(
                "the Ville", "Hobbs Cafe", "nowhere"),
            [])
        self.assertEqual(
            self.tree.get_accessible_game_object_list_from_address(
                "the Ville:Hobbs Cafe:cafe"),
            ["refrigerator", "cafe customer seating", "piano"])
        self.assertEqual(
            self.tree.get_accessible_game_object_list_from_address("a:b"), [])

    def test_is_valid_arena(self):
        self.assertTrue(self.tree.is_valid_arena("the Ville", "Hobbs Cafe", "cafe"))
        self.assertFalse(
            self.tree.is_valid_arena("the Ville", "Hobbs Cafe", "nowhere"))
        self.assertFalse(
            self.tree.is_valid_arena("the Ville", "nowhere", "cafe"))
        self.assertFalse(self.tree.is_valid_arena("nowhere", "Hobbs Cafe", "cafe"))
        self.assertFalse(self.tree.is_valid_arena(None, "Hobbs Cafe", "cafe"))
        self.assertFalse(self.tree.is_valid_arena("the Ville", "Hobbs Cafe", None))

    def test_malformed_addresses_never_raise(self):
        garbage = [None, 42, [], {}, ["a"], "a", "a:b", "a:b:c", "a:b:c:d"]
        for value in garbage:
            self.tree.get_str_accessible_sectors(value)
            self.tree.get_str_accessible_sector_arenas(value)
            self.tree.get_str_accessible_arena_game_objects(value)
            self.tree.get_accessible_sector_list(value)
            self.tree.get_accessible_arena_list(value, value)
            self.tree.get_accessible_game_object_list(value, value, value)
            self.tree.get_accessible_game_object_list_from_address(value)
            self.tree.is_valid_arena(value, value, value)


class DetermineActionGroundingTests(unittest.TestCase):
    def setUp(self):
        self.scratch = Scratch("")
        self.scratch.name = "Test Persona"
        self.scratch.curr_tile = (1, 1)
        self.scratch.curr_time = datetime.datetime(2023, 6, 1, 0, 5)
        self.scratch.f_daily_schedule = [["sleeping", 30]]
        self.scratch.f_daily_schedule_hourly_org = [["sleeping", 30]]
        self.persona = _FakePersona(self.scratch, _memory_tree())
        self.maze = _FakeMaze({
            "world": "the Ville",
            "sector": "Isabella Rodriguez's apartment",
            "arena": "main room",
            "game_object": "bed",
        })

    def _mock_llm(self, sector, arena, game_object):
        patcher = mock.patch.multiple(
            plan,
            run_gpt_prompt_action_sector=mock.Mock(return_value=(sector, [])),
            run_gpt_prompt_action_arena=mock.Mock(return_value=(arena, [])),
            run_gpt_prompt_action_game_object=mock.Mock(
                return_value=(game_object, [])),
            run_gpt_prompt_pronunciatio=mock.Mock(return_value=("x", [])),
            run_gpt_prompt_event_triple=mock.Mock(return_value=("e", [])),
            run_gpt_prompt_act_obj_desc=mock.Mock(return_value=("d", [])),
            run_gpt_prompt_act_obj_event_triple=mock.Mock(return_value=("o", [])),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_determine_action_stores_only_grounded_address_with_garbage_llm(self):
        self._mock_llm("moon base", "moon base", "moon base")
        plan._determine_action(self.persona, self.maze)
        self.assertEqual(
            self.scratch.act_address,
            "the Ville:Isabella Rodriguez's apartment:main room:<random>")

    def test_determine_action_stores_real_object_address_with_valid_llm(self):
        self._mock_llm("Hobbs Cafe", "cafe", "piano")
        plan._determine_action(self.persona, self.maze)
        self.assertEqual(
            self.scratch.act_address, "the Ville:Hobbs Cafe:cafe:piano")

    def test_determine_action_grounds_when_tile_arena_is_invalid(self):
        self.maze = _FakeMaze({
            "world": "the Ville",
            "sector": "Isabella Rodriguez's apartment",
            "arena": "nowhere",
            "game_object": "bed",
        })
        self._mock_llm("moon base", "moon base", "moon base")
        plan._determine_action(self.persona, self.maze)
        self.assertEqual(
            self.scratch.act_address,
            "the Ville:Isabella Rodriguez's apartment:kitchen:<random>")


if __name__ == "__main__":
    unittest.main(verbosity=2)
