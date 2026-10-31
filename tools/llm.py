"""
LLM access with guardrails.

  - Gemini first, Groq as backup (a provider that fails is skipped for the
    rest of the call, so a 503 never stalls the agent)
  - Output must parse into a Pydantic schema
  - An optional validate() function checks the content (e.g. "only choose
    options that exist"); on failure the model gets the error and retries
"""
from __future__ import annotations

import os
import re
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMUnavailable(RuntimeError):
    """No provider produced a valid answer."""


def _extract_json(text: str) -> str:
    """Models sometimes wrap JSON in ``` fences or add a sentence; keep the object."""
    match = re.search(r"\{.*\}", text or "", re.S)
    return match.group(0) if match else (text or "")


def _gemini(system: str, prompt: str) -> tuple[str, str]:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise LLMUnavailable("GEMINI_API_KEY not set")
    from google import genai
    from google.genai import types

    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    resp = genai.Client(api_key=key).models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json",
            temperature=0.2,
        ),
    )
    return resp.text, f"gemini:{model}"


_groq_model_cache: str | None = None


def _pick_groq_model(client) -> str:
    """Use GROQ_MODEL if set, otherwise pick a capable text model from the live list."""
    global _groq_model_cache
    if os.getenv("GROQ_MODEL"):
        return os.environ["GROQ_MODEL"]
    if _groq_model_cache:
        return _groq_model_cache
    ids = [m.id for m in client.models.list().data]
    skip = ("whisper", "tts", "orpheus", "guard", "playai", "allam")
    text_ids = [i for i in ids if not any(x in i.lower() for x in skip)]
    for preferred in ("70b", "gpt-oss-120b", "gpt-oss", "llama", "qwen"):
        for i in text_ids:
            if preferred in i.lower():
                _groq_model_cache = i
                return i
    if text_ids:
        _groq_model_cache = text_ids[0]
        return text_ids[0]
    raise LLMUnavailable("No Groq text model available")


def _groq(system: str, prompt: str) -> tuple[str, str]:
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise LLMUnavailable("GROQ_API_KEY not set")
    from groq import Groq

    client = Groq(api_key=key)
    model = _pick_groq_model(client)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system or "Respond in JSON."},
                  {"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    return resp.choices[0].message.content, f"groq:{model}"


PROVIDERS = [("gemini", _gemini), ("groq", _groq)]


def _log(log, entry):
    if log is not None:
        log.append(entry)


def generate_json(prompt: str, schema: type[T], *, system: str = "",
                  validate: Callable[[T], None] | None = None,
                  max_attempts: int = 3, log: list | None = None) -> T:
    feedback = ""
    down: dict[str, str] = {}
    used = "none"

    for attempt in range(1, max_attempts + 1):
        full_prompt = prompt
        if feedback:
            full_prompt += (f"\n\nYOUR PREVIOUS ANSWER WAS REJECTED: {feedback}\n"
                            "Fix this and return only valid JSON.")

        text = None
        for name, fn in PROVIDERS:
            if name in down:
                continue
            try:
                text, used = fn(system, full_prompt)
                break
            except Exception as e:
                down[name] = str(e)[:150]
                _log(log, {"event": "llm_provider_failed", "provider": name,
                           "error": str(e)[:150]})
        if text is None:
            raise LLMUnavailable(f"All LLM providers failed: {down}")

        try:
            obj = schema.model_validate_json(_extract_json(text))
            if validate:
                validate(obj)          # raises ValueError with a reason
            _log(log, {"event": "llm_ok", "provider": used, "attempt": attempt})
            return obj
        except (ValidationError, ValueError) as e:
            feedback = str(e)[:500]
            _log(log, {"event": "llm_output_rejected", "provider": used,
                       "attempt": attempt, "reason": feedback[:200]})

    raise LLMUnavailable(f"No valid answer after {max_attempts} attempts: {feedback[:200]}")
