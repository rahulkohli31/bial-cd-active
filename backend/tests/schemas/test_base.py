"""`CamelModel` — the single camelCase base. Serializes snake_case fields to
camelCase on the wire (`resets_at` -> `resetsAt`) and, with `populate_by_name`,
accepts both spellings on input."""

from __future__ import annotations

from src.schemas import CamelModel


class _Sample(CamelModel):
    resets_at: str
    used_tokens: int


def test_serializes_snake_to_camel() -> None:
    dumped = _Sample(resets_at="midnight", used_tokens=5).model_dump(by_alias=True)
    assert dumped == {"resetsAt": "midnight", "usedTokens": 5}


def test_json_dump_uses_camel_aliases() -> None:
    payload = _Sample(resets_at="midnight", used_tokens=5).model_dump_json(by_alias=True)
    assert '"resetsAt"' in payload
    assert '"usedTokens"' in payload
    assert "resets_at" not in payload


def test_accepts_snake_case_field_names_on_input() -> None:
    # populate_by_name: construction by the Python field name still validates.
    model = _Sample(resets_at="midnight", used_tokens=5)
    assert model.resets_at == "midnight"
    assert model.used_tokens == 5


def test_accepts_camel_case_aliases_on_input() -> None:
    model = _Sample.model_validate({"resetsAt": "midnight", "usedTokens": 5})
    assert model.resets_at == "midnight"
    assert model.used_tokens == 5


def test_single_word_fields_are_camel_noop() -> None:
    # `to_camel` is a no-op on single-word fields — the wire key is unchanged.
    class _OneWord(CamelModel):
        status: str

    assert _OneWord(status="ok").model_dump(by_alias=True) == {"status": "ok"}
