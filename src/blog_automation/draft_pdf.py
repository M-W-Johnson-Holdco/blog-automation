"""Convert generated Markdown blog drafts to PDF."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

# xhtml2pdf/Helvetica only embeds WinAnsi glyphs; Unicode dashes render as black boxes.
_UNICODE_DASHES = str.maketrans(
    {
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
        "\u00ad": "-",  # soft hyphen
        "\ufe58": "-",  # small em dash
        "\ufe63": "-",  # small hyphen-minus
        "\uff0d": "-",  # fullwidth hyphen-minus
    }
)


PDF_CSS = """
body {
    font-family: Helvetica, Arial, sans-serif;
    margin: 36px;
    line-height: 1.45;
    font-size: 11pt;
    color: #111;
}
h1 {
    font-size: 20pt;
    margin-bottom: 12px;
}
h2 {
    font-size: 14pt;
    margin-top: 20px;
    margin-bottom: 8px;
}
h3 {
    font-size: 12pt;
    margin-top: 14px;
    margin-bottom: 6px;
}
p {
    margin: 0 0 10px 0;
}
table {
    border-collapse: collapse;
    width: 100%;
    margin: 12px 0;
}
th, td {
    border: 1px solid #999;
    padding: 6px 8px;
    text-align: left;
    vertical-align: top;
}
th {
    background: #f3f3f3;
}
pre {
    font-family: Courier, monospace;
    font-size: 7pt;
    line-height: 1.3;
    background: #f8f8f8;
    border: 1px solid #ddd;
    padding: 8px;
    white-space: pre-wrap;
    word-wrap: break-word;
    margin: 8px 0;
}
code {
    font-family: Courier, monospace;
    font-size: 8pt;
}
hr {
    border: none;
    border-top: 1px solid #ccc;
    margin: 20px 0;
}
"""


def normalize_text_for_pdf(text: str) -> str:
    """Replace Unicode dash variants with ASCII hyphens for PDF font compatibility."""
    return text.translate(_UNICODE_DASHES)


def markdown_body_to_html(markdown_text: str) -> str:
    """Return an HTML fragment suitable for CMS blog body fields."""
    try:
        import markdown as markdown_lib
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: markdown. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    return markdown_lib.markdown(
        markdown_text,
        extensions=["extra", "tables", "sane_lists"],
    )


def markdown_to_html(markdown_text: str) -> str:
    body = markdown_body_to_html(markdown_text)
    return (
        "<!DOCTYPE html>"
        "<html><head><meta charset=\"utf-8\">"
        f"<style>{PDF_CSS}</style>"
        "</head><body>"
        f"{body}"
        "</body></html>"
    )


def save_draft_pdf(markdown_text: str, pdf_path: Path) -> None:
    """Write a PDF version of a Markdown draft."""
    try:
        from xhtml2pdf import pisa
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: xhtml2pdf. Install dependencies with "
            "`python -m pip install -r requirements.txt`."
        ) from exc

    html = markdown_to_html(normalize_text_for_pdf(markdown_text))
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    buffer = BytesIO()
    status = pisa.CreatePDF(html, dest=buffer, encoding="utf-8")
    if status.err:
        raise RuntimeError(f"PDF generation failed with status {status.err}")

    pdf_path.write_bytes(buffer.getvalue())
