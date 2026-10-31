"""Generated documentation stays in sync with the code."""

from __future__ import annotations

from netcoop.config import REPO_ROOT
from netcoop.protocol import protocol_doc
from netcoop.protocol.vocabulary import ErrorCode, MessageType


def test_protocol_md_is_up_to_date() -> None:
    committed = (REPO_ROOT / "PROTOCOL.md").read_text(encoding="utf-8")
    assert committed == protocol_doc.build(), "PROTOCOL.md is stale: run `netcoop schemas`"


def test_protocol_md_covers_every_type_and_error() -> None:
    text = protocol_doc.build()
    assert all(f"### {t.value}" in text for t in MessageType)
    assert all(f"`{c.value}`" in text for c in ErrorCode)
    assert text.count("```mermaid") >= 4  # normal run, challenge round, timeout, version mismatch
    assert "| `correlation_id` |" in text and "conversation-id" in text


def test_readme_documents_the_confidence_formula_and_safety_note() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "P(top)" in readme and "SIMULATION ONLY" in readme.upper()
