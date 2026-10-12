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
