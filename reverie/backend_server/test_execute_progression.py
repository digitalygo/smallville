"""Real path execution must advance state and tolerate an unknown destination."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona.cognitive_modules.execute import execute


class MazeFixture:
    def __init__(self):
        self.collision_maze = [["0"] * 6 for _ in range(4)]
        self.address_tiles = {"the Ville:cafe:table": {(3, 1)}}

    def access_tile(self, tile):
        return {"events": set()}


def resident():
    scratch = SimpleNamespace(
        curr_tile=(1, 1),
        act_path_set=False,
        planned_path=[],
        act_description="walking to the cafe",
        act_address="the Ville:cafe:table",
        act_pronunciatio="☕",
    )
    return SimpleNamespace(name="Synthetic Resident", scratch=scratch)


class ExecutionProgressionTests(unittest.TestCase):
    def test_real_path_consumes_tiles_and_reaches_destination(self):
        persona = resident()
        visited = []
        for _ in range(3):
            tile, icon, description = execute(
                persona,
                MazeFixture(),
                {persona.name: persona},
                persona.scratch.act_address,
            )
            persona.scratch.curr_tile = tile
            visited.append(tile)
            self.assertEqual(icon, "☕")
            self.assertIn("cafe", description)
        self.assertEqual(visited, [(2, 1), (3, 1), (3, 1)])
        self.assertEqual(persona.scratch.planned_path, [])
        self.assertTrue(persona.scratch.act_path_set)

    def test_unknown_or_empty_destination_stays_put_without_crashing(self):
        for plan in ("unknown:model:destination", "empty"):
            with self.subTest(plan=plan):
                persona = resident()
                maze = MazeFixture()
                maze.address_tiles = {"empty": set()}
                tile, _, _ = execute(persona, maze, {persona.name: persona}, plan)
                self.assertEqual(tile, (1, 1))
                self.assertEqual(persona.scratch.planned_path, [])
