"""
Author: Joon Sung Park (joonspk@stanford.edu)

File: gpt_structure.py
Description: Wrapper functions for calling OpenAI / OpenRouter APIs.
"""
from __future__ import annotations

import json
import os
import random
import time

from openai import OpenAI

from utils import *

# ---------------------------------------------------------------------------
# Provider client cache  (lazy, keyed by provider name)
# ---------------------------------------------------------------------------
_clients: dict[str, OpenAI] = {}


def _get_client(provider: str) -> OpenAI:
    """Return (and cache) an OpenAI-compatible client for the given provider."""
    if provider in _clients:
        return _clients[provider]

    if provider == "openrouter":
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_api_key,
            default_headers={
                "HTTP-Referer": openrouter_referer,
                "X-Title": openrouter_title,
                "X-OpenRouter-Title": openrouter_title,
                "X-OpenRouter-Categories": openrouter_categories,
            },
        )
    else:
        client = OpenAI(api_key=openai_api_key)

    _clients[provider] = client
    return client


# ---------------------------------------------------------------------------
# Model resolution for a given persona
#   Priority: persona.scratch fields > LLM_PERSONA_MODELS env map > env defaults
# ---------------------------------------------------------------------------

# Sensible defaults per provider when no explicit model is configured.
_DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-4o",
    "openrouter": "openai/gpt-4o",
}

_VALID_PROVIDERS = frozenset({"openai", "openrouter"})


def _validate_provider(provider: str) -> None:
    """Fail closed on unknown providers."""
    if provider not in _VALID_PROVIDERS:
        raise ValueError(
            f"Unknown LLM provider '{provider}'. "
            f"Allowed values: {', '.join(sorted(_VALID_PROVIDERS))}."
        )


def _resolve_model(persona=None, fallback_engine: str | None = None):
    """Resolve (provider, model) for the next API call.

    Returns a ``(provider, model)`` tuple.
    """
    provider = llm_provider
    model = llm_model

    # 1. Check persona scratch fields  (highest priority)
    has_scratch_provider = False
    has_scratch_model = False
    if persona is not None:
        ps = getattr(persona, "scratch", None)
        if ps is not None:
            if getattr(ps, "llm_provider", None):
                provider = str(ps.llm_provider).strip().lower()
                _validate_provider(provider)
                has_scratch_provider = True
            if getattr(ps, "llm_model", None):
                model = ps.llm_model
                has_scratch_model = True

    # 2. Check per-persona env override map  (only for fields scratch left unset)
    if persona is not None and llm_persona_models:
        name = getattr(persona.scratch, "name", None) if hasattr(persona, "scratch") else None
        if name and name in llm_persona_models:
            raw = llm_persona_models[name]
            # Only treat known provider prefixes as explicit provider:model splits.
            # Bare model slugs containing ":" (e.g. "openai/gpt-4o") stay intact.
            for known_prefix in ("openai:", "openrouter:"):
                if raw.startswith(known_prefix):
                    if not has_scratch_provider:
                        provider = known_prefix[:-1]
                    if not has_scratch_model:
                        model = raw[len(known_prefix):]
                    break
            else:
                # No known prefix -- treat the whole value as a model name
                if not has_scratch_model:
                    model = raw

    # 3. Fail closed on the resolved provider before any API call
    _validate_provider(provider)

    # 4. Fallback to provider default
    if model is None:
        model = _DEFAULT_MODELS.get(provider, "gpt-4o")

    return provider, model


# ---------------------------------------------------------------------------
# Rate-limit helper
# ---------------------------------------------------------------------------

def temp_sleep(seconds=0.1):
    time.sleep(seconds)


# ============================================================================
#  SECTION 1: CHAT-COMPLETIONS  (ChatGPT / GPT-4 / OpenRouter)
# ============================================================================

def ChatGPT_single_request(prompt, persona=None):
    temp_sleep()
    provider, model = _resolve_model(persona)
    client = _get_client(provider)

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        return completion.choices[0].message.content
    except Exception:
        print("ChatGPT ERROR")
        return "ChatGPT ERROR"


def GPT4_request(prompt, persona=None):
    temp_sleep()
    provider, model = _resolve_model(persona)
    client = _get_client(provider)

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        return completion.choices[0].message.content
    except Exception:
        print("ChatGPT ERROR")
        return "ChatGPT ERROR"


def ChatGPT_request(prompt, persona=None):
    provider, model = _resolve_model(persona)
    client = _get_client(provider)

    try:
        completion = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        return completion.choices[0].message.content
    except Exception:
        print("ChatGPT ERROR")
        return "ChatGPT ERROR"


def GPT4_safe_generate_response(
    prompt,
    example_output,
    special_instruction,
    repeat=3,
    fail_safe_response="error",
    func_validate=None,
    func_clean_up=None,
    verbose=False,
    persona=None,
):
    prompt = 'GPT-3 Prompt:\n"""\n' + prompt + '\n"""\n'
    prompt += f"Output the response to the prompt above in json. {special_instruction}\n"
    prompt += "Example output json:\n"
    prompt += '{"output": "' + str(example_output) + '"}'

    if verbose:
        print("CHAT GPT PROMPT")
        print(prompt)

    for i in range(repeat):
        try:
            curr_gpt_response = GPT4_request(prompt, persona=persona).strip()
            end_index = curr_gpt_response.rfind("}") + 1
            curr_gpt_response = curr_gpt_response[:end_index]
            curr_gpt_response = json.loads(curr_gpt_response)["output"]

            if func_validate(curr_gpt_response, prompt=prompt):
                return func_clean_up(curr_gpt_response, prompt=prompt)

            if verbose:
                print("---- repeat count: \n", i, curr_gpt_response)
                print(curr_gpt_response)
                print("~~~~")

        except Exception:
            pass

    return False


def ChatGPT_safe_generate_response(
    prompt,
    example_output,
    special_instruction,
    repeat=3,
    fail_safe_response="error",
    func_validate=None,
    func_clean_up=None,
    verbose=False,
    persona=None,
):
    prompt = '"""\n' + prompt + '\n"""\n'
    prompt += f"Output the response to the prompt above in json. {special_instruction}\n"
    prompt += "Example output json:\n"
    prompt += '{"output": "' + str(example_output) + '"}'

    if verbose:
        print("CHAT GPT PROMPT")
        print(prompt)

    for i in range(repeat):
        try:
            curr_gpt_response = ChatGPT_request(prompt, persona=persona).strip()
            end_index = curr_gpt_response.rfind("}") + 1
            curr_gpt_response = curr_gpt_response[:end_index]
            curr_gpt_response = json.loads(curr_gpt_response)["output"]

            if func_validate(curr_gpt_response, prompt=prompt):
                return func_clean_up(curr_gpt_response, prompt=prompt)

            if verbose:
                print("---- repeat count: \n", i, curr_gpt_response)
                print(curr_gpt_response)
                print("~~~~")

        except Exception:
            pass

    return False


def ChatGPT_safe_generate_response_OLD(
    prompt,
    repeat=3,
    fail_safe_response="error",
    func_validate=None,
    func_clean_up=None,
    verbose=False,
    persona=None,
):
    if verbose:
        print("CHAT GPT PROMPT")
        print(prompt)

    for i in range(repeat):
        try:
            curr_gpt_response = ChatGPT_request(prompt, persona=persona).strip()
            if func_validate(curr_gpt_response, prompt=prompt):
                return func_clean_up(curr_gpt_response, prompt=prompt)
            if verbose:
                print(f"---- repeat count: {i}")
                print(curr_gpt_response)
                print("~~~~")

        except Exception:
            pass

    print("FAIL SAFE TRIGGERED")
    return fail_safe_response


# ============================================================================
#  SECTION 2: LEGACY GPT-3 COMPLETIONS  (normalized to chat)
# ============================================================================

def GPT_request(prompt, gpt_parameter, persona=None):
    """Legacy completion-style request normalized to chat completions.

    OpenRouter does not support text-davinci models, so we convert the
    completion parameters to a chat-completions call internally.
    """
    temp_sleep()
    provider, model = _resolve_model(persona, fallback_engine=gpt_parameter.get("engine"))
    client = _get_client(provider)

    # Build chat messages from the raw prompt
    system_prompt = (
        "You are a helpful assistant. Always respond exactly as instructed. "
        "Output only the requested text with no extra commentary."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    # Map completion parameters to chat-completions parameters
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": gpt_parameter.get("temperature", 0),
        "max_tokens": gpt_parameter.get("max_tokens", 50),
        "top_p": gpt_parameter.get("top_p", 1),
    }

    stop_seq = gpt_parameter.get("stop")
    if stop_seq and stop_seq != [""]:
        kwargs["stop"] = stop_seq

    # frequency_penalty / presence_penalty only for models that support them
    fp = gpt_parameter.get("frequency_penalty")
    if fp is not None:
        kwargs["frequency_penalty"] = fp
    pp = gpt_parameter.get("presence_penalty")
    if pp is not None:
        kwargs["presence_penalty"] = pp

    try:
        completion = client.chat.completions.create(**kwargs)
        return completion.choices[0].message.content
    except Exception:
        print("TOKEN LIMIT EXCEEDED")
        return "TOKEN LIMIT EXCEEDED"


def generate_prompt(curr_input, prompt_lib_file):
    """Replace !<INPUT n>! placeholders in a prompt template with values."""
    if isinstance(curr_input, str):
        curr_input = [curr_input]
    curr_input = [str(i) for i in curr_input]

    with open(prompt_lib_file, "r") as f:
        prompt = f.read()
    for count, i in enumerate(curr_input):
        prompt = prompt.replace(f"!<INPUT {count}>!", i)
    if "<commentblockmarker>###</commentblockmarker>" in prompt:
        prompt = prompt.split("<commentblockmarker>###</commentblockmarker>")[1]
    return prompt.strip()


def safe_generate_response(
    prompt,
    gpt_parameter,
    repeat=5,
    fail_safe_response="error",
    func_validate=None,
    func_clean_up=None,
    verbose=False,
    persona=None,
):
    if verbose:
        print(prompt)

    for i in range(repeat):
        curr_gpt_response = GPT_request(prompt, gpt_parameter, persona=persona)
        if func_validate(curr_gpt_response, prompt=prompt):
            return func_clean_up(curr_gpt_response, prompt=prompt)
        if verbose:
            print("---- repeat count: ", i, curr_gpt_response)
            print(curr_gpt_response)
            print("~~~~")
    return fail_safe_response


# ============================================================================
#  SECTION 3: EMBEDDINGS
# ============================================================================

# ---------------------------------------------------------------------------
# Embedding model defaults per provider
# ---------------------------------------------------------------------------
_EMBEDDING_MODELS: dict[str, str] = {
    "openai": "text-embedding-ada-002",
    "openrouter": "openai/text-embedding-ada-002",
}


def get_embedding(text, model=None, persona=None):
    """Return embedding vector for ``text``.

    If ``model`` is not explicitly set, a provider-appropriate default
    is selected (e.g. ``openai/text-embedding-ada-002`` for OpenRouter).
    """
    text = text.replace("\n", " ")
    if not text:
        text = "this is blank"
    provider, _ = _resolve_model(persona)
    if model is None:
        model = _EMBEDDING_MODELS.get(provider, "text-embedding-ada-002")
    client = _get_client(provider)
    return client.embeddings.create(input=[text], model=model).data[0].embedding


# ============================================================================
#  SECTION 4: SELF-TEST
# ============================================================================

if __name__ == "__main__":
    gpt_parameter = {
        "engine": "text-davinci-003",
        "max_tokens": 50,
        "temperature": 0,
        "top_p": 1,
        "stream": False,
        "frequency_penalty": 0,
        "presence_penalty": 0,
        "stop": ['"'],
    }
    curr_input = ["driving to a friend's house"]
    prompt_lib_file = "prompt_template/test_prompt_July5.txt"
    prompt = generate_prompt(curr_input, prompt_lib_file)

    def __func_validate(gpt_response):
        if len(gpt_response.strip()) <= 1:
            return False
        if len(gpt_response.strip().split(" ")) > 1:
            return False
        return True

    def __func_clean_up(gpt_response):
        cleaned_response = gpt_response.strip()
        return cleaned_response

    output = safe_generate_response(
        prompt, gpt_parameter, 5, "rest", __func_validate, __func_clean_up, True
    )

    print(output)
