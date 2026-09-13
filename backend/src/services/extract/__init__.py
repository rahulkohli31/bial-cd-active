"""Untrusted-archive safety: the zip-bomb bound and the parse-error contract.

WHAT THIS PACKAGE USED TO BE: the server-side extraction machinery — docx/xlsx flattened to
Markdown, pptx rendered to PDF by a converter that was never deployed. Both are gone. A
file is stored as itself now and read by a script in the citizen's own sandbox, which is what
lets an answer be about the whole file rather than its first thousand rows.

WHAT SURVIVED IS NOT OFFICE-SPECIFIC. `assert_zip_not_bomb` bounds an archive that declares more
than it carries, and `FileParseError` is the governor's whole error contract. Both moved to the
upload lane, which is the only place archives arrive."""

from src.services.extract.zip_safety import FileParseError as FileParseError
from src.services.extract.zip_safety import assert_zip_not_bomb as assert_zip_not_bomb
