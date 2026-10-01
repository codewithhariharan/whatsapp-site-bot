"""The one place the bot talks to a model: Gemini, through Vertex AI.

Every parser and /ask path goes through `generate()`, so the SDK, the model
names and the error handling live here rather than being repeated five times.

There is no API key. The client authenticates as the VM's service account via
Application Default Credentials — from inside the container that means the GCE
metadata server — so there is no secret to store, rotate or leak.

Two Gemini behaviours shape this module:

  - Thinking tokens count against max_output_tokens. Current Flash models think
    by default, so a cap sized for the visible reply ("400 tokens is a SELECT
    plus a line of reasoning") could be spent thinking and come back empty.
    Callers state the reply they expect; the allowance for thinking is added on
    top here. The knob that limits thinking differs between model generations
    (a token budget on 2.5, a level on 3.x) and the wrong one is a 400 on every
    call, so it is set only where it is known — see `_thinking_config()`.
  - Rate limiting is a 429 ClientError, not its own exception class, so "is
    this worth retrying" is a question about the error, not its type. See
    `is_transient()`.
"""

import logging

import httpx
from google import genai
from google.genai import errors, types

from config import settings

logger = logging.getLogger("site_bot")

# Credentials are resolved on the first call, not here, so importing this with
# no Google credentials (the tests, a laptop) is fine.
client = genai.Client(
    vertexai=True,
    project=settings.GOOGLE_CLOUD_PROJECT,
    location=settings.GOOGLE_CLOUD_LOCATION,
)

# Parsing every post and rendering small /ask answers: high volume, simple work.
FAST_MODEL = settings.GEMINI_FAST_MODEL
# Writing SQL and laying out long answers: the calls where quality shows.
SMART_MODEL = settings.GEMINI_SMART_MODEL

# Headroom for thinking on top of the visible reply. Only tokens actually
# generated are billed, so a generous ceiling costs nothing when unused.
_THINKING_ALLOWANCE = 8192


def _thinking_config(model: str) -> types.ThinkingConfig | None:
    """Hold thinking to the allowance, where the model takes a token budget.

    The allowance is only headroom if thinking stays inside it. Left on
    automatic, 2.5 Flash can think for up to 24k tokens: laying out a 31-row
    day it spent the allowance AND the room meant for the reply, and the
    listing stopped a third of the way down. 3.x models take a level instead
    of a budget and reject this field, so they are left at their default.
    """
    if model.startswith("gemini-2.5"):
        return types.ThinkingConfig(thinking_budget=_THINKING_ALLOWANCE)
    return None


class EmptyReply(RuntimeError):
    """The model returned no text — cut off by the cap, or blocked."""


def generate(prompt: str, *, model: str, max_tokens: int,
             system: str | None = None, json_output: bool = False,
             cut_off_note: str | None = None) -> str:
    """Send one prompt, return the reply text.

    `system` goes first so it forms a stable prefix: Gemini caches repeated
    prefixes implicitly, so the fixed schema and rules in /ask are billed at
    the cached rate on every call after the first. Keep volatile text (the
    date, the question) in `prompt`, never in `system`.

    `json_output` asks the API for a JSON body, which removes the stray code
    fences and commentary a plain-text reply sometimes carried. The callers
    still parse defensively.

    `cut_off_note` is appended when the reply stopped at the token cap. A list
    that ends mid-way looks complete to whoever reads it, so a caller sending
    the text to people passes the sentence that says it is not.
    """
    config = types.GenerateContentConfig(
        system_instruction=system,
        max_output_tokens=max_tokens + _THINKING_ALLOWANCE,
        response_mime_type="application/json" if json_output else None,
        thinking_config=_thinking_config(model),
        # No tools are passed, but say so: it keeps the SDK from logging an
        # automatic-function-calling notice on every call.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    response = client.models.generate_content(
        model=model, contents=prompt, config=config,
    )
    text = response.text
    reason = None
    if response.candidates:
        reason = response.candidates[0].finish_reason
    if not text or not text.strip():
        raise EmptyReply(f"{model} returned no text (finish_reason={reason})")
    text = text.strip()
    if getattr(reason, "name", reason) == "MAX_TOKENS":
        usage = getattr(response, "usage_metadata", None)
        logger.warning(
            "%s reply stopped at the token cap (%d chars kept, %s thinking tokens)",
            model, len(text), getattr(usage, "thoughts_token_count", "?"))
        if cut_off_note:
            text += cut_off_note
    return text


def is_transient(exc: BaseException) -> bool:
    """True for failures worth waiting out: outages, overload, rate limits."""
    if isinstance(exc, errors.ServerError):
        return True
    if isinstance(exc, errors.ClientError):
        return exc.code in (408, 429)
    return isinstance(exc, httpx.TransportError)
