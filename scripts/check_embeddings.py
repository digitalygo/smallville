#!/usr/bin/env python3
"""Opt-in single synthetic Gemini Embedding 2 probe through OpenRouter."""

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reverie/backend_server"))
from embedding_space import validate_vector
from persona.prompt_template import gpt_structure as gpt
from utils import LLMProviderError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dimensions", type=int, choices=(768, 1536, 3072), default=3072
    )
    args = parser.parse_args()
    if (
        not gpt.openrouter_api_key.strip()
        or gpt.openrouter_api_key.strip() == "sk-or-v1-your-openrouter-key"
    ):
        parser.error(
            "OPENROUTER_API_KEY must be securely configured in the environment."
        )
    model = "google/gemini-embedding-2"
    started = time.monotonic()
    try:
        client = gpt._get_client("openrouter")

        def request():
            response = client.embeddings.create(
                model=model,
                dimensions=args.dimensions,
                encoding_format="float",
                input=[
                    "Synthetic simulation test: a fictional resident walks to a cafe."
                ],
            )
            return validate_vector(response.data[0].embedding, args.dimensions)

        vector = gpt._call_with_retries("openrouter", "embedding", request)
    except (LLMProviderError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "provider": "openrouter",
                "model": model,
                "dimensions": len(vector),
                "encoding_format": "float",
                "finite": all(math.isfinite(value) for value in vector),
                "norm": round(math.hypot(*vector), 6),
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "synthetic_embedding_verified": True,
                "request_attempts": gpt.request_activity()["requests"],
                "hetzner_called": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
