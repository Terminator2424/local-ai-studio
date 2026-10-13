"""Exportación de Markdown a PDF (A4) con PyMuPDF Story."""
from __future__ import annotations

import tempfile
from pathlib import Path

PDF_CSS = """
body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.4; color: #1b1b1b; }
h1 { font-size: 21pt; color: #1e2a78; margin-bottom: 6pt; }
h2 { font-size: 15pt; color: #1e2a78; margin-top: 16pt; margin-bottom: 4pt; border-bottom: 1px solid #b9c0ee; }
h3 { font-size: 12pt; color: #3b4bc8; margin-top: 11pt; margin-bottom: 3pt; }
h4 { font-size: 11pt; color: #3b4bc8; margin-top: 8pt; margin-bottom: 2pt; }
p { margin-top: 3pt; margin-bottom: 3pt; }
li { margin-bottom: 2pt; }
blockquote { color: #555555; margin-left: 8pt; font-size: 9pt; }
table { border-collapse: collapse; margin-top: 4pt; margin-bottom: 4pt; }
th { background-color: #e8ebff; border: 1px solid #8f99d6; padding: 3pt; }
td { border: 1px solid #aab1de; padding: 3pt; }
code, pre { font-family: monospace; font-size: 9.5pt; color: #222a66; }
.eq { margin-left: 16pt; margin-top: 3pt; margin-bottom: 3pt; }
"""


def export_pdf(markdown_text: str, path: str | Path) -> None:
    """Convierte Markdown a PDF (A4). Las fórmulas LaTeX se dibujan como imágenes (matplotlib); si alguna no es
    compatible, se escribe en Unicode legible. El HTML se genera en modo seguro (sin imágenes ni enlaces externos)."""
    import pymupdf

    from .mdview import md_to_html

    with tempfile.TemporaryDirectory() as tmp:
        html, _ = md_to_html(markdown_text, "light", "Inter", outdir=Path(tmp), safe=True)
        story = pymupdf.Story(html=html, user_css=PDF_CSS, archive=pymupdf.Archive(tmp))
        writer = pymupdf.DocumentWriter(str(path))
        try:
            mediabox = pymupdf.paper_rect("a4")
            where = pymupdf.Rect(mediabox.x0 + 50, mediabox.y0 + 50, mediabox.x1 - 50, mediabox.y1 - 50)
            more = True
            while more:
                dev = writer.begin_page(mediabox)
                more, _ = story.place(where)
                story.draw(dev)
                writer.end_page()
        finally:
            writer.close()
