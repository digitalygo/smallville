"""Offline routing, vector provenance, persistence and migration refusal tests."""

import datetime
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import utils
from embedding_space import EmbeddingSpaceError
from persona.cognitive_modules.retrieve import extract_relevance
from persona.memory_structures.associative_memory import AssociativeMemory
from persona.prompt_template import gpt_structure as gpt

MODEL = "google/gemini-embedding-2"
SPACE = {"version": 1, "provider": "openrouter", "model": MODEL, "dimensions": 3072}


def write_memory(directory, vectors=None, space=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "embeddings.json").write_text(json.dumps(vectors or {}))
    (directory / "nodes.json").write_text("{}")
    (directory / "kw_strength.json").write_text(
        '{"kw_strength_event":{},"kw_strength_thought":{}}'
    )
    if space is not None:
        (directory / "embedding_space.json").write_text(json.dumps(space))


def persona_memory(directory):
    return SimpleNamespace(
        scratch=SimpleNamespace(
            name="Synthetic Resident", llm_provider=None, llm_model=None
        ),
        a_mem=AssociativeMemory(str(directory)),
    )


def add_event(persona, vector, key="synthetic-memory"):
    return persona.a_mem.add_event(
        datetime.datetime(2023, 2, 13),
        None,
        "Synthetic Resident",
        "is",
        "walking",
        "Synthetic resident walking",
        {"walking"},
        1,
        (key, vector),
        None,
    )


class EmbeddingSpaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "memory"
        write_memory(self.directory)
        self.persona = persona_memory(self.directory)
        self.client = Mock()
        self.client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.01] * 3072)]
        )
        self.patches = [
            patch.object(gpt, "llm_provider", "hetzner"),
            patch.object(gpt, "llm_model", "Qwen3.8-27B"),
            patch.object(gpt, "llm_persona_models", {}),
            patch.object(gpt, "llm_embedding_provider", "openrouter"),
            patch.object(gpt, "llm_embedding_model", MODEL),
            patch.object(gpt, "llm_embedding_models", {}),
            patch.object(gpt, "llm_embedding_dimensions", None),
            patch.object(gpt, "llm_legacy_embedding_space", None),
            patch.object(gpt, "llm_api_retries", 1),
            patch.object(gpt, "temp_sleep"),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        factory_patch = patch.object(gpt, "_get_client", return_value=self.client)
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)

    def test_approved_embedding_route_keeps_qwen_chat_and_requests_float_3072(self):
        vector = gpt.get_embedding(
            "Synthetic resident\nwalks to a cafe", persona=self.persona
        )
        self.factory.assert_called_once_with("openrouter")
        self.assertEqual(gpt._resolve_model(self.persona), ("hetzner", "Qwen3.8-27B"))
        self.assertEqual(len(vector), 3072)
        self.assertEqual(
            self.client.embeddings.create.call_args.kwargs,
            {
                "input": ["Synthetic resident walks to a cafe"],
                "model": MODEL,
                "encoding_format": "float",
                "dimensions": 3072,
            },
        )

    def test_checkpoint_memory_roundtrip_retains_space_and_real_retrieval(self):
        vector = gpt.get_embedding("Synthetic resident walking", persona=self.persona)
        add_event(self.persona, vector)
        saved = Path(self.temp.name) / "saved"
        self.persona.a_mem.save(str(saved))
        self.assertEqual(
            json.loads((saved / "embedding_space.json").read_text()), SPACE
        )
        loaded = persona_memory(saved)
        relevance = extract_relevance(loaded, loaded.a_mem.seq_event, "Synthetic query")
        self.assertAlmostEqual(relevance["node_1"], 1)
        self.assertEqual(loaded.a_mem.embeddings.space, SPACE)

    def test_same_dimensions_different_model_is_refused_before_api(self):
        write_memory(self.directory, {"memory": [0.01] * 3072}, SPACE)
        persona = persona_memory(self.directory)
        with (
            patch.object(gpt, "llm_embedding_model", "google/gemini-embedding-001"),
            self.assertRaisesRegex(EmbeddingSpaceError, "provider/model"),
        ):
            gpt.get_embedding("Synthetic query", persona=persona)
        self.factory.assert_not_called()

    def test_dimension_change_is_refused_without_changing_saved_data(self):
        write_memory(self.directory, {"memory": [0.01] * 3072}, SPACE)
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        with (
            patch.object(gpt, "llm_embedding_dimensions", 1536),
            self.assertRaisesRegex(EmbeddingSpaceError, "dimensions"),
        ):
            gpt.get_embedding("Synthetic query", persona=persona_memory(self.directory))
        self.assertEqual(
            before, {p.name: p.read_bytes() for p in self.directory.iterdir()}
        )
        self.factory.assert_not_called()

    def test_untagged_legacy_cache_is_refused_even_when_dimensions_match(self):
        write_memory(self.directory, {"memory": [0.01] * 3072})
        before = (self.directory / "embeddings.json").read_bytes()
        with self.assertRaisesRegex(EmbeddingSpaceError, "LLM_LEGACY_EMBEDDING_SPACE"):
            gpt.get_embedding("Synthetic query", persona=persona_memory(self.directory))
        self.factory.assert_not_called()
        self.assertEqual(before, (self.directory / "embeddings.json").read_bytes())
        self.assertFalse((self.directory / "embedding_space.json").exists())

    def test_explicit_verified_legacy_space_tags_new_copy_without_altering_original(
        self,
    ):
        write_memory(self.directory, {"memory": [0.01] * 3072})
        original = (self.directory / "embeddings.json").read_bytes()
        persona = persona_memory(self.directory)
        legacy = {key: value for key, value in SPACE.items() if key != "version"}
        with patch.object(gpt, "llm_legacy_embedding_space", legacy):
            gpt.get_embedding("Synthetic query", persona=persona)
        output = Path(self.temp.name) / "new-copy"
        persona.a_mem.save(str(output))
        self.assertEqual(
            json.loads((output / "embedding_space.json").read_text()), SPACE
        )
        self.assertEqual(original, (self.directory / "embeddings.json").read_bytes())
        self.assertFalse((self.directory / "embedding_space.json").exists())

    def test_legacy_attestation_cannot_hide_mixed_or_wrong_dimensions(self):
        legacy = {key: value for key, value in SPACE.items() if key != "version"}
        for vectors in ({"a": [0.1] * 1536}, {"a": [0.1] * 3072, "b": [0.1] * 1536}):
            with self.subTest(lengths=[len(v) for v in vectors.values()]):
                write_memory(self.directory, vectors)
                with (
                    patch.object(gpt, "llm_legacy_embedding_space", legacy),
                    self.assertRaises(EmbeddingSpaceError),
                ):
                    gpt.get_embedding(
                        "Synthetic query", persona=persona_memory(self.directory)
                    )
        self.factory.assert_not_called()

    def test_legacy_attestation_for_other_model_does_not_authorize_new_model(self):
        write_memory(self.directory, {"memory": [0.1] * 3072})
        legacy = {
            "provider": "openrouter",
            "model": "google/gemini-embedding-001",
            "dimensions": 3072,
        }
        with (
            patch.object(gpt, "llm_legacy_embedding_space", legacy),
            self.assertRaises(EmbeddingSpaceError),
        ):
            gpt.get_embedding("Synthetic query", persona=persona_memory(self.directory))
        self.factory.assert_not_called()

    def test_malformed_api_vectors_retry_bounded_without_mutating_cache(self):
        for vector in (
            [0.1] * 1536,
            [0] * 3072,
            [float("nan")] * 3072,
            [True] * 3072,
            [],
            "encoded",
        ):
            with self.subTest(vector_type=type(vector).__name__):
                self.client.embeddings.create.reset_mock()
                self.client.embeddings.create.return_value = SimpleNamespace(
                    data=[SimpleNamespace(embedding=vector)]
                )
                with self.assertRaises(utils.LLMProviderError) as raised:
                    gpt.get_embedding("Synthetic query", persona=self.persona)
                self.assertEqual(raised.exception.attempts, 2)
                self.assertEqual(self.client.embeddings.create.call_count, 2)
                self.assertEqual(self.persona.a_mem.embeddings, {})

    def test_bad_insert_is_refused_before_creating_a_node(self):
        gpt.bind_embedding_space(self.persona)
        with self.assertRaises(EmbeddingSpaceError):
            add_event(self.persona, [0.1] * 1536)
        self.assertEqual(self.persona.a_mem.id_to_node, {})
        self.assertEqual(self.persona.a_mem.seq_event, [])
        self.assertEqual(self.persona.a_mem.embeddings, {})

    def test_untagged_save_cannot_inherit_another_directories_provenance(self):
        output = Path(self.temp.name) / "existing-space"
        write_memory(output, {"saved": [0.1] * 3072}, SPACE)
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        with self.assertRaisesRegex(EmbeddingSpaceError, "overwrite"):
            self.persona.a_mem.save(str(output))
        self.assertEqual(before, {p.name: p.read_bytes() for p in output.iterdir()})

    def test_corrupt_metadata_and_invalid_cached_vectors_fail_closed(self):
        for metadata in (
            None,
            [],
            {},
            False,
            {**SPACE, "dimensions": True},
            {**SPACE, "version": 2},
        ):
            with self.subTest(metadata_type=type(metadata).__name__):
                (self.directory / "embedding_space.json").write_text(
                    json.dumps(metadata)
                )
                with self.assertRaises(EmbeddingSpaceError):
                    persona_memory(self.directory)
        (self.directory / "embedding_space.json").unlink()
        write_memory(self.directory, {"invalid": [float("inf")] * 3072}, SPACE)
        with self.assertRaises(EmbeddingSpaceError):
            gpt.bind_embedding_space(persona_memory(self.directory))

    def test_symlinked_metadata_is_refused_even_if_dangling(self):
        link = self.directory / "embedding_space.json"
        for target in (
            Path(self.temp.name) / "missing",
            self.directory / "kw_strength.json",
        ):
            link.symlink_to(target)
            with self.assertRaises(OSError):
                persona_memory(self.directory)
            link.unlink()

    def test_config_and_gemini_dimension_bounds(self):
        self.assertEqual(utils._parse_embedding_dimensions("1536"), 1536)
        self.assertIsNone(utils._parse_embedding_dimensions(""))
        for raw in ("0", "-1", "nan", "1.5"):
            with self.assertRaises(ValueError):
                utils._parse_embedding_dimensions(raw)
        for raw in (
            "{}",
            "null",
            '{"provider":"openrouter","model":"m","dimensions":true}',
        ):
            with self.assertRaises(ValueError):
                utils._parse_legacy_embedding_space(raw)
        for dimensions in (127, 3073):
            with (
                patch.object(gpt, "llm_embedding_dimensions", dimensions),
                self.assertRaises(ValueError),
            ):
                gpt.get_embedding("Synthetic query", persona=self.persona)
        self.factory.assert_not_called()

    def test_reduced_dimensions_are_explicitly_requested_and_tagged(self):
        self.client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.01] * 768)]
        )
        with patch.object(gpt, "llm_embedding_dimensions", 768):
            vector = gpt.get_embedding("Synthetic query", persona=self.persona)
        self.assertEqual(len(vector), 768)
        self.assertEqual(self.persona.a_mem.embeddings.space["dimensions"], 768)
        self.assertEqual(
            self.client.embeddings.create.call_args.kwargs["dimensions"], 768
        )

    def test_generic_model_latches_observed_dimension_without_sending_unsupported_parameter(
        self,
    ):
        self.client.embeddings.create.return_value = SimpleNamespace(
            data=[SimpleNamespace(embedding=[0.1] * 1536)]
        )
        with patch.object(gpt, "llm_embedding_model", "openai/text-embedding-ada-002"):
            vector = gpt.get_embedding("Synthetic query", persona=self.persona)
        self.assertEqual(len(vector), 1536)
        self.assertNotIn("dimensions", self.client.embeddings.create.call_args.kwargs)
        self.assertEqual(self.persona.a_mem.embeddings.space["dimensions"], 1536)

    def test_startup_checks_legacy_cache_before_any_catalog_or_embedding_request(self):
        import reverie

        write_memory(self.directory, {"legacy": [0.1] * 1536})
        with (
            patch.object(gpt, "verify_hetzner_models") as catalog,
            self.assertRaises(EmbeddingSpaceError),
        ):
            reverie._validate_provider_keys(
                {"Synthetic Resident": persona_memory(self.directory)}
            )
        catalog.assert_not_called()
        self.factory.assert_not_called()

    def test_real_checkpoint_resume_and_fork_preserve_space(self):
        import reverie

        storage = Path(self.temp.name) / "storage"
        seed = "base_the_ville_isabella_maria_klaus"
        shutil.copytree(Path(utils.fs_storage) / seed, storage / seed)
        with (
            patch.object(reverie, "fs_storage", str(storage)),
            patch.object(
                reverie, "fs_temp_storage", str(Path(self.temp.name) / "runtime")
            ),
        ):
            server = reverie.ReverieServer(seed, "new-space")
            for persona in server.personas.values():
                gpt.bind_embedding_space(persona)
            persona = next(iter(server.personas.values()))
            add_event(
                persona,
                gpt.get_embedding("Synthetic checkpoint memory", persona=persona),
            )
            server.save()
            resumed = reverie.ReverieServer(None, "new-space", resume=True)
            for persona in resumed.personas.values():
                gpt.bind_embedding_space(persona)
                self.assertEqual(persona.a_mem.embeddings.space, SPACE)
            forked = reverie.ReverieServer("new-space", "forked-space")
            self.assertEqual(
                next(iter(forked.personas.values())).a_mem.embeddings.space, SPACE
            )
            with (
                patch.object(gpt, "llm_embedding_dimensions", 1536),
                self.assertRaises(EmbeddingSpaceError),
            ):
                gpt.bind_embedding_space(next(iter(resumed.personas.values())))
