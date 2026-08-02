"""
Isolated unit tests for null-safe Scratch serialization.

The seeded bootstrap scratch files carry ``curr_time: null`` and
``act_start_time: null``. ``Scratch.save`` must serialize those as JSON null
instead of calling ``strftime`` on ``None``, and ``Scratch`` load must map a
null ``act_start_time`` back to ``act_start_time=None`` without touching
``curr_time``.

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

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from persona.memory_structures.scratch import Scratch  # noqa: E402

_TIME_FORMAT = "%B %d, %Y, %H:%M:%S"


def _load_scratch(path):
    """Load a scratch, failing if any ResourceWarning is emitted.

    ``Scratch`` load must close its input file. An unclosed file surfaces as a
    ``ResourceWarning`` during deallocation, so capture warnings and treat any
    as a test failure.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        scratch = Scratch(path)
    resource_warnings = [
        w for w in caught if issubclass(w.category, ResourceWarning)
    ]
    if resource_warnings:
        raise AssertionError(
            "unclosed file warning on scratch load: "
            + "; ".join(str(w.message) for w in resource_warnings)
        )
    return scratch


def _scratch_dict(curr_time=None, act_start_time=None):
    """A full scratch payload mirroring the seeded bootstrap scratch.json."""
    return {
        "vision_r": 8,
        "att_bandwidth": 8,
        "retention": 8,
        "curr_time": curr_time,
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
        "act_start_time": act_start_time,
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


class ScratchNullTimestampSaveTests(unittest.TestCase):
    """``Scratch.save`` must be null-safe for curr_time and act_start_time."""

    def test_save_null_timestamps_preserves_json_null(self):
        """A fresh scratch with null timestamps saves JSON null, no crash."""
        scratch = Scratch("/nonexistent/scratch.json")
        self.assertIsNone(scratch.curr_time)
        self.assertIsNone(scratch.act_start_time)

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "scratch.json")
            scratch.save(out)
            with open(out) as f:
                saved = json.load(f)

        self.assertIsNone(saved["curr_time"])
        self.assertIsNone(saved["act_start_time"])

    def test_save_non_null_timestamps_use_existing_format(self):
        """Non-null timestamps serialize with the existing datetime format."""
        scratch = Scratch("/nonexistent/scratch.json")
        scratch.curr_time = datetime.datetime(2023, 2, 13, 8, 30, 15)
        scratch.act_start_time = datetime.datetime(2023, 2, 13, 8, 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "scratch.json")
            scratch.save(out)
            with open(out) as f:
                saved = json.load(f)

        self.assertEqual(
            saved["curr_time"], "February 13, 2023, 08:30:15"
        )
        self.assertEqual(
            saved["act_start_time"], "February 13, 2023, 08:00:00"
        )


class ScratchNullTimestampLoadTests(unittest.TestCase):
    """``Scratch`` load must map null act_start_time to None, not curr_time."""

    def _load(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scratch.json")
            with open(path, "w") as f:
                json.dump(payload, f)
            return _load_scratch(path)

    def test_load_null_act_start_time_sets_act_start_time_none(self):
        """Null act_start_time yields act_start_time=None and keeps curr_time."""
        payload = _scratch_dict(
            curr_time="February 13, 2023, 00:00:00",
            act_start_time=None,
        )
        scratch = self._load(payload)

        self.assertIsNone(scratch.act_start_time)
        self.assertEqual(
            scratch.curr_time, datetime.datetime(2023, 2, 13, 0, 0, 0)
        )

    def test_load_non_null_act_start_time_parses_datetime(self):
        """A non-null act_start_time parses back to a datetime."""
        payload = _scratch_dict(
            curr_time="February 13, 2023, 00:00:00",
            act_start_time="February 13, 2023, 08:00:00",
        )
        scratch = self._load(payload)

        self.assertEqual(
            scratch.act_start_time, datetime.datetime(2023, 2, 13, 8, 0, 0)
        )
        self.assertEqual(
            scratch.curr_time, datetime.datetime(2023, 2, 13, 0, 0, 0)
        )


class ScratchRoundtripTests(unittest.TestCase):
    """Save then load must preserve null and non-null timestamps."""

    def _roundtrip(self, scratch):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "scratch.json")
            scratch.save(out)
            return _load_scratch(out)

    def test_roundtrip_null_timestamps(self):
        """Null timestamps survive a full save/load cycle."""
        scratch = Scratch("/nonexistent/scratch.json")
        loaded = self._roundtrip(scratch)

        self.assertIsNone(loaded.curr_time)
        self.assertIsNone(loaded.act_start_time)

    def test_roundtrip_non_null_timestamps(self):
        """Non-null timestamps survive a full save/load cycle unchanged."""
        scratch = Scratch("/nonexistent/scratch.json")
        scratch.curr_time = datetime.datetime(2023, 2, 13, 12, 45, 30)
        scratch.act_start_time = datetime.datetime(2023, 2, 13, 12, 0, 0)
        loaded = self._roundtrip(scratch)

        self.assertEqual(
            loaded.curr_time, datetime.datetime(2023, 2, 13, 12, 45, 30)
        )
        self.assertEqual(
            loaded.act_start_time, datetime.datetime(2023, 2, 13, 12, 0, 0)
        )


class ScratchNoResourceWarningTests(unittest.TestCase):
    """Scratch load and roundtrip must not leak unclosed file handles."""

    def test_load_emits_no_resource_warning(self):
        """Loading a saved scratch closes its input file."""
        payload = _scratch_dict(
            curr_time="February 13, 2023, 00:00:00",
            act_start_time="February 13, 2023, 08:00:00",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "scratch.json")
            with open(path, "w") as f:
                json.dump(payload, f)
            scratch = _load_scratch(path)

        self.assertEqual(
            scratch.curr_time, datetime.datetime(2023, 2, 13, 0, 0, 0)
        )

    def test_roundtrip_emits_no_resource_warning(self):
        """A full save/load cycle closes every file it opens."""
        scratch = Scratch("/nonexistent/scratch.json")
        scratch.curr_time = datetime.datetime(2023, 2, 13, 12, 45, 30)
        scratch.act_start_time = datetime.datetime(2023, 2, 13, 12, 0, 0)
        loaded = self._roundtrip(scratch)

        self.assertEqual(
            loaded.curr_time, datetime.datetime(2023, 2, 13, 12, 45, 30)
        )

    def _roundtrip(self, scratch):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "scratch.json")
            scratch.save(out)
            return _load_scratch(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
