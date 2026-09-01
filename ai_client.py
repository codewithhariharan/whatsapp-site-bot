"""Claude client construction, shared by the parser and the /ask handler.

Vertex is the intended production path: credentials come from the VM's service
account via ADC, so no API key is stored on disk. It requires the GCP project to
have been granted access to Anthropic models in the Vertex AI Model Garden,
which on an org-managed project is an approval someone else has to grant. Until
that lands the same code runs against the direct Anthropic API by setting
ANTHROPIC_API_KEY and leaving VERTEX_PROJECT_ID empty.

Selection is by configuration, not by trial: VERTEX_PROJECT_ID wins if set,
otherwise ANTHROPIC_API_KEY. Falling back automatically on an API error would
turn a missing Model Garden grant into a silent switch to a billing path nobody
chose.
"""
import logging

from config import settings

logger = logging.getLogger("site_bot")

USE_VERTEX = bool(settings.VERTEX_PROJECT_ID)

_client = None


def get_client():
    """The Anthropic client, built on first use.

    Lazy for the same reason the database engine is: importing this module must
    not require credentials, or the test suite cannot collect.
    """
    global _client
    if _client is not None:
        return _client

    if USE_VERTEX:
        from anthropic import AnthropicVertex

        _client = AnthropicVertex(
            project_id=settings.VERTEX_PROJECT_ID,
            region=settings.VERTEX_REGION,
        )
        logger.info("Claude via Vertex AI (region=%s)", settings.VERTEX_REGION)
    else:
        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "No Claude credentials: set VERTEX_PROJECT_ID for Vertex, "
                "or ANTHROPIC_API_KEY for the direct API."
            )
        from anthropic import Anthropic

        _client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        logger.info("Claude via the direct Anthropic API")

    return _client


def model_id(name: str) -> str:
    """Translate a configured model id for the active backend.

    The two backends spell dated snapshots differently, and the wrong spelling
    is a 404 rather than anything self-explanatory:

        Vertex        claude-haiku-4-5@20251001
        direct API    claude-haiku-4-5

    Current-generation models are unsuffixed on both, so they pass through
    untouched. Configure the Vertex form; this strips the suffix when talking to
    the direct API.
    """
    if USE_VERTEX:
        return name
    return name.split("@", 1)[0]
