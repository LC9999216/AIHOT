"""Production construction for the grounded LLM extraction boundary."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Optional

from .client import LlmConfigurationError, OpenAICompatibleStructuredClient
from .structured_extractor import GroundedExtractor


_REQUIRED = (
    "HUNTER_LLM_BASE_URL",
    "HUNTER_LLM_API_KEY",
    "HUNTER_LLM_MODEL",
)


def build_grounded_extractor_from_env(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[GroundedExtractor]:
    """Build the real extractor when fully configured; otherwise fail closed."""
    values = os.environ if env is None else env
    configured = {name: str(values.get(name, "")).strip() for name in _REQUIRED}
    if not any(configured.values()):
        return None
    missing = [name for name, value in configured.items() if not value]
    if missing:
        raise LlmConfigurationError(
            "incomplete LLM configuration; missing " + ", ".join(missing)
        )
    try:
        timeout = float(values.get("HUNTER_LLM_TIMEOUT_SECONDS", "30"))
        max_bytes = int(values.get("HUNTER_LLM_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))
    except (TypeError, ValueError):
        raise LlmConfigurationError(
            "HUNTER_LLM_TIMEOUT_SECONDS and HUNTER_LLM_MAX_RESPONSE_BYTES must be numeric"
        ) from None
    client = OpenAICompatibleStructuredClient(
        base_url=configured["HUNTER_LLM_BASE_URL"],
        api_key=configured["HUNTER_LLM_API_KEY"],
        model=configured["HUNTER_LLM_MODEL"],
        timeout=timeout,
        max_response_bytes=max_bytes,
    )
    return GroundedExtractor(
        llm=client,
        max_repairs=1,
        secret_values=[configured["HUNTER_LLM_API_KEY"]],
    )


__all__ = ["build_grounded_extractor_from_env"]
