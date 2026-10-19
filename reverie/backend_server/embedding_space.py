"""Provenance and shape checks for persistent persona embedding caches."""

import math

from durability import read_json_checked


class EmbeddingSpaceError(ValueError):
    pass


def validate_vector(vector, dimensions=None):
    if not isinstance(vector, (list, tuple)) or not vector:
        raise EmbeddingSpaceError("Embedding must be a nonempty numeric vector.")
    if dimensions is not None and len(vector) != dimensions:
        raise EmbeddingSpaceError(
            "Embedding dimensions differ from the configured memory space."
        )
    if any(
        type(value) not in (int, float) or not math.isfinite(value) for value in vector
    ):
        raise EmbeddingSpaceError("Embedding contains invalid numeric values.")
    if not any(value != 0 for value in vector):
        raise EmbeddingSpaceError("Embedding must have nonzero magnitude.")
    return list(vector)


def validate_space(space, *, allow_unknown_dimensions=False):
    if not isinstance(space, dict) or set(space) != {
        "version",
        "provider",
        "model",
        "dimensions",
    }:
        raise EmbeddingSpaceError("Invalid embedding_space.json schema.")
    if type(space["version"]) is not int or space["version"] != 1:
        raise EmbeddingSpaceError("Unsupported embedding space version.")
    if (
        space["provider"] not in ("openai", "openrouter")
        or not isinstance(space["model"], str)
        or not space["model"].strip()
    ):
        raise EmbeddingSpaceError("Invalid embedding provider/model provenance.")
    dimensions = space["dimensions"]
    if not (allow_unknown_dimensions and dimensions is None) and (
        type(dimensions) is not int or dimensions < 1
    ):
        raise EmbeddingSpaceError("Invalid embedding space dimensions.")
    return dict(space)


class EmbeddingIndex(dict):
    def __init__(self, vectors, space=None):
        if not isinstance(vectors, dict):
            raise EmbeddingSpaceError("Saved embeddings must be a JSON object.")
        super().__init__(vectors)
        self.space = (
            validate_space(space, allow_unknown_dimensions=not vectors)
            if space is not None
            else None
        )
        self._bound_request = None

    @classmethod
    def load(cls, directory):
        missing = object()
        space = read_json_checked(
            directory + "/embedding_space.json", missing_default=missing
        )
        if space is not missing:
            validate_space(space, allow_unknown_dimensions=True)
        return cls(
            read_json_checked(directory + "/embeddings.json"),
            None if space is missing else space,
        )

    def bind(self, provider, model, dimensions=None, legacy_space=None):
        request = (provider, model, dimensions)
        if self._bound_request == request:
            return self.space["dimensions"]
        if not self:
            self.space = {
                "version": 1,
                "provider": provider,
                "model": model,
                "dimensions": dimensions,
            }
        elif self.space is None:
            if legacy_space is None:
                raise EmbeddingSpaceError(
                    "Saved embeddings have no model provenance. Refusing to mix vector spaces. "
                    "Verify their original provider/model/dimensions before setting "
                    "LLM_LEGACY_EMBEDDING_SPACE, or use a new simulation from an empty seed. "
                    "Existing data has not been changed."
                )
            self.space = validate_space({"version": 1, **legacy_space})
        if self.space["provider"] != provider or self.space["model"] != model:
            raise EmbeddingSpaceError(
                "Saved embedding provider/model differs from the configured route. Existing data is preserved; use its original route or explicitly re-embed into a separate simulation."
            )
        if dimensions is not None and self.space["dimensions"] != dimensions:
            raise EmbeddingSpaceError(
                "Saved embedding dimensions differ from the configured route. Existing data is preserved; vectors are never padded or truncated."
            )
        for vector in self.values():
            validate_vector(vector, self.space["dimensions"])
        self._bound_request = request
        return self.space["dimensions"]

    def observe(self, vector):
        if self._bound_request is None:
            raise EmbeddingSpaceError(
                "Bind the embedding route before accepting new vectors."
            )
        checked = validate_vector(vector, self.space["dimensions"])
        if self.space["dimensions"] is None:
            self.space["dimensions"] = len(checked)
        return checked

    def __setitem__(self, key, value):
        if self._bound_request is not None:
            value = self.observe(value)
        super().__setitem__(key, value)

    def prepare_pair(self, pair):
        if self._bound_request is None:
            return pair
        return pair[0], self.observe(pair[1])

    def check_save_destination(self, directory):
        missing = object()
        existing = read_json_checked(
            directory + "/embedding_space.json", missing_default=missing
        )
        if existing is not missing:
            validate_space(existing, allow_unknown_dimensions=True)
            if existing != self.space:
                raise EmbeddingSpaceError(
                    "Refusing to overwrite a different or unverified embedding space. Save to a separate simulation or directory."
                )
