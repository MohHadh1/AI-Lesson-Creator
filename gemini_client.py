"""One place that talks to the Gemini API.

Lesson generation and the two quality-control reviews all call
``generate_json``. They do not share prompts. The API key is read from
``GEMINI_API_KEY`` and is never printed.
"""

from __future__ import annotations

import json
import os
import re
from time import sleep

from dotenv import load_dotenv
from google import genai
from google.genai import types

from errors import LessonCreatorError, public_error_message

# gemini-3.8-flash is the current default. Earlier names are tried only
# when the first model is down.
DEFAULT_MODEL = "gemini-3.8-flash"
_FALLBACK_MODELS = (
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
)


def model_name() -> str:
    """Return the Gemini model id to call.

    Set ``GEMINI_MODEL`` in ``.env`` to override the default. Blank or
    odd values fall back to ``DEFAULT_MODEL``.
    """
    load_dotenv()
    chosen = os.getenv("GEMINI_MODEL", "").strip()
    if not chosen or any(char.isspace() for char in chosen):
        return DEFAULT_MODEL
    return chosen


def _models_to_try() -> list[str]:
    """Prefer the configured model, then other current Gemini Flash models."""
    names = [model_name()]
    for name in _FALLBACK_MODELS:
        if name not in names:
            names.append(name)
    return names


def require_api_key() -> str:
    """Return the Gemini API key or raise a clear error. Never log it."""
    load_dotenv()
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise LessonCreatorError(
            "GEMINI_API_KEY is missing. Add it to the .env file in the project folder, then restart the app."
        )
    return key


# The SDK closes its HTTP client when the Client object is garbage-collected.
# A temporary Client() would close that connection in the middle of a request,
# so the app keeps one client for the process.
_CLIENT: genai.Client | None = None


def _client() -> genai.Client:
    """Return a live Gemini client. The key is never written to disk."""
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = genai.Client(
            api_key=require_api_key(),
            http_options=types.HttpOptions(timeout=120_000),
        )
    return _CLIENT


def parse_json_object(text: str) -> dict:
    """Parse a JSON object from a model response.

    Models sometimes wrap JSON in a markdown fence or add a sentence
    before the object. This accepts those shapes and still requires an object.
    """
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("The model did not return JSON.")
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError("The model returned JSON that could not be read.") from exc
    if not isinstance(value, dict):
        raise ValueError("The model returned JSON that was not an object.")
    return value


def _response_text(response: object) -> str:
    """Read the text the model actually wrote, skipping an empty reply."""
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    chunks: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "thought", False):
                continue
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text.strip():
                chunks.append(part_text.strip())
    if chunks:
        return "\n".join(chunks)
    raise LessonCreatorError("The model returned an empty response. Try again.")


def _friendly_api_error(exc: Exception, model: str) -> str:
    """Map an API failure to a sentence that does not contain the key."""
    raw = public_error_message(exc).lower()
    if "api key" in raw or "permission" in raw or "unauthenticated" in raw or "401" in raw:
        return (
            "The Gemini API rejected the request. Check that GEMINI_API_KEY in the .env file is valid, then try again."
        )
    if "not found" in raw or "404" in raw or "model" in raw and "invalid" in raw:
        return (
            f"The model '{model}' is not available for this API key. "
            "Set GEMINI_MODEL in .env to a supported model such as gemini-3.5-flash."
        )
    if "429" in raw or "resource_exhausted" in raw or "quota" in raw:
        return "The Gemini API rate limit was reached. Wait a moment, then retry this step."
    if "timeout" in raw or "deadline" in raw:
        return "The Gemini request timed out. Retry this step."
    return f"The Gemini request failed: {public_error_message(exc)}"


def _try_another_model(message: str) -> bool:
    """True when a different Gemini model might still answer."""
    return any(
        token in message
        for token in ("404", "not found", "503", "unavailable", "high demand")
    )


def _rate_limited(message: str) -> bool:
    return any(token in message for token in ("429", "resource_exhausted", "quota"))


def _send(
    *,
    model: str,
    system: str,
    user: str,
    temperature: float,
    max_output_tokens: int,
    json_mode: bool,
    use_thinking: bool,
) -> str:
    """Send one generateContent request and return the reply text."""
    config_kwargs: dict = {
        "system_instruction": system,
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
    }
    if json_mode:
        config_kwargs["response_mime_type"] = "application/json"
    if use_thinking:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
    client = _client()
    response = client.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(**config_kwargs),
    )
    return _response_text(response)


def _call_model(
    *,
    system: str,
    user: str,
    temperature: float,
    max_output_tokens: int,
    json_mode: bool,
    limit_thinking: bool,
) -> str:
    """Send a request, switching models when one is down or overloaded."""
    last_error: Exception | None = None
    for model in _models_to_try():
        thinking_options = (True, False) if limit_thinking else (False,)
        move_to_next_model = False
        for use_thinking in thinking_options:
            if move_to_next_model:
                break
            for attempt in (1, 2):
                try:
                    return _send(
                        model=model,
                        system=system,
                        user=user,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                        json_mode=json_mode,
                        use_thinking=use_thinking,
                    )
                except LessonCreatorError:
                    raise
                except Exception as exc:
                    last_error = exc
                    message = str(exc).lower()
                    if use_thinking and "thinking" in message:
                        break
                    if attempt == 1 and _rate_limited(message):
                        sleep(8)
                        continue
                    if _rate_limited(message):
                        # Attempt 2 also hit a rate limit. Move to the next
                        # fallback model rather than stopping entirely.
                        move_to_next_model = True
                        break
                    if _try_another_model(message):
                        move_to_next_model = True
                        break
                    raise LessonCreatorError(_friendly_api_error(exc, model)) from exc
    raise LessonCreatorError(
        "The Gemini API rate limit was reached on all available models. "
        "Wait a moment, then retry this step."
    ) from last_error


def generate_json(
    *,
    system: str,
    user: str,
    temperature: float,
    max_output_tokens: int = 8192,
) -> dict:
    """Ask Gemini for one JSON object.

    A second call is made only when the first reply is not valid JSON.
    """
    text = _call_model(
        system=system,
        user=user,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        json_mode=True,
        limit_thinking=True,
    )
    try:
        return parse_json_object(text)
    except ValueError:
        repair_request = (
            user
            + "\n\nYour previous reply was not a JSON object. Reply again with only the JSON object."
        )
        repaired = _call_model(
            system=system,
            user=repair_request,
            temperature=0.1,
            max_output_tokens=max_output_tokens,
            json_mode=False,
            limit_thinking=True,
        )
        try:
            return parse_json_object(repaired)
        except ValueError as exc:
            raise LessonCreatorError(
                "The model did not return readable JSON. Retry this step."
            ) from exc
