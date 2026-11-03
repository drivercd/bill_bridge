"""Run-wide settings. Per-client billing rules live in data/clients.json."""

import os

DEFAULT_MODEL = "claude-opus-5-5"
MAX_AI_ATTEMPTS = 3
CONTRACT_SECTIONS_TO_RETRIEVE = 3


def model_name() -> str:
    return os.environ.get("BILLBRIDGE_MODEL", DEFAULT_MODEL)


def llm_mode() -> str:
    """'mock' (default, no API key needed) or 'live' (calls the Anthropic API)."""
    return os.environ.get("BILLBRIDGE_LLM_MODE", "mock").lower()


# M3: human checkpoint and durable execution
REVIEW_WINDOW_HOURS = 48          # supervisor decision window (2 business days, simplified to 48 h)
APPROVAL_VALIDITY_HOURS = 72      # an approval must be executed within this window or be re-reviewed
MAX_COMMIT_ATTEMPTS = 3           # invoicing API attempts per commit, same idempotency key
COMMIT_BACKOFF_SECONDS = (2, 4, 8)
