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

_clients: dict[str, OpenAI] = {}


def _get_client(provider: str) -> OpenAI:
    """Return (and cache) an OpenAI-compatible client for the given provider."""
    _validate_provider(provider)
    if provider in _clients:
        return _clients[provider]

    if provider == "openrouter":
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_api_key,
            timeout=llm_api_timeout_seconds,
            max_retries=0,
            default_headers={
                "HTTP-Referer": openrouter_referer,
                "X-Title": openrouter_title,
                "X-OpenRouter-Title": openrouter_title,
                "X-OpenRouter-Categories": openrouter_categories,
            },
        )
    else:
        client = OpenAI(
            api_key=openai_api_key,
            timeout=llm_api_timeout_seconds,
            max_retries=0,
        )

    _clients[provider] = client
    return client


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

    if persona is not None and llm_persona_models:
        name = getattr(persona.scratch, "name", None) if hasattr(persona, "scratch") else None
        if name and name in llm_persona_models:
            raw = llm_persona_models[name]
            for known_prefix in ("openai:", "openrouter:"):
                if raw.startswith(known_prefix):
                    if not has_scratch_provider:
                        provider = known_prefix[:-1]
                    if not has_scratch_model:
                        model = raw[len(known_prefix):]
                    break
            else:
                if not has_scratch_model:
                    model = raw

    _validate_provider(provider)

    if model is None:
        model = _DEFAULT_MODELS.get(provider, "gpt-4o")

    return provider, model


def temp_sleep(seconds=0.1):
    time.sleep(seconds)


def _call_with_retries(provider, operation, func):
    """Run ``func`` with bounded exponential retries.

    ``LLM_API_RETRIES`` controls the number of retries after the initial
    attempt; backoff starts at ``LLM_API_RETRY_BASE_SECONDS`` and doubles per
    retry. Ordinary provider and network exceptions are retried, while
    ``KeyboardInterrupt`` and ``SystemExit`` always propagate immediately.
    After exhaustion a sanitized ``LLMProviderError`` names the provider,
    operation, and total attempt count without any prompt, key, or response
    body.
    """
    total_attempts = llm_api_retries + 1
    last_exception = None
    for attempt in range(total_attempts):
        try:
            return func()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            last_exception = exc
            if attempt < total_attempts - 1:
                temp_sleep(llm_api_retry_base_seconds * (2 ** attempt))
    raise LLMProviderError(
        provider=provider, operation=operation, attempts=total_attempts
    ) from last_exception


class _MalformedProviderResponse(Exception):
    """Internal signal that a provider chat response failed validation."""


def _chat_completion_content(client, **kwargs):
    """Create a chat completion and return its validated string content.

    Missing or empty choices, a missing message, and None, non-string, or
    whitespace-only content raise ``_MalformedProviderResponse`` so that
    ``_call_with_retries`` treats the response as retryable. Valid nonempty
    string content is returned unchanged.
    """
    completion = client.chat.completions.create(**kwargs)
    try:
        content = completion.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        raise _MalformedProviderResponse from None
    if not isinstance(content, str) or not content.strip():
        raise _MalformedProviderResponse from None
    return content


def ChatGPT_single_request(prompt, persona=None):
    temp_sleep()
    provider, model = _resolve_model(persona)
    client = _get_client(provider)
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    kwargs.update(reasoning_effort_kwargs(provider, llm_reasoning_effort))
    return _call_with_retries(
        provider,
        "chat",
        lambda: _chat_completion_content(client, **kwargs),
    )


def GPT4_request(prompt, persona=None):
    temp_sleep()
    provider, model = _resolve_model(persona)
    client = _get_client(provider)
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    kwargs.update(reasoning_effort_kwargs(provider, llm_reasoning_effort))
    return _call_with_retries(
        provider,
        "chat",
        lambda: _chat_completion_content(client, **kwargs),
    )


def ChatGPT_request(prompt, persona=None):
    provider, model = _resolve_model(persona)
    client = _get_client(provider)
    kwargs: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    kwargs.update(reasoning_effort_kwargs(provider, llm_reasoning_effort))
    return _call_with_retries(
        provider,
        "chat",
        lambda: _chat_completion_content(client, **kwargs),
    )


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

        except LLMProviderError:
            raise
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

        except LLMProviderError:
            raise
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

        except LLMProviderError:
            raise
        except Exception:
            pass

    print("FAIL SAFE TRIGGERED")
    return fail_safe_response


def GPT_request(prompt, gpt_parameter, persona=None):
    """Legacy completion-style request normalized to chat completions.

    OpenRouter does not support text-davinci models, so we convert the
    completion parameters to a chat-completions call internally. The legacy
    output limits ``max_tokens`` and ``stop`` are intentionally dropped;
    every other legacy control (temperature, top_p, frequency/presence
    penalties) still applies.
    """
    temp_sleep()
    provider, model = _resolve_model(persona, fallback_engine=gpt_parameter.get("engine"))
    client = _get_client(provider)

    system_prompt = (
        "You are a helpful assistant. Always respond exactly as instructed. "
        "Output only the requested text with no extra commentary."
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": gpt_parameter.get("temperature", 0),
        "top_p": gpt_parameter.get("top_p", 1),
    }

    fp = gpt_parameter.get("frequency_penalty")
    if fp is not None:
        kwargs["frequency_penalty"] = fp
    pp = gpt_parameter.get("presence_penalty")
    if pp is not None:
        kwargs["presence_penalty"] = pp

    kwargs.update(reasoning_effort_kwargs(provider, llm_reasoning_effort))

    return _call_with_retries(
        provider,
        "chat",
        lambda: _chat_completion_content(client, **kwargs),
    )


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


_EMBEDDING_MODELS: dict[str, str] = {
    "openai": "text-embedding-ada-002",
    "openrouter": "openai/text-embedding-ada-002",
}


def _resolve_embedding_model(provider: str, explicit: str | None = None) -> str:
    """Resolve the embedding model for ``provider``.

    Precedence: explicit argument > per-provider ``LLM_EMBEDDING_MODELS``
    > global ``LLM_EMBEDDING_MODEL`` > provider-safe default.

    The chat model (``LLM_MODEL``) is never reused as an embedding model.
    """
    if explicit:
        return explicit
    if provider in llm_embedding_models:
        return llm_embedding_models[provider]
    if llm_embedding_model:
        return llm_embedding_model
    return _EMBEDDING_MODELS.get(provider, "text-embedding-ada-002")


def get_embedding(text, model=None, persona=None):
    """Return embedding vector for ``text``.

    If ``model`` is not explicitly set, the embedding model is resolved
    from per-provider ``LLM_EMBEDDING_MODELS``, then global
    ``LLM_EMBEDDING_MODEL``, then a provider-appropriate safe default
    (e.g. ``openai/text-embedding-ada-002`` for OpenRouter). The chat
    model is never used for embeddings.
    """
    text = text.replace("\n", " ")
    if not text:
        text = "this is blank"
    provider, _ = _resolve_model(persona)
    model = _resolve_embedding_model(provider, explicit=model)
    client = _get_client(provider)
    kwargs: dict = {
        "input": [text],
        "model": model,
    }
    if provider == "openrouter":
        kwargs["encoding_format"] = "float"
    response = _call_with_retries(
        provider,
        "embedding",
        lambda: client.embeddings.create(**kwargs),
    )
    return response.data[0].embedding


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
