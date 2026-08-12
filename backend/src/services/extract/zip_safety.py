"""Zip-bomb pre-filter for untrusted OOXML uploads (R17; ports `server/zip-safety.js`).

A dependency-free central-directory scan that SUMS each entry's self-declared uncompressed size
and rejects the archive before any parse if the total exceeds a cap — bounding the *actual*
resource, not a declared entry count (the untrusted-file-parsing learning). It is a pre-filter,
NOT a hard guarantee: the declared sizes are attacker-controllable, so the real backstop is the
parser's own streaming bounds (openpyxl read-only + row cap) and the container's memory limit.
ZIP64 is refused outright (we don't parse the ZIP64 records).
"""

from __future__ import annotations

import struct

# ~300 MB summed-uncompressed ceiling (Express `MAX_DECOMPRESSED_BYTES`). A fixed safety bound.
MAX_DECOMPRESSED_BYTES = 300 * 1024 * 1024

_ZIP_LOCAL_SIG = b"\x50\x4b\x03\x04"  # PK\x03\x04
_EOCD_SIG = 0x06054B50
_CDH_SIG = 0x02014B50
_EOCD_MIN = 22
_MAX_COMMENT = 0xFFFF


class FileParseError(Exception):
    """A rejected/malformed archive. `status` maps to an HTTP code (413 for too-large)."""

    def __init__(self, message: str, *, status: int = 400, code: str = "FILE_PARSE_ERROR") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def looks_like_zip(data: bytes) -> bool:
    """True iff `data` starts with the ZIP local-file-header signature (PK\\x03\\x04)."""
    return len(data) >= 4 and data[:4] == _ZIP_LOCAL_SIG


def _find_eocd(data: bytes) -> int:
    # Scan backward over the last 22+64KB for the End-Of-Central-Directory signature.
    lowest = max(0, len(data) - (_EOCD_MIN + _MAX_COMMENT))
    for offset in range(len(data) - _EOCD_MIN, lowest - 1, -1):
        if struct.unpack_from("<I", data, offset)[0] == _EOCD_SIG:
            return offset
    raise FileParseError("Malformed archive (no ZIP end-of-central-directory).")


def assert_zip_not_bomb(data: bytes, max_uncompressed: int = MAX_DECOMPRESSED_BYTES) -> None:
    """Reject the archive if its summed declared uncompressed size exceeds `max_uncompressed`
    (or it is malformed / ZIP64). A no-op for a too-small buffer (the structure gate owns it)."""
    if len(data) < _EOCD_MIN:
        return
    eocd = _find_eocd(data)
    cd_count = struct.unpack_from("<H", data, eocd + 10)[0]
    cd_offset = struct.unpack_from("<I", data, eocd + 16)[0]
    if cd_count == 0xFFFF or cd_offset == 0xFFFFFFFF:
        raise FileParseError(
            "File is too large to parse safely (ZIP64).", status=413, code="FILE_TOO_LARGE"
        )

    total = 0
    pointer = cd_offset
    for _ in range(cd_count):
        if pointer + 46 > len(data) or struct.unpack_from("<I", data, pointer)[0] != _CDH_SIG:
            raise FileParseError("Malformed archive (bad ZIP central directory).")
        uncompressed = struct.unpack_from("<I", data, pointer + 24)[0]
        if uncompressed == 0xFFFFFFFF:
            raise FileParseError(
                "File is too large to parse safely (ZIP64 entry).",
                status=413,
                code="FILE_TOO_LARGE",
            )
        total += uncompressed
        if total > max_uncompressed:
            megabytes = round(max_uncompressed / (1024 * 1024))
            raise FileParseError(
                f"File is too large when decompressed (over {megabytes} MB). "
                "Rejected to protect the server.",
                status=413,
                code="FILE_TOO_LARGE",
            )
        name_len = struct.unpack_from("<H", data, pointer + 28)[0]
        extra_len = struct.unpack_from("<H", data, pointer + 30)[0]
        comment_len = struct.unpack_from("<H", data, pointer + 32)[0]
        pointer += 46 + name_len + extra_len + comment_len
