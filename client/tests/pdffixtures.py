"""Small PDFs written by hand for the offline tests: pages of any size with text in the
standard Helvetica font (PDFium knows its metrics, so QtPdf finds the text and the
boxes of its characters) and, if wanted, a nested outline (bookmarks).

    make_text_pdf([(595, 842, [(72, 100, 24, "Needle in a haystack")]), ...],
                  outline=[("Chapter 1", 0, []), ("Chapter 2", 1, [("Section 2.1", 2, [])])])

Text positions are (x, baseline, size) in points from the page's top left, as the
tests measure them; PDF's own y axis runs up from the bottom.
"""

from __future__ import annotations

Outline = list[tuple[str, int, "Outline"]]


def _string(text: str) -> bytes:
    raw = text.encode("cp1252")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def make_text_pdf(pages: list[tuple[float, float, list[tuple[float, float, float, str]]]],
                  outline: Outline | None = None) -> bytes:
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")                  # filled in at the end
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    page_ids = []
    for width, height, texts in pages:
        stream = b"".join(b"BT /F1 %g Tf %g %g Td %s Tj ET\n" % (size, x, height - baseline, _string(text))
                          for x, baseline, size, text in texts)
        contents = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
        page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %g %g] /Resources << /Font << /F1 %d 0 R >> >>"
                            b" /Contents %d 0 R >>" % (tree, width, height, font, contents)))
    objects[tree - 1] = (b"<< /Type /Pages /Kids [%s] /Count %d >>"
                         % (b" ".join(b"%d 0 R" % p for p in page_ids), len(page_ids)))

    extra = b""
    if outline:
        root = add(b"")

        def build(items: Outline, parent: int) -> tuple[int, int, int]:
            ids = [add(b"") for _ in items]
            total = len(items)
            for n, (title, page, children) in enumerate(items):
                fields = [b"/Title " + _string(title), b"/Parent %d 0 R" % parent,
                          b"/Dest [%d 0 R /XYZ 0 %g 0]" % (page_ids[page], pages[page][1])]
                if n:
                    fields.append(b"/Prev %d 0 R" % ids[n - 1])
                if n + 1 < len(ids):
                    fields.append(b"/Next %d 0 R" % ids[n + 1])
                if children:
                    first, last, count = build(children, ids[n])
                    fields += [b"/First %d 0 R" % first, b"/Last %d 0 R" % last, b"/Count %d" % count]
                    total += count
                objects[ids[n] - 1] = b"<< " + b" ".join(fields) + b" >>"
            return ids[0], ids[-1], total

        first, last, count = build(outline, root)
        objects[root - 1] = b"<< /Type /Outlines /First %d 0 R /Last %d 0 R /Count %d >>" % (first, last, count)
        extra = b" /Outlines %d 0 R /PageMode /UseOutlines" % root
    objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R%s >>" % (tree, extra)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, xref)
    return bytes(out)


def make_epub(chapters: Outline, *, nav: bool = True) -> bytes:
    """An EPUB whose table of contents is `chapters` (title, chapter number, children):
    an EPUB 3 navigation document, or with nav=False only an EPUB 2 NCX."""
    import io
    import zipfile

    def nav_list(items: Outline) -> str:
        return "<ol>" + "".join(f'<li><a href="text/ch{n}.xhtml#top">{title}</a>{nav_list(kids) if kids else ""}</li>'
                                for title, n, kids in items) + "</ol>"

    counter = [0]

    def ncx_points(items: Outline) -> str:
        out = ""
        for title, n, kids in items:
            counter[0] += 1
            out += (f'<navPoint id="p{counter[0]}" playOrder="{counter[0]}"><navLabel><text>{title}</text></navLabel>'
                    f'<content src="text/ch{n}.xhtml"/>{ncx_points(kids)}</navPoint>')
        return out

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        book.writestr("mimetype", "application/epub+zip")
        book.writestr("META-INF/container.xml",
                      '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                      '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                      '</rootfiles></container>')
        manifest = '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        if nav:
            manifest += '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        book.writestr("OEBPS/content.opf",
                      '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
                      f'<manifest>{manifest}</manifest><spine toc="ncx"></spine></package>')
        if nav:
            book.writestr("OEBPS/nav.xhtml",
                          '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" '
                          'xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                          f'<nav epub:type="toc">{nav_list(chapters)}</nav></body></html>')
        book.writestr("OEBPS/toc.ncx",
                      '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                      f'<navMap>{ncx_points(chapters)}</navMap></ncx>')
    return buffer.getvalue()
