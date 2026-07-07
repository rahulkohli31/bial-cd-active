"""Zip-bomb pre-filter (U11) — the summed-uncompressed cap + malformed/ZIP64 rejection."""

from __future__ import annotations

import io
import zipfile

import pytest

from src.services.extract.zip_safety import (
    FileParseError,
    assert_zip_not_bomb,
    looks_like_zip,
)


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_looks_like_zip() -> None:
    assert looks_like_zip(_zip({"a.txt": b"hi"})) is True
    assert looks_like_zip(b"not a zip") is False
    assert looks_like_zip(b"PK") is False  # too short


def test_valid_zip_under_cap_passes() -> None:
    assert_zip_not_bomb(_zip({"a.txt": b"x" * 1000}))  # no raise (well under 300 MB)


def test_summed_uncompressed_over_cap_rejected() -> None:
    # A low cap makes a normal archive's real uncompressed sum exceed it → FILE_TOO_LARGE.
    data = _zip({"a.txt": b"x" * 5000, "b.txt": b"y" * 5000})
    with pytest.raises(FileParseError) as excinfo:
        assert_zip_not_bomb(data, max_uncompressed=1000)
    assert excinfo.value.status == 413
    assert excinfo.value.code == "FILE_TOO_LARGE"


def test_malformed_archive_rejected() -> None:
    # 32 bytes of garbage (>= EOCD_MIN) with no EOCD signature.
    with pytest.raises(FileParseError):
        assert_zip_not_bomb(b"\x00" * 32)


def test_too_small_buffer_is_noop() -> None:
    assert_zip_not_bomb(b"PK\x03\x04")  # < 22 bytes → structure gate owns it, no raise
