"""Build a tiny in-memory PDF for tests. Avoids checking a binary into git."""
from __future__ import annotations


def make_pdf_bytes(text: str = "hello from a tiny pdf") -> bytes:
    """Return a valid 1-page PDF containing the given text."""
    content = (
        "BT /F1 12 Tf 72 720 Td (" + text.replace("(", r"\(").replace(")", r"\)") + ") Tj ET"
    )
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
        b"4 0 obj<</Length " + str(len(content)).encode() + b">>stream\n"
        + content.encode() + b"\nendstream\nendobj\n"
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    )
    xref_offset = len(body)
    xref = (
        b"xref\n0 6\n0000000000 65535 f \n"
        + b"".join(
            f"{body.find(f'{i} 0 obj'.encode()):010d} 00000 n \n".encode()
            for i in range(1, 6)
        )
    )
    trailer = (
        b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n"
        + str(xref_offset).encode()
        + b"\n%%EOF"
    )
    return body + xref + trailer
