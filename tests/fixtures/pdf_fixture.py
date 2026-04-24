"""Build a tiny in-memory PDF for tests. Avoids checking a binary into git."""
from __future__ import annotations


def make_pdf_bytes(text: str = "hello from a tiny pdf") -> bytes:
    """Return a valid 1-page PDF containing the given text. Input must be single-line ASCII; backslashes and parens are escaped."""
    safe = (
        text.replace("\\", r"\\")
            .replace("(", r"\(")
            .replace(")", r"\)")
    )
    content = "BT /F1 12 Tf 72 720 Td (" + safe + ") Tj ET"

    # MediaBox = US Letter (612 x 792 pt); text at (72, 720) in 12pt Helvetica
    objects: list[bytes] = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n",
        b"4 0 obj<</Length " + str(len(content)).encode() + b">>stream\n" + content.encode() + b"\nendstream\nendobj\n",
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n",
    ]

    header = b"%PDF-1.4\n"
    offsets = [len(header)]
    for obj in objects[:-1]:
        offsets.append(offsets[-1] + len(obj))
    body = header + b"".join(objects)
    xref_offset = len(body)
    xref = b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
        f"{off:010d} 00000 n \n".encode() for off in offsets
    )
    trailer = b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n" + str(xref_offset).encode() + b"\n%%EOF"
    return body + xref + trailer
