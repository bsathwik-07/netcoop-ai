"""Optional LLM garnish: a readable narrative, and a last-resort field-name mapper.

The diagnosis never depends on this module. The root cause, confidence and evidence are decided
by the deterministic engine; the LLM only (a) rewrites the finished report as prose and
(b) suggests a canonical name for a telemetry field that aliases and fuzzy matching could not
map (such values are tagged quality=imputed, method=llm_suggested, i.e. down-weighted).

Opt-in: set NETCOOP_LLM=1 and provide Anthropic credentials (ANTHROPIC_API_KEY or an
`ant auth login` profile). Model: NETCOOP_LLM_MODEL (default claude-sonnet-5-5). Any failure,
timeout or refusal silently falls back to the template narrative / no mapping.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any

from netcoop.protocol.payloads import DiagnosticReport

logger = logging.getLogger("netcoop.llm")
DEFAULT_MODEL = "claude-sonnet-5-5"
TIMEOUT_S = 20.0
# Models that accept the server-side refusal fallback in its "default" form on the Claude API.
FALLBACK_MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-1"}

NARRATIVE_SYSTEM = (
    "You write the summary paragraph of a network incident report for campus network operators. "
    "You receive the finished diagnostic report as JSON. Restate it in 4-6 plain sentences: what is "
    "wrong and where, the key evidence with its numbers, why the main alternative is less likely, "
    "how confident the system is and why, and what to do or check next. Use only facts in the JSON. "
    "Never change, add or soften the status, the root cause, the root device or the confidence value. "
    "If the status is insufficient_evidence or failed, say clearly that no definitive root cause is "
    "claimed. Output only the paragraph."
)


def llm_enabled() -> bool:
    return os.environ.get("NETCOOP_LLM", "").strip().lower() in ("1", "true", "on", "yes")


def _model() -> str:
    return os.environ.get("NETCOOP_LLM_MODEL", DEFAULT_MODEL)


@lru_cache(maxsize=1)
def _client() -> Any:
    import anthropic

    return anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=1)


def _ask(system: str, user: str, max_tokens: int) -> str | None:
    """One short request; returns the text or None on any failure (the caller falls back)."""
    import anthropic

    model = _model()
    kwargs: dict[str, Any] = {
        "model": model, "max_tokens": max_tokens, "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": "low"},
    }
    try:
        if model in FALLBACK_MODELS:
            response = _client().beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        else:
            response = _client().messages.create(**kwargs)
    except anthropic.APIConnectionError as exc:
        logger.warning("LLM unreachable, using template: %s", exc)
        return None
    except anthropic.APIStatusError as exc:
        logger.warning("LLM request failed (%s), using template: %s", exc.status_code, exc.message)
        return None
    except Exception as exc:  # missing credentials, etc.: the LLM is garnish, never a failure
        logger.warning("LLM unavailable, using template: %s", exc)
        return None
    if response.stop_reason == "refusal":
        return None
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text or None


def _consistent(narrative: str, report: DiagnosticReport) -> bool:
    """Reject a narrative that drops the decided device or contradicts the confidence."""
    lowered = narrative.lower()
    if report.root_device and report.status.value != "normal" and report.root_device.lower() not in lowered:
        return False
    return f"{report.confidence:.2f}" in narrative or report.confidence_category.value in lowered


def llm_narrator(report: DiagnosticReport) -> str | None:
    payload = report.model_dump(mode="json", exclude={"narrative", "data_quality", "agent_findings_summary"})
    payload["supporting_evidence"] = payload["supporting_evidence"][:6]
    text = _ask(NARRATIVE_SYSTEM, json.dumps(payload), max_tokens=1200)
    if text is None or not _consistent(text, report):
        return None
    return text + " [narrative written by an LLM from the deterministic report]"


def llm_field_mapper(field: str, candidates: list[str]) -> str | None:
    system = ("Map a network telemetry field name to one canonical metric name. Reply with exactly one name "
              "from the list, or NONE if no name clearly matches.")
    text = _ask(system, f"Field: {field!r}\nCanonical names: {', '.join(candidates)}", max_tokens=64)
    if not text:
        return None
    answer = text.strip().split()[0].strip("`'\".,")
    return answer if answer in candidates else None


def optional_hooks() -> dict[str, Any]:
    """Keyword arguments for build_agents: empty unless NETCOOP_LLM is enabled."""
    if not llm_enabled():
        return {}
    return {"narrator": llm_narrator, "field_mapper": llm_field_mapper}
