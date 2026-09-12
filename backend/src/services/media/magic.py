"""Shared media allowlist + magic-byte gate — the SINGLE source of truth for the binary media
types an attachment may carry (Express `ALLOWED_MEDIA` / `validateAttachmentBytes`).

Every path that can put bytes in front of the model applies it, so a block at upload cannot
slip in through another:
  * `/v1/attachments` (+ office branch) — where bytes first arrive.
  * `messages/store.py`'s rehydrator — a stored reference read back every turn, which is
    also how the same bytes reach a build. Checked AGAIN rather than trusted: upload and
    serve are two different reads, only one validated.

A retired chat relay was a third consumer, and re-checking there is why this is shared code.
"""

from __future__ import annotations

from src.services.media.lanes import is_code_lane

# Allowlisted media types → magic-byte prefix. WebP is a RIFF container: the "RIFF" prefix is
# checked here and the "WEBP" form-type at offset 8 separately (see `bytes_match_declared`).
ALLOWED_MEDIA: dict[str, bytes] = {
    "image/png": bytes([0x89, 0x50, 0x4E, 0x47]),
    "image/jpeg": bytes([0xFF, 0xD8]),
    "image/gif": bytes([0x47, 0x49, 0x46, 0x38]),
    "image/webp": bytes([0x52, 0x49, 0x46, 0x46]),
    "application/pdf": bytes([0x25, 0x50, 0x44, 0x46]),
}


def magic_matches(data: bytes, magic: bytes) -> bool:
    """True iff `data` opens with the `magic` prefix."""
    return len(data) >= len(magic) and data[: len(magic)] == magic


def bytes_match_declared(media_type: str, data: bytes) -> bool:
    """True iff `media_type` is allowlisted AND `data` opens with its magic prefix (+ the WebP
    form-type at offset 8). Its caller — `messages/store.py`'s rehydrator — DROPS a block whose
    declared type is not allowed or whose bytes belie it, so unvalidated content never reaches
    the model. The upload route gates on `ALLOWED_MEDIA`/`magic_matches` directly."""
    magic = ALLOWED_MEDIA.get(media_type)
    if magic is None or not magic_matches(data, magic):
        return False
    return not (media_type == "image/webp" and data[8:12] != b"WEBP")


def chip_kind_for(media_type: str) -> str:
    """The chip vocabulary the browser branches on: `document` for a PDF, `image` otherwise.

    HERE RATHER THAN AT THE UPLOAD ROUTE, which is where this rule used to live as a lone
    ternary. It now has a second caller — the conversation projection, which has to name the
    same kind for a chip rebuilt on reload as the upload response named when the file was first
    attached. Two call sites deriving one vocabulary independently is how a reloaded chip ends
    up rendering as a different shape from the one the citizen just watched appear.

    THREE KINDS, ONE PER THING A CHIP CAN DO. An image opens over the conversation, a
    PDF opens in a tab, and a code-lane file returns the file — because a spreadsheet, document or
    deck cannot be rendered in a browser without a converter this platform does not host.
    `image` is the fallback rather than `file`, so an unrecognised type keeps the behaviour it had
    before the code lane existed rather than silently becoming a download.

    It sits beside `ALLOWED_MEDIA` deliberately: the five formats admitted here each need a kind
    here in the same change. Splitting them across two modules is what would let a format be
    admitted with no chip that knows what to do with it — a spreadsheet drawn as a broken
    thumbnail, which is what the `image` fallback would have made of one.
    """
    if media_type == "application/pdf":
        return "document"
    if is_code_lane(media_type):
        return "file"
    return "image"
