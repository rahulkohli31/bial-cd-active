"""Untrusted-file extraction: office (docx/xlsx → Markdown) + deck (pptx → PDF) (R17). Public
surface via explicit re-exports."""

from src.services.extract.deck import DeckConvertError as DeckConvertError
from src.services.extract.deck import DeckResult as DeckResult
from src.services.extract.deck import convert_deck_to_pdf as convert_deck_to_pdf
from src.services.extract.deck import deck_attachments_enabled as deck_attachments_enabled
from src.services.extract.office import EXCEL_MEDIA_TYPE as EXCEL_MEDIA_TYPE
from src.services.extract.office import OFFICE_MEDIA_TYPES as OFFICE_MEDIA_TYPES
from src.services.extract.office import PPTX_MEDIA_TYPE as PPTX_MEDIA_TYPE
from src.services.extract.office import WORD_MEDIA_TYPE as WORD_MEDIA_TYPE
from src.services.extract.office import ExtractResult as ExtractResult
from src.services.extract.office import OfficeExtractError as OfficeExtractError
from src.services.extract.office import extract_office as extract_office
from src.services.extract.zip_safety import FileParseError as FileParseError
