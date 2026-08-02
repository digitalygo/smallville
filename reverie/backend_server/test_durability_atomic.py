"""
Atomic JSON publication, descriptor-safe reads, and renameat2 loading.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import gc
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

class AtomicWriteTests(unittest.TestCase):
    """``durability.atomic_write_json`` crash-safe publication."""

    def test_atomic_write_creates_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.json")
            durability.atomic_write_json(path, {"a": 1, "b": [1, 2]})
            with open(path) as f:
                self.assertEqual(json.load(f), {"a": 1, "b": [1, 2]})

    def test_atomic_write_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.json")
            durability.atomic_write_json(path, {"old": True})
            durability.atomic_write_json(path, {"new": True})
            with open(path) as f:
                self.assertEqual(json.load(f), {"new": True})

    def test_atomic_write_interrupted_leaves_original_intact(self):
        """A failed replace leaves the previous complete content readable."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.json")
            durability.atomic_write_json(path, {"original": True})
            with mock.patch.object(
                durability.os, "replace", side_effect=OSError("boom")
            ):
                with self.assertRaises(OSError):
                    durability.atomic_write_json(
                        path, {"partial": "x" * 10000}
                    )
            with open(path) as f:
                self.assertEqual(json.load(f), {"original": True})

    def test_atomic_write_cleans_temp_on_failure(self):
        """A failed write leaves no temp files behind."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.json")
            with mock.patch.object(
                durability.os, "replace", side_effect=OSError("boom")
            ):
                with self.assertRaises(OSError):
                    durability.atomic_write_json(path, {"a": 1})
            leftovers = [n for n in os.listdir(tmp) if n.startswith(".tmp-")]
            self.assertEqual(leftovers, [])

    def test_atomic_write_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "nested", "deep", "out.json")
            durability.atomic_write_json(path, {"a": 1})
            self.assertTrue(os.path.isfile(path))



class AtomicWriteBeneathTests(unittest.TestCase):
    """``durability.atomic_write_json_beneath`` descriptor-relative writes."""

    def test_beneath_creates_valid_json_and_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            durability.atomic_write_json_beneath(
                tmp, ("a", "b"), "out.json", {"a": 1, "b": [1, 2]}
            )
            path = os.path.join(tmp, "a", "b", "out.json")
            with open(path) as f:
                self.assertEqual(json.load(f), {"a": 1, "b": [1, 2]})
            self.assertTrue(os.path.isdir(os.path.join(tmp, "a", "b")))

    def test_beneath_overwrites_existing_leaf(self):
        with tempfile.TemporaryDirectory() as tmp:
            durability.atomic_write_json_beneath(
                tmp, (), "out.json", {"old": True}
            )
            durability.atomic_write_json_beneath(
                tmp, (), "out.json", {"new": True}
            )
            with open(os.path.join(tmp, "out.json")) as f:
                self.assertEqual(json.load(f), {"new": True})

    def test_beneath_interrupted_preserves_prior_content(self):
        """A failed replace leaves the previous complete content readable."""
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a")
            os.makedirs(nested)
            durability.atomic_write_json_beneath(
                tmp, ("a",), "out.json", {"original": True}
            )
            with mock.patch.object(
                durability.os, "replace", side_effect=OSError("boom")
            ):
                with self.assertRaises(OSError):
                    durability.atomic_write_json_beneath(
                        tmp, ("a",), "out.json", {"partial": "x" * 10000}
                    )
            with open(os.path.join(nested, "out.json")) as f:
                self.assertEqual(json.load(f), {"original": True})
            leftovers = [
                name
                for name in os.listdir(nested)
                if name.startswith(".tmp-")
            ]
            self.assertEqual(leftovers, [])

    def test_beneath_rejects_unsafe_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            for parents, filename in (
                ((".",), "out.json"),
                (("..",), "out.json"),
                (("a/",), "out.json"),
                (("a\x00b",), "out.json"),
                (("a", "b\\c"), "out.json"),
                ((), "."),
                ((), ".."),
                ((), "a/b"),
                ((), "a\x00b"),
                ((), ""),
            ):
                with self.subTest(parents=parents, filename=filename):
                    with self.assertRaises(ValueError):
                        durability.atomic_write_json_beneath(
                            tmp, parents, filename, {"a": 1}
                        )

    def test_beneath_refuses_symlinked_parent_directory(self):
        """A preexisting symlink parent is refused; outside is untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            with open(os.path.join(outside, "victim.json"), "w") as f:
                json.dump({"keep": True}, f)
            os.symlink(outside, os.path.join(tmp, "linked"))
            with self.assertRaises(OSError):
                durability.atomic_write_json_beneath(
                    tmp, ("linked",), "out.json", {"a": 1}
                )
            self.assertEqual(os.listdir(outside), ["victim.json"])
            with open(os.path.join(outside, "victim.json")) as f:
                self.assertEqual(json.load(f), {"keep": True})

    def test_beneath_replaces_symlink_leaf_without_touching_target(self):
        """A symlink leaf is replaced by rename, never followed."""
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside.json")
            with open(outside, "w") as f:
                json.dump({"keep": True}, f)
            leaf = os.path.join(tmp, "out.json")
            os.symlink(outside, leaf)
            durability.atomic_write_json_beneath(
                tmp, (), "out.json", {"a": 1}
            )
            self.assertFalse(os.path.islink(leaf))
            with open(leaf) as f:
                self.assertEqual(json.load(f), {"a": 1})
            with open(outside) as f:
                self.assertEqual(json.load(f), {"keep": True})

    def test_beneath_emits_no_resource_warning(self):
        """Beneath writes close every descriptor, success or failure."""
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a")
            os.makedirs(nested)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                durability.atomic_write_json_beneath(
                    tmp, ("a",), "ok.json", {"a": 1}
                )
                with mock.patch.object(
                    durability.os, "replace", side_effect=OSError("boom")
                ):
                    with self.assertRaises(OSError):
                        durability.atomic_write_json_beneath(
                            tmp, ("a",), "ok.json", {"b": 2}
                        )
                gc.collect()
            resource_warnings = [
                w for w in caught if issubclass(w.category, ResourceWarning)
            ]
            self.assertEqual(resource_warnings, [])



class ReadJsonCheckedTests(unittest.TestCase):
    """``durability.read_json_checked`` descriptor-safe regular JSON reads."""

    def _read_no_warning(self, path):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            result = durability.read_json_checked(path)
        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        if resource_warnings:
            raise AssertionError(
                "unclosed file warning on checked read: "
                + "; ".join(str(w.message) for w in resource_warnings)
            )
        return result

    def test_reads_valid_utf8_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "data.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"name": "café", "items": [1, 2]}, f)
            self.assertEqual(
                self._read_no_warning(path),
                {"name": "café", "items": [1, 2]},
            )

    def test_refuses_symlink_with_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "real.json")
            with open(real, "w") as f:
                json.dump({}, f)
            link = os.path.join(tmp, "link.json")
            os.symlink(real, link)
            with self.assertRaises(OSError) as ctx:
                durability.read_json_checked(link)
            self.assertIn(link, str(ctx.exception))

    def test_refuses_directory_with_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "adir")
            os.makedirs(path)
            with self.assertRaises(OSError) as ctx:
                durability.read_json_checked(path)
            self.assertIn(path, str(ctx.exception))

    def test_refuses_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pipe.json")
            os.mkfifo(path)
            with self.assertRaises(OSError) as ctx:
                durability.read_json_checked(path)
            self.assertIn(path, str(ctx.exception))

    def test_malformed_json_raises_value_error_with_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w") as f:
                f.write("{not valid json")
            with self.assertRaises(ValueError) as ctx:
                durability.read_json_checked(path)
            self.assertIn(path, str(ctx.exception))

    def test_missing_file_raises_clear_os_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "missing.json")
            with self.assertRaises(OSError) as ctx:
                durability.read_json_checked(path)
            self.assertIn(path, str(ctx.exception))

    def test_rejects_traversal_components_before_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside.json")
            with open(outside, "w") as f:
                json.dump({"secret": True}, f)
            with self.assertRaises(ValueError) as ctx:
                durability.read_json_checked(
                    os.path.join(tmp, "..", "outside.json")
                )
            self.assertIn("Unsafe", str(ctx.exception))

    def test_refuses_symlinked_parent_without_reading_victim(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside_dir = os.path.join(tmp, "outside")
            os.makedirs(outside_dir)
            victim = os.path.join(outside_dir, "data.json")
            with open(victim, "w") as f:
                f.write("{not valid json")
            path = os.path.join(tmp, "linked", "data.json")
            os.symlink(outside_dir, os.path.join(tmp, "linked"))

            with self.assertRaises(OSError) as ctx:
                durability.read_json_checked(path)
            self.assertIn(path, str(ctx.exception))
            with open(victim) as f:
                self.assertEqual(f.read(), "{not valid json")

    def test_parent_swap_to_symlink_after_root_open_never_reads_victim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "root")
            os.makedirs(os.path.join(root, "a"))
            with open(os.path.join(root, "a", "data.json"), "w") as f:
                json.dump({"ok": True}, f)
            outside_dir = os.path.join(tmp, "outside")
            os.makedirs(outside_dir)
            victim = os.path.join(outside_dir, "data.json")
            with open(victim, "w") as f:
                f.write("{not valid json")

            real_open = os.open
            real_rename = os.rename
            real_symlink = os.symlink
            state = {"done": False}
            old_cwd = os.getcwd()

            def swap(path, flags, *args, **kwargs):
                if (
                    not state["done"]
                    and "dir_fd" not in kwargs
                    and path == "."
                ):
                    state["done"] = True
                    fd = real_open(path, flags, *args, **kwargs)
                    real_rename(
                        os.path.join(root, "a"), os.path.join(root, "a_real")
                    )
                    real_symlink(outside_dir, os.path.join(root, "a"))
                    return fd
                return real_open(path, flags, *args, **kwargs)

            os.chdir(root)
            try:
                path = os.path.join("a", "data.json")
                with mock.patch.object(durability.os, "open", side_effect=swap):
                    with self.assertRaises(OSError) as ctx:
                        durability.read_json_checked(path)
                self.assertIn(path, str(ctx.exception))
            finally:
                os.chdir(old_cwd)
            with open(victim) as f:
                self.assertEqual(f.read(), "{not valid json")

    def test_parse_failure_emits_no_resource_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.json")
            with open(path, "w") as f:
                f.write("{not valid json")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                with self.assertRaises(ValueError):
                    durability.read_json_checked(path)
            resource_warnings = [
                w for w in caught if issubclass(w.category, ResourceWarning)
            ]
            self.assertEqual(resource_warnings, [])



class ReadJsonBeneathTests(unittest.TestCase):
    """``durability.read_json_beneath`` descriptor-relative JSON reads."""

    def _write_tree(self, tmp):
        nested = os.path.join(tmp, "a", "b")
        os.makedirs(nested)
        path = os.path.join(nested, "data.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"name": "café", "items": [1, 2]}, f)
        return path

    def test_reads_valid_nested_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_tree(tmp)
            self.assertEqual(
                durability.read_json_beneath(tmp, ("a", "b", "data.json")),
                {"name": "café", "items": [1, 2]},
            )

    def test_rejects_unsafe_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            for components in (
                (),
                [],
                (".", "data.json"),
                ("..", "data.json"),
                ("a/", "data.json"),
                ("a", "b\\c", "d.json"),
                ("a\x00b", "data.json"),
                ("a\nb", "data.json"),
                (123, "data.json"),
                ("", "data.json"),
            ):
                with self.subTest(components=components):
                    with self.assertRaises(ValueError):
                        durability.read_json_beneath(tmp, components)

    def test_missing_final_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_tree(tmp)
            with self.assertRaises(FileNotFoundError):
                durability.read_json_beneath(tmp, ("a", "b", "missing.json"))

    def test_missing_parent_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_tree(tmp)
            with self.assertRaises(FileNotFoundError):
                durability.read_json_beneath(tmp, ("a", "gone", "data.json"))

    def test_refuses_final_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_tree(tmp)
            outside = os.path.join(tmp, "outside.json")
            with open(outside, "w") as f:
                json.dump({"secret": True}, f)
            os.symlink(outside, os.path.join(tmp, "a", "b", "link.json"))
            with self.assertRaises(OSError):
                durability.read_json_beneath(tmp, ("a", "b", "link.json"))

    def test_refuses_symlink_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_tree(tmp)
            outside_dir = os.path.join(tmp, "outside")
            os.makedirs(outside_dir)
            with open(os.path.join(outside_dir, "data.json"), "w") as f:
                json.dump({"secret": True}, f)
            os.symlink(outside_dir, os.path.join(tmp, "a", "linked"))
            with self.assertRaises(OSError):
                durability.read_json_beneath(tmp, ("a", "linked", "data.json"))

    def test_ancestor_swap_to_symlink_after_root_open_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_tree(tmp)
            outside_dir = os.path.join(tmp, "outside")
            os.makedirs(outside_dir)
            with open(os.path.join(outside_dir, "data.json"), "w") as f:
                json.dump({"secret": True}, f)
            real_open = os.open
            real_rename = os.rename
            real_symlink = os.symlink
            state = {"done": False}

            def swap(path, flags, *args, **kwargs):
                if not state["done"] and "dir_fd" not in kwargs and path == tmp:
                    state["done"] = True
                    fd = real_open(path, flags, *args, **kwargs)
                    real_rename(
                        os.path.join(tmp, "a"), os.path.join(tmp, "a_real")
                    )
                    real_symlink(outside_dir, os.path.join(tmp, "a"))
                    return fd
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(durability.os, "open", side_effect=swap):
                with self.assertRaises(OSError):
                    durability.read_json_beneath(tmp, ("a", "data.json"))
            with open(os.path.join(outside_dir, "data.json")) as f:
                self.assertEqual(json.load(f), {"secret": True})

    def test_malformed_json_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a")
            os.makedirs(nested)
            with open(os.path.join(nested, "bad.json"), "w") as f:
                f.write("{not valid json")
            with self.assertRaises(ValueError) as ctx:
                durability.read_json_beneath(tmp, ("a", "bad.json"))
            self.assertIn("bad.json", str(ctx.exception))

    def test_refuses_directory_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a")
            os.makedirs(nested)
            os.makedirs(os.path.join(nested, "dir.json"))
            with self.assertRaises(OSError):
                durability.read_json_beneath(tmp, ("a", "dir.json"))

    def test_emits_no_resource_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_tree(tmp)
            with open(os.path.join(tmp, "a", "b", "bad.json"), "w") as f:
                f.write("{not valid json")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                value = durability.read_json_beneath(
                    tmp, ("a", "b", "data.json")
                )
                self.assertEqual(value["name"], "café")
                with self.assertRaises(ValueError):
                    durability.read_json_beneath(tmp, ("a", "b", "bad.json"))
                with self.assertRaises(FileNotFoundError):
                    durability.read_json_beneath(tmp, ("a", "b", "gone.json"))
                gc.collect()
            resource_warnings = [
                w for w in caught if issubclass(w.category, ResourceWarning)
            ]
            self.assertEqual(resource_warnings, [])



class Renameat2LoadingTests(unittest.TestCase):
    """Architecture-neutral renameat2 resolution, fail-closed, and real use."""

    def setUp(self):
        self._original = durability._renameat2
        durability._renameat2 = None

    def tearDown(self):
        durability._renameat2 = self._original

    def test_find_library_resolves_aarch64_musl_soname(self):
        calls = []
        func = mock.Mock(return_value=0)

        def fake_cdll(name, use_errno=False):
            calls.append(name)
            if name is None:
                raise AttributeError(
                    "no renameat2 in the main program namespace"
                )
            libc = mock.Mock()
            libc.renameat2 = func
            return libc

        with mock.patch.object(
            durability.ctypes.util,
            "find_library",
            return_value="libc.musl-aarch64.so.1",
        ):
            with mock.patch.object(
                durability.ctypes, "CDLL", side_effect=fake_cdll
            ):
                loaded = durability._load_renameat2()
        self.assertIs(loaded, func)
        self.assertEqual(calls, [None, "libc.musl-aarch64.so.1"])
        self.assertEqual(
            loaded.argtypes,
            (
                durability.ctypes.c_int,
                durability.ctypes.c_char_p,
                durability.ctypes.c_int,
                durability.ctypes.c_char_p,
                durability.ctypes.c_uint,
            ),
        )
        durability.rename_no_replace("src", "dst")
        func.assert_called_once()

    def test_main_namespace_found_without_find_library_soname(self):
        func = mock.Mock(return_value=0)

        def fake_cdll(name, use_errno=False):
            libc = mock.Mock()
            libc.renameat2 = func
            return libc

        with mock.patch.object(
            durability.ctypes.util, "find_library", return_value=None
        ):
            with mock.patch.object(
                durability.ctypes, "CDLL", side_effect=fake_cdll
            ):
                loaded = durability._load_renameat2()
        self.assertIs(loaded, func)
        durability.rename_no_replace("src", "dst")
        func.assert_called_once()

    def test_fail_closed_when_renameat2_unavailable(self):
        def fake_cdll(name, use_errno=False):
            raise AttributeError("no renameat2")

        with mock.patch.object(
            durability.ctypes.util, "find_library", return_value=None
        ):
            with mock.patch.object(
                durability.ctypes, "CDLL", side_effect=fake_cdll
            ):
                loaded = durability._load_renameat2()
        self.assertIsNone(loaded)
        with self.assertRaises(OSError) as ctx:
            durability.rename_no_replace("src", "dst")
        self.assertIn("renameat2", str(ctx.exception))

    def test_current_linux_actual_no_replace(self):
        if durability._load_renameat2() is None:
            self.skipTest("renameat2 not available on this system")
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "src")
            os.makedirs(os.path.join(src, "payload"))
            with open(os.path.join(src, "payload", "id.txt"), "w") as f:
                f.write("alpha")
            dst = os.path.join(tmp, "dst")
            durability.rename_no_replace(src, dst)
            self.assertTrue(os.path.isdir(dst))
            self.assertFalse(os.path.exists(src))

            loser = os.path.join(tmp, "loser")
            os.makedirs(os.path.join(loser, "payload"))
            with open(os.path.join(loser, "payload", "id.txt"), "w") as f:
                f.write("beta")
            with self.assertRaises(FileExistsError):
                durability.rename_no_replace(loser, dst)
            self.assertTrue(
                os.path.isdir(loser),
                "the loser's staging must remain for its owner to clean",
            )
            with open(os.path.join(dst, "payload", "id.txt")) as f:
                self.assertEqual(f.read(), "alpha")

