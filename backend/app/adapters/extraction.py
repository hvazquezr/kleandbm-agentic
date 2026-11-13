"""Text extraction from uploaded documents into located passages.

Every passage records where it came from (spec §3: extracted passages keep source
locations for traceability):
- PDF:   {"page": n, "char_start": i, "char_end": j}   offsets within that page's text
- DOCX:  {"paragraphs": [first, last]} or {"table": t, "rows": [first, last]}   1-based
- XLSX:  {"sheet": name, "range": "B7:H14"}
- CSV:   {"rows": [first, last]}   1-based, header is row 1
- text:  {"lines": [first, last], "char_start": i, "char_end": j}

Extracted text is untrusted data. Callers must never treat it as instructions.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

TARGET_CHARS = 1200  # soft passage size; a single long unit may exceed it

SUPPORTED_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "text/csv": "csv",
    "text/plain": "text",
    "text/markdown": "text",
}
# Stored and downloadable, but no text is extracted yet (no OCR or vision in P2a).
STORED_ONLY_TYPES = {"image/png", "image/jpeg"}

EXTENSION_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


class ExtractionError(Exception):
    pass


@dataclass(frozen=True)
class Passage:
    text: str
    location: dict[str, Any]


@dataclass
class ExtractionResult:
    passages: list[Passage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def content_type_for(filename: str, declared: str | None) -> str:
    """Trust the file extension over the browser-declared type, which is often generic."""
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return EXTENSION_TYPES.get(ext) or (declared or "application/octet-stream").split(";")[0].strip()


def extract(content: bytes, content_type: str) -> ExtractionResult:
    kind = SUPPORTED_TYPES.get(content_type)
    if kind is None:
        if content_type in STORED_ONLY_TYPES:
            return ExtractionResult(notes=["Image stored; text extraction for images is not supported yet."])
        raise ExtractionError(f"unsupported content type {content_type}")
    try:
        return {"pdf": _pdf, "docx": _docx, "xlsx": _xlsx, "csv": _csv, "text": _text}[kind](content)
    except ExtractionError:
        raise
    except Exception as exc:  # malformed files from users must not crash the upload
        raise ExtractionError(f"could not read {kind} file: {exc}") from exc


# --- Chunking -------------------------------------------------------------------------------


def _chunk_units(units: list[tuple[str, Any]]) -> list[tuple[str, list[Any]]]:
    """Group consecutive text units into passages of about TARGET_CHARS, never splitting a unit."""
    out: list[tuple[str, list[Any]]] = []
    buf: list[str] = []
    keys: list[Any] = []
    size = 0
    for text, key in units:
        if buf and size + len(text) > TARGET_CHARS:
            out.append(("\n".join(buf), keys))
            buf, keys, size = [], [], 0
        buf.append(text)
        keys.append(key)
        size += len(text) + 1
    if buf:
        out.append(("\n".join(buf), keys))
    return out


def _split_long(text: str, start: int) -> list[tuple[str, int, int]]:
    """Split one long block on paragraph or sentence boundaries; returns (text, start, end)."""
    if len(text) <= TARGET_CHARS:
        return [(text, start, start + len(text))]
    pieces: list[tuple[str, int, int]] = []
    pos = 0
    while pos < len(text):
        end = min(len(text), pos + TARGET_CHARS)
        if end < len(text):
            window = text[pos:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > TARGET_CHARS // 3:
                end = pos + cut + 1
        piece = text[pos:end]
        if piece.strip():
            lead = len(piece) - len(piece.lstrip())
            pieces.append((piece.strip(), start + pos + lead, start + pos + lead + len(piece.strip())))
        pos = end
    return pieces


# --- Formats --------------------------------------------------------------------------------


def _pdf(content: bytes) -> ExtractionResult:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    result = ExtractionResult()
    empty_pages = 0
    for page_no, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if not text:
            empty_pages += 1
            continue
        for piece, start, end in _split_long(text, 0):
            result.passages.append(Passage(piece, {"page": page_no, "char_start": start, "char_end": end}))
    if empty_pages:
        result.notes.append(f"{empty_pages} page(s) had no extractable text (scanned images need OCR).")
    return result


def _docx(content: bytes) -> ExtractionResult:
    import docx

    document = docx.Document(io.BytesIO(content))
    result = ExtractionResult()
    paragraphs = [
        (p.text.strip() if not p.style.name.startswith("Heading") else f"# {p.text.strip()}", i)
        for i, p in enumerate(document.paragraphs, start=1)
        if p.text.strip()
    ]
    for text, keys in _chunk_units(paragraphs):
        result.passages.append(Passage(text, {"paragraphs": [keys[0], keys[-1]]}))
    for t_index, table in enumerate(document.tables, start=1):
        rows = [
            (" | ".join(cell.text.strip() for cell in row.cells), r_index)
            for r_index, row in enumerate(table.rows, start=1)
        ]
        for text, keys in _chunk_units([r for r in rows if r[0].replace("|", "").strip()]):
            result.passages.append(Passage(text, {"table": t_index, "rows": [keys[0], keys[-1]]}))
    return result


def _format_cell(value: Any, number_format: str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if "%" in number_format:
            return f"{value * 100:.1f}%"
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if isinstance(value, int):
        return f"{value:,}" if "#,##0" in number_format else str(value)
    return str(value).strip()


MAX_FORMULA_CHARS = 40


def _describe_formula(formula: str) -> str:
    """Keep short formulas verbatim (=D8-E8 often *is* the business definition) and summarize
    long lookups so they don't drown the values: =SUMIFS over 'Monthly Extract'."""
    if len(formula) <= MAX_FORMULA_CHARS:
        return formula
    func = re.match(r"=\s*([A-Z][A-Z0-9.]*)\(", formula)
    sheets = sorted(set(re.findall(r"'([^']+)'!", formula)))
    text = f"={func.group(1) if func else 'formula'}"
    return f"{text} over {', '.join(repr(s) for s in sheets)}" if sheets else f"{text}(…)"


@dataclass
class _Region:
    lines: list[tuple[str, int]]  # (rendered row, row number)
    min_col: int
    max_col: int

    @property
    def size(self) -> int:
        return sum(len(line) + 1 for line, _ in self.lines)


def _xlsx(content: bytes) -> ExtractionResult:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    values = load_workbook(io.BytesIO(content), data_only=True, read_only=False)
    formulas = load_workbook(io.BytesIO(content), data_only=False, read_only=False)
    result = ExtractionResult()
    missing_cached = 0

    for ws in values.worksheets:
        wf = formulas[ws.title]
        # 1. Regions: runs of non-empty rows. Blank rows separate tables and note blocks.
        regions: list[_Region] = []
        current: _Region | None = None
        for row in ws.iter_rows():
            cells = []
            for cell in row:
                formula = wf[cell.coordinate].value
                is_formula = isinstance(formula, str) and formula.startswith("=")
                if cell.value is None:
                    if is_formula:
                        missing_cached += 1
                        cells.append((cell, f"{_describe_formula(formula)} (no cached value)"))
                    continue
                text = _format_cell(cell.value, cell.number_format)
                if not text:
                    continue
                cells.append((cell, f"{text} [{_describe_formula(formula)}]" if is_formula else text))
            if not cells:
                current = None
                continue
            line = f"{cells[0][0].row}: " + " | ".join(f"{c.coordinate}={t}" for c, t in cells)
            cols = [c.column for c, _ in cells]
            if current is None:
                current = _Region([], min(cols), max(cols))
                regions.append(current)
            current.lines.append((line, cells[0][0].row))
            current.min_col = min(current.min_col, *cols)
            current.max_col = max(current.max_col, *cols)

        # 2. Passages: pack whole regions together up to the target size, so titles and notes
        #    stay with their tables. Split an oversized region by rows and repeat its first row
        #    (the header) at the top of every piece, so numbers keep their column names.
        def emit(parts: list[_Region], lines: list[tuple[str, int]], header: list[tuple[str, int]] | None = None) -> None:
            first, last = lines[0][1], lines[-1][1]
            rng = (f"{get_column_letter(min(p.min_col for p in parts))}{first}:"
                   f"{get_column_letter(max(p.max_col for p in parts))}{last}")
            body = "\n".join(text for text, _ in ([*header, *lines] if header else lines))
            location: dict[str, Any] = {"sheet": ws.title, "range": rng}
            if header:
                # Repeated for context; not part of the range.
                location["header_rows"] = [n for _, n in header]
            result.passages.append(Passage(f"[{ws.title}]\n{body}", location))

        pack: list[_Region] = []
        pack_size = 0
        for region in regions:
            if region.size > TARGET_CHARS:
                if pack:
                    emit(pack, [ln for p in pack for ln in p.lines])
                    pack, pack_size = [], 0
                # A single-cell first row is a section title; the column header follows it.
                n_header = 2 if " | " not in region.lines[0][0] and len(region.lines) > 2 else 1
                header, rows = region.lines[:n_header], region.lines[n_header:]
                for i, (_, keys) in enumerate(_chunk_units([(text, (text, n)) for text, n in rows])):
                    if i == 0:
                        emit([region], [*header, *keys])  # the header rows are part of this range
                    else:
                        emit([region], keys, header=header)
                continue
            if pack and pack_size + region.size > TARGET_CHARS:
                emit(pack, [ln for p in pack for ln in p.lines])
                pack, pack_size = [], 0
            pack.append(region)
            pack_size += region.size
        if pack:
            emit(pack, [ln for p in pack for ln in p.lines])

    if missing_cached:
        result.notes.append(
            f"{missing_cached} formula cell(s) had no cached value; the file was saved without recalculation."
        )
    return result


def _csv(content: bytes) -> ExtractionResult:
    text = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return ExtractionResult(notes=["Empty CSV."])
    header = rows[0]
    units = [(" | ".join(header), 1)]
    units += [
        (" | ".join(f"{h}={v}" for h, v in zip(header, r) if v.strip()), i)
        for i, r in enumerate(rows[1:], start=2)
        if any(v.strip() for v in r)
    ]
    result = ExtractionResult()
    for chunk, keys in _chunk_units(units):
        result.passages.append(Passage(chunk, {"rows": [keys[0], keys[-1]]}))
    return result


def _text(content: bytes) -> ExtractionResult:
    text = content.decode("utf-8", errors="replace")
    result = ExtractionResult()
    # Paragraph units with their line and character spans.
    for match in re.finditer(r"\S(?:.*\S)?(?:\n(?!\s*\n).*\S)*", text):
        block = match.group(0)
        first_line = text.count("\n", 0, match.start()) + 1
        for piece, start, end in _split_long(block, match.start()):
            line_a = text.count("\n", 0, start) + 1
            line_b = text.count("\n", 0, end) + 1
            result.passages.append(
                Passage(piece, {"lines": [max(line_a, first_line), line_b], "char_start": start, "char_end": end})
            )
    return result
