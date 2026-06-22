"""
smallville environment configuration.

This file loads settings from a .env file (if present) and exposes them as
module-level variables. The existing import contract is preserved so that
callers using ``from utils import *`` continue to work.
"""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

# Locate .env relative to this file:
#   reverie/backend_server/utils.py  ->  ../../.env  (repo root)
#   fallback: same directory, then parent
_env_candidates = [
    Path(__file__).resolve().parent.parent.parent / ".env",
    Path(__file__).resolve().parent.parent / ".env",
    Path(__file__).resolve().parent / ".env",
]
for _p in _env_candidates:
    if _p.exists():
        load_dotenv(_p, override=False)
        break

# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------
openai_api_key = os.getenv("OPENAI_API_KEY", "")
openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
key_owner = os.getenv("KEY_OWNER", "anonymous")

# ---------------------------------------------------------------------------
# Provider and model defaults
# ---------------------------------------------------------------------------
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

# Per-persona model override map  (JSON string -> dict)
# Format: {"Persona Name": "provider:model"} or {"Persona Name": "model"}
llm_persona_models: dict[str, str] = {}
_persona_models_raw = os.getenv("LLM_PERSONA_MODELS", "")
if _persona_models_raw:
    try:
        llm_persona_models = json.loads(_persona_models_raw)
    except (json.JSONDecodeError, TypeError):
        pass

# ---------------------------------------------------------------------------
# OpenRouter attribution
# ---------------------------------------------------------------------------
openrouter_referer = os.getenv(
    "OPENROUTER_REFERER", "https://github.com/digitalygo/smallville"
)
openrouter_title = os.getenv("OPENROUTER_TITLE", "smallville")
openrouter_categories = os.getenv("OPENROUTER_CATEGORIES", "game,roleplay")

# ---------------------------------------------------------------------------
# File-system paths
# ---------------------------------------------------------------------------
maze_assets_loc = "../../environment/frontend_server/static_dirs/assets"
env_matrix = f"{maze_assets_loc}/the_ville/matrix"
env_visuals = f"{maze_assets_loc}/the_ville/visuals"

fs_storage = "../../environment/frontend_server/storage"
fs_temp_storage = "../../environment/frontend_server/temp_storage"

collision_block_id = "32125"

# ---------------------------------------------------------------------------
# Debug flag
# ---------------------------------------------------------------------------
debug = os.getenv("DEBUG", "False").lower() in ("true", "1", "yes")
