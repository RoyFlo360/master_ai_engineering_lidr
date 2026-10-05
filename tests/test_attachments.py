"""Unit tests for attachment text extraction and transcript enrichment.

The extractor is pure once it has bytes in hand, so these tests build the bytes
in memory — a real DOCX via ``python-docx``, a real (corrupt) PDF blob — and
never touch the filesystem or the network.
"""

from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from docx import Document

from app.attachments.extractor import (
    AttachmentExtractionError,
    UnsupportedAttachmentError,
    enrich_transcript,
    extract_text,
)
from app.attachments.upload import multipart_attachment_fields


def docx_bytes(*paragraphs: str) -> bytes:
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --- extension dispatch -----------------------------------------------------


def test_an_unknown_extension_is_rejected():
    with pytest.raises(UnsupportedAttachmentError) as excinfo:
        extract_text(filename="notes.txt", content=b"whatever", max_chars=1000)

    assert excinfo.value.filename == "notes.txt"


def test_the_extension_check_is_case_insensitive():
    with pytest.raises(UnsupportedAttachmentError):
        extract_text(filename="NOTES.TXT", content=b"x", max_chars=1000)


# --- DOCX -------------------------------------------------------------------


def test_docx_paragraphs_are_extracted_and_blank_ones_dropped():
    content = docx_bytes("Project brief", "", "Build a web app.", "   ")

    text = extract_text(filename="brief.docx", content=content, max_chars=10_000)

    assert text == "Project brief\nBuild a web app."


def test_docx_text_is_truncated_to_max_chars():
    content = docx_bytes("x" * 5000)

    text = extract_text(filename="long.docx", content=content, max_chars=100)

    assert len(text) == 100


# --- PDF --------------------------------------------------------------------


def test_a_corrupt_pdf_raises_an_extraction_error_not_a_raw_parser_error():
    with pytest.raises(AttachmentExtractionError) as excinfo:
        extract_text(filename="broken.pdf", content=b"not a pdf at all", max_chars=1000)

    assert excinfo.value.filename == "broken.pdf"


# --- enrichment -------------------------------------------------------------


def test_no_attachments_returns_the_transcript_unchanged():
    """No fenced sections are added; the transcript is passed through as-is."""
    assert enrich_transcript(transcript="  hello  ", attachments=[]) == "  hello  "


def test_each_attachment_is_fenced_with_its_filename():
    enriched = enrich_transcript(
        transcript="the raw transcript",
        attachments=[("brief.docx", "extracted brief text")],
    )

    assert "the raw transcript" in enriched
    assert "--- attachment: brief.docx ---" in enriched
    assert "extracted brief text" in enriched
    assert "--- end attachment ---" in enriched


def test_empty_attachment_text_is_skipped():
    enriched = enrich_transcript(
        transcript="raw", attachments=[("empty.docx", ""), ("ok.docx", "body")]
    )

    assert "empty.docx" not in enriched
    assert "ok.docx" in enriched


# --- the client-side multipart shape ----------------------------------------


def upload(
    name: str = "brief.pdf",
    content: bytes = b"%PDF-1.4",
    type: str | None = "application/pdf",
) -> SimpleNamespace:
    """Duck-type of a Streamlit ``UploadedFile``."""
    return SimpleNamespace(name=name, getvalue=lambda: content, type=type)


def test_multipart_fields_use_httpxs_list_shape():
    fields = multipart_attachment_fields([upload()])

    assert fields == [("attachments", ("brief.pdf", b"%PDF-1.4", "application/pdf"))]


def test_multiple_files_share_the_attachments_field_name():
    fields = multipart_attachment_fields([upload("a.pdf"), upload("b.docx")])

    assert [name for name, _ in fields] == ["attachments", "attachments"]


def test_a_missing_content_type_falls_back_to_octet_stream():
    fields = multipart_attachment_fields([upload(type=None)])

    assert fields[0][1][2] == "application/octet-stream"


def test_the_built_fields_encode_as_a_multipart_request():
    """Regression: the old top-level 3-tuple shape raised ``ValueError: too many
    values to unpack (expected 2, got 3)`` while httpx built the body."""
    fields = multipart_attachment_fields([upload()])

    request = httpx.Request(
        "POST",
        "http://test/sessions/session-id/estimate",
        data={"transcript": "x" * 20},
        files=fields,
    )

    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.read()
    assert b'name="attachments"' in body
    assert b'filename="brief.pdf"' in body
