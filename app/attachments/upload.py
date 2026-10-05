"""Build the ``files=`` payload for uploading attachments to the estimator API.

This lives outside the Streamlit script on purpose: importing
``streamlit_app_loader`` executes the whole page, so a helper defined there cannot
be unit-tested. It is pure — it only reshapes objects that duck-type Streamlit's
``UploadedFile`` (``name``, ``getvalue()``, ``type``).

``httpx`` accepts two shapes for ``files=``:

- a mapping ``{field_name: (filename, content, content_type)}`` for one file per
  field, and
- a list of 2-tuples ``(field_name, (filename, content, content_type))`` when the
  same field carries several files.

The session endpoint declares ``attachments: list[UploadFile]``, so it needs the
list form. Passing a top-level 3-tuple instead makes httpx raise
``ValueError: too many values to unpack (expected 2, got 3)`` while it builds the
multipart body — a client-side crash before any request leaves the process.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# The multipart field name the endpoint declares: ``attachments: list[UploadFile]``.
ATTACHMENT_FIELD_NAME = "attachments"

DEFAULT_CONTENT_TYPE = "application/octet-stream"


def multipart_attachment_fields(
    uploads: Iterable[Any],
) -> list[tuple[str, tuple[str, bytes, str]]]:
    """Shape uploads in httpx's list form: ``(field, (filename, bytes, mime))``."""
    return [
        (
            ATTACHMENT_FIELD_NAME,
            (upload.name, upload.getvalue(), upload.type or DEFAULT_CONTENT_TYPE),
        )
        for upload in uploads
    ]
