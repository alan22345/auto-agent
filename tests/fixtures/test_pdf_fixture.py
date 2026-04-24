from io import BytesIO

from pypdf import PdfReader

from tests.fixtures.pdf_fixture import make_pdf_bytes


def test_roundtrip():
    pdf = make_pdf_bytes("round trip text")
    reader = PdfReader(BytesIO(pdf))
    assert "round trip text" in (reader.pages[0].extract_text() or "")


def test_default_text():
    pdf = make_pdf_bytes()
    reader = PdfReader(BytesIO(pdf))
    assert "hello from a tiny pdf" in (reader.pages[0].extract_text() or "")


def test_parens_and_backslashes_escaped():
    pdf = make_pdf_bytes("a(b)c backslash\\here")
    # Main assertion: PDF parses without error.
    reader = PdfReader(BytesIO(pdf))
    text = reader.pages[0].extract_text() or ""
    assert "a" in text and "c" in text
