#!/usr/bin/env python3
"""Explicit opt-in Hetzner catalog check; --chat sends one synthetic prompt.

Requires HETZNER_API_KEY from the environment. Never prints the key,
prompt, response or a raw provider exception. Uses the app's shared gate.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reverie/backend_server"))
from persona.prompt_template import gpt_structure as gpt
from utils import LLMProviderError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Also send one synthetic chat request to Qwen3.8-27B",
    )
    args = parser.parse_args()
    if not gpt.hetzner_api_key.strip():
        parser.error("HETZNER_API_KEY must be securely provisioned in the environment.")
    model = "Qwen3.8-27B"
    try:
        gpt.verify_hetzner_models({model})
        if args.chat:
            client = gpt._get_client("hetzner")
            gpt._call_with_retries(
                "hetzner",
                "chat",
                lambda: gpt._chat_completion_content(
                    client,
                    model=model,
                    messages=[
                        {
                            "role": "user",
                            "content": "Synthetic simulation test: a fictional resident walks to a cafe. Reply with OK.",
                        }
                    ],
                ),
            )
    except (LLMProviderError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "provider": "hetzner",
                "model": model,
                "catalog_verified": True,
                "synthetic_chat_verified": args.chat,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
