"""
smallville environment configuration.

This file loads settings from a .env file (if present) and exposes them as
module-level variables. The existing import contract is preserved so that
callers using ``from utils import *`` continue to work.
"""

import json
import math
import os
from pathlib import Path
from dotenv import load_dotenv

_env_candidates = [
    Path(__file__).resolve().parent.parent.parent / ".env",
    Path(__file__).resolve().parent.parent / ".env",
    Path(__file__).resolve().parent / ".env",
]
for _p in _env_candidates:
    if _p.exists():
        load_dotenv(_p, override=False)
        break

openai_api_key = os.getenv("OPENAI_API_KEY", "")
openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
key_owner = os.getenv("KEY_OWNER", "anonymous")

_VALID_PROVIDERS = frozenset({"openai", "openrouter"})


def _validate_provider(provider: str) -> None:
    """Fail closed on unknown providers."""
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider}'. "
            f"Allowed values: {', '.join(sorted(_VALID_PROVIDERS))}."
        )


_llm_provider_raw = os.getenv("LLM_PROVIDER", "openai").strip().lower()
_validate_provider(_llm_provider_raw)
llm_provider: str = _llm_provider_raw

llm_model = os.getenv("LLM_MODEL", "").strip() or None


def _load_json_map(raw: str, env_name: str) -> dict[str, str]:
    """Parse a JSON object-of-strings env var, failing fast on malformed input.

    Raises ``ValueError`` with a clear message when the value is not valid
    JSON or is not a flat object mapping strings to strings. An empty or
    whitespace-only value yields an empty map.
    """
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(
            f"Invalid JSON in {env_name}: {exc}. "
            f"Expected a JSON object mapping names to model strings."
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"Invalid value in {env_name}: expected a JSON object, "
            f"got {type(parsed).__name__}."
        )
    result: dict[str, str] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(
                f"Invalid entry in {env_name}: keys and values must be strings, "
                f"got {key!r}: {value!r}."
            )
        result[key] = value
    return result


llm_persona_models: dict[str, str] = _load_json_map(
    os.getenv("LLM_PERSONA_MODELS", ""), "LLM_PERSONA_MODELS"
)

llm_embedding_model = os.getenv("LLM_EMBEDDING_MODEL", "").strip() or None
llm_embedding_models: dict[str, str] = _load_json_map(
    os.getenv("LLM_EMBEDDING_MODELS", ""), "LLM_EMBEDDING_MODELS"
)


class LLMProviderError(Exception):
    """Raised when an LLM provider call fails after bounded retries.

    The message names the provider, operation, and attempt count only.
    Prompts, API keys, and response bodies are never included.
    """

    def __init__(self, provider: str, operation: str, attempts: int) -> None:
        self.provider = provider
        self.operation = operation
        self.attempts = attempts
        super().__init__(
            f"LLM provider '{provider}' failed for {operation} "
            f"after {attempts} attempts."
        )


def _parse_llm_api_retries(raw: str) -> int:
    """Parse LLM_API_RETRIES as an integer >= 1, failing fast when malformed."""
    if not raw or not raw.strip():
        return 3
    try:
        value = int(raw.strip())
    except ValueError:
        raise ValueError(
            "Invalid LLM_API_RETRIES: expected an integer at least 1."
        ) from None
    if value < 1:
        raise ValueError(
            "Invalid LLM_API_RETRIES: must be an integer at least 1."
        )
    return value


def _parse_llm_api_retry_base_seconds(raw: str) -> float:
    """Parse LLM_API_RETRY_BASE_SECONDS as a finite float >= 0, failing fast."""
    if not raw or not raw.strip():
        return 1.0
    try:
        value = float(raw.strip())
    except ValueError:
        raise ValueError(
            "Invalid LLM_API_RETRY_BASE_SECONDS: expected a finite number "
            "at least 0."
        ) from None
    if not math.isfinite(value):
        raise ValueError(
            "Invalid LLM_API_RETRY_BASE_SECONDS: must be a finite number "
            ">= 0."
        )
    if value < 0:
        raise ValueError(
            "Invalid LLM_API_RETRY_BASE_SECONDS: must be >= 0."
        )
    return value


def _parse_llm_api_timeout_seconds(raw: str) -> float:
    """Parse LLM_API_TIMEOUT_SECONDS as a finite float > 0, failing fast."""
    if not raw or not raw.strip():
        return 60.0
    try:
        value = float(raw.strip())
    except ValueError:
        raise ValueError(
            "Invalid LLM_API_TIMEOUT_SECONDS: expected a finite number > 0."
        ) from None
    if not math.isfinite(value):
        raise ValueError(
            "Invalid LLM_API_TIMEOUT_SECONDS: must be a finite number > 0."
        )
    if value <= 0:
        raise ValueError(
            "Invalid LLM_API_TIMEOUT_SECONDS: must be > 0."
        )
    return value


llm_api_retries: int = _parse_llm_api_retries(os.getenv("LLM_API_RETRIES", ""))
llm_api_retry_base_seconds: float = _parse_llm_api_retry_base_seconds(
    os.getenv("LLM_API_RETRY_BASE_SECONDS", "")
)
llm_api_timeout_seconds: float = _parse_llm_api_timeout_seconds(
    os.getenv("LLM_API_TIMEOUT_SECONDS", "")
)

openrouter_referer = os.getenv(
    "OPENROUTER_REFERER", "https://github.com/digitalygo/smallville"
)
openrouter_title = os.getenv("OPENROUTER_TITLE", "smallville")
openrouter_categories = os.getenv("OPENROUTER_CATEGORIES", "game,roleplay")

_backend_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.normpath(os.path.join(_backend_dir, "..", ".."))
_frontend_server_dir = os.path.join(_project_root, "environment", "frontend_server")

maze_assets_loc = os.path.join(_frontend_server_dir, "static_dirs", "assets")
env_matrix = os.path.join(maze_assets_loc, "the_ville", "matrix")
env_visuals = os.path.join(maze_assets_loc, "the_ville", "visuals")

fs_storage = os.path.join(_frontend_server_dir, "storage")
fs_temp_storage = os.path.join(_frontend_server_dir, "temp_storage")

collision_block_id = "32125"

debug = os.getenv("DEBUG", "False").lower() in ("true", "1", "yes")
