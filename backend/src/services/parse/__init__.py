"""Untrusted-file parsing for the app parse endpoint: structured-row/text parsers
behind a killable process governor. Public surface via explicit re-exports."""

from src.services.parse.governor import run_parse as run_parse
from src.services.parse.parsers import parse_kind_for as parse_kind_for
