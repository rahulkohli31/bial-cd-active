"""The reader a chat's session runs is the one the sandbox image ships, byte for byte.

Reads `sandbox/`, which the gates image does not copy in; deselected there by name.
"""

from __future__ import annotations

from pathlib import Path

from src.services.analysis.placement import READER

_SHIPPED = Path(__file__).resolve().parents[4] / "sandbox" / "scripts" / "read_attachment.py"


def test_the_session_reader_is_the_sandbox_reader_byte_for_byte() -> None:
    assert READER == _SHIPPED.read_bytes()
