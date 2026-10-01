"""Format detection by content, never by file extension.

Extensions lie. A ``.csv`` delivered by an operator's accounting package is
routinely a fixed-width text file, and an ``.xls`` export is often HTML or a
zip of CSVs. Trusting the extension means trusting whoever named the file
rather than what is in it, which is how a tool ends up reporting zero findings
on a file it never actually read.

Detection therefore reads the first bytes and the zip directory, and returns a
format *and* the reason. When nothing matches, the caller is told what was seen
instead of being handed a silent fallback.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

CSV = "csv"
TSV = "tsv"
XLSX = "xlsx"
XLS = "xls"
ZIP = "zip"
JSON = "json"
JSONL = "jsonl"
DBF = "dbf"
FIXED = "fixed_width"
HTML = "html"
PDF = "pdf"
UNKNOWN = "unknown"

READABLE = frozenset({CSV, TSV, XLSX, ZIP, JSON, JSONL, DBF, FIXED})
"""Formats the tool can actually turn into canonical CSV.

``XLS`` is deliberately absent. The legacy binary format needs a third-party
reader we do not ship, and a file we cannot parse must fail loudly rather than
be skipped in silence.
"""

_UNSUPPORTED = {
    PDF: "PDF is a document, not a table. Export the data to CSV or XLSX first.",
    XLS: (
        "legacy .xls is not supported. Re-save as .xlsx, or export to CSV. "
        "The modern format is a different file format, not a renamed one."
    ),
    HTML: (
        "file is HTML, not a data export. If this came from a web table, use the "
        "site's CSV export rather than scraping."
    ),
    UNKNOWN: "file type not recognised",
}

_ZIP_MAGIC = b"PK\x03\x04"
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF_MAGIC = b"%PDF"

_DBSE_VERSIONS = frozenset(
    {0x02, 0x03, 0x04, 0x05, 0x30, 0x31, 0x32, 0x43, 0x63, 0x7B, 0x83, 0x8B, 0x8E, 0xCB, 0xF5, 0xFB}
)
"""dBase / FoxPro version bytes that appear at offset 0 of a DBF header."""

_DBSE_MIN_HEADER = 32
_DBSE_FIELD_DESC = 32


@dataclass
class Detection:
    """What the file appears to be, and the evidence for that claim."""

    format: str
    reason: str
    path: Path
    member: str | None = None
    """Inside a zip: the entry chosen to read. Not chosen before inspecting names."""
    candidates: list[str] = field(default_factory=list)
    """Zip entries that could plausibly hold data, best first."""
    detail: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    """Anything the detection step wants to carry into the audit manifest.

    Separate from ``reason`` because a reason explains one decision while a note
    records a caveat about the file as a whole -- a member whose extension lies,
    a header count that does not match the declared record count.
    """

    @property
    def supported(self) -> bool:
        return self.format in READABLE

    @property
    def unsupported_reason(self) -> str | None:
        return None if self.supported else _UNSUPPORTED.get(self.format, "unsupported")

    def to_dict(self) -> dict:
        d: dict = {
            "format": self.format,
            "reason": self.reason,
            "supported": self.supported,
        }
        if self.member:
            d["member"] = self.member
        if self.candidates:
            d["candidates"] = self.candidates
        if self.unsupported_reason:
            d["unsupported_reason"] = self.unsupported_reason
        if self.notes:
            d["notes"] = list(self.notes)
        if self.detail:
            d["detail"] = self.detail
        return d


def _read_head(path: Path, size: int = 4096) -> bytes:
    with open(path, "rb") as f:
        return f.read(size)


def _looks_like_dbf(head: bytes) -> bool:
    """Validate a dBase header structurally, not just by version byte.

    A single version byte is a one-in-256 guess that a text file could satisfy.
    Requiring plausible record count, header length and record length, plus a
    field descriptor block that ends where it should, makes a false positive
    something we would have to work very hard to produce.
    """
    if len(head) < _DBSE_MIN_HEADER:
        return False
    if head[0] not in _DBSE_VERSIONS:
        return False

    n_records = int.from_bytes(head[4:8], "little")
    header_len = int.from_bytes(head[8:10], "little")
    record_len = int.from_bytes(head[10:12], "little")

    if not (0 <= n_records <= 50_000_000):
        return False
    if not (_DBSE_MIN_HEADER < header_len <= 4000):
        return False
    if not (1 <= record_len <= 32_000):
        return False

    # The header must hold at least one field descriptor and its terminator.
    if header_len < _DBSE_MIN_HEADER + _DBSE_FIELD_DESC + 1:
        return False
    if head[header_len - 1] != 0x0D:
        return False
    return True


def _looks_like_html(head: bytes) -> bool:
    probe = head[:1024].lstrip().lower()
    return probe.startswith((b"<!doctype html", b"<html", b"<table", b"<?xml")) and (
        b"<html" in probe or b"<table" in probe
    )


def _delimiter_of(head: bytes) -> str | None:
    """Best-guess field delimiter from the first few lines.

    Counts occurrences outside quoted regions on each candidate line and picks
    the delimiter that is both present and most consistent across lines. A
    single line is not enough: a comma inside a facility name would otherwise
    decide the whole file.
    """
    text = head.decode("utf-8", errors="replace")
    lines = [ln for ln in text.splitlines()[:20] if ln.strip()][:5]
    if not lines:
        return None

    best: tuple[str, int] | None = None
    for cand in (",", "\t", ";", "|"):
        counts = [_count_outside_quotes(ln, cand) for ln in lines]
        if not counts or min(counts) < 1:
            continue
        # Consistency matters more than raw count: a delimiter appearing once
        # in every line is a schema, one appearing 40 times once is a note.
        if len(set(counts)) > 1:
            mode = max(set(counts), key=counts.count)
            if counts.count(mode) < len(counts):
                continue
        total = sum(counts)
        if best is None or total > best[1]:
            best = (cand, total)
    return best[0] if best else None


def _count_outside_quotes(line: str, delim: str) -> int:
    n = 0
    in_q = False
    for ch in line:
        if ch == '"':
            in_q = not in_q
        elif ch == delim and not in_q:
            n += 1
    return n


def _json_shape(path: Path, head: bytes) -> tuple[str, str] | None:
    """Distinguish a JSON document from newline-delimited JSON."""
    text = head.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if text[0] in "{[":
        # A whole-document JSON file is the exception: pretty-printed files
        # have newlines inside, so ndjson is only claimed when the *first* line
        # already parses on its own.
        first = text.splitlines()[0].strip().rstrip(",")
        if first.startswith("{") and first.endswith("}") and "\n" in text:
            return JSONL, "each line is a JSON object"
        return JSON, "starts with a JSON object or array"
    if text[0] in "\"'0123456789-":
        for line in text.splitlines()[:5]:
            s = line.strip()
            if not s:
                continue
            if s.startswith("{") or s.startswith("["):
                continue
            return None
        return JSONL, "each line parses as a JSON value"
    return None


def _ooxml_kind(archive: zipfile.ZipFile) -> str | None:
    """Identify an Office package by its internal part names.

    An .xlsx is itself a zip, so every workbook lands in the zip branch before
    anything else gets a chance to look at it. ``xl/`` identifies a
    spreadsheet; ``word/`` and ``ppt/`` identify documents and slide decks,
    which share the same zip container and the same ``[Content_Types].xml``
    preamble but are not production data. Reading a .docx as a table would
    produce a report about a word processor file.
    """
    names = set(archive.namelist())
    if "[Content_Types].xml" not in names:
        return None
    if any(n.startswith("xl/") for n in names):
        return XLSX
    if any(n.startswith("word/") for n in names):
        return "xlsx/docx"
    if any(n.startswith("ppt/") for n in names):
        return "xlsx/pptx"
    return "xlsx/opaque"


def _zip_entry_format(archive: zipfile.ZipFile) -> list[str]:
    """Rank zip entries that could hold tabular production data.

    Ranking is deliberate rather than alphabetical. A regulatory monthly bundle
    usually ships a README next to the data and both end in ``.txt``; the CSV
    has to win that comparison or the tool reads the readme and reports on
    prose.
    """
    ranked: list[tuple[int, str]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = info.filename
        low = name.lower()
        if low.endswith((".xlsx", ".xlsm")):
            score = 0
        elif low.endswith(".csv"):
            score = 1
        elif low.endswith((".tsv", ".psv")):
            score = 2
        elif low.endswith((".json", ".jsonl", ".ndjson")):
            score = 3
        elif low.endswith(".dbf"):
            score = 4
        elif low.endswith((".txt", ".dat")):
            # Last, and only if nothing better exists: .txt is both real
            # fixed-width data and the most common filler in an archive.
            score = 5
        else:
            continue
        if low.startswith(("__macosx/", "._")):
            continue
        if info.file_size == 0:
            continue
        depth = name.count("/")
        # Shallow wins over deep: a bundle of monthly subdirectories is a
        # judgement call the caller should make, not the tool.
        ranked.append((depth * 10 + score, name))
    ranked.sort()
    return [n for _, n in ranked]


def detect(path: Path, member: str | None = None) -> Detection:
    """Identify the format of ``path`` from its bytes.

    ``member`` selects an entry inside a zip. When omitted, the best-scoring
    data entry is chosen and reported, because silently concatenating months of
    unrelated files is worse than asking.
    """
    path = Path(path)
    if not path.exists():
        return Detection(UNKNOWN, f"file not found: {path}", path)
    if not path.is_file():
        return Detection(UNKNOWN, f"not a regular file: {path}", path)
    if path.stat().st_size == 0:
        return Detection(UNKNOWN, "file is empty", path)

    head = _read_head(path)

    if head.startswith(_PDF_MAGIC):
        return Detection(PDF, "PDF magic bytes at offset 0", path)

    if head.startswith(_OLE2_MAGIC):
        return Detection(XLS, "OLE2 compound-file header (legacy Excel or MS binary)", path)

    if head.startswith(_ZIP_MAGIC):
        return _detect_zip(path, member)

    if _looks_like_html(head):
        return Detection(HTML, "HTML markup at start of file", path)

    if _looks_like_dbf(head):
        return Detection(DBF, "dBase header validates structurally", path)

    js = _json_shape(path, head)
    if js:
        return Detection(js[0], js[1], path)

    delim = _delimiter_of(head)
    if delim == ",":
        return Detection(CSV, "comma-delimited text with a consistent field count", path)
    if delim == "\t":
        return Detection(TSV, "tab-delimited text with a consistent field count", path)
    if delim in (";", "|"):
        return Detection(
            CSV,
            f"{delim!r}-delimited text; read as comma-separated after translation",
            path,
            detail={"source_delimiter": delim},
        )

    if head[:4].lower() == b"%!ps":
        return Detection(UNKNOWN, "file is PostScript, not a data table", path)

    return Detection(
        FIXED,
        "no delimiter pattern found; treating as fixed-width text",
        path,
    )


def _detect_zip(path: Path, member: str | None) -> Detection:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()

            # A workbook is a zip, so this check has to come first. Getting it
            # wrong means reading a spreadsheet with the archive adapter and
            # reporting on the file's internal XML.
            ooxml = _ooxml_kind(archive)
            if ooxml == XLSX:
                det = Detection(XLSX, "Office Open XML workbook (zip with xl/ parts)", path)
                det.detail["sheets_hint"] = "sheet chosen by filled-column count"
                return det
            if ooxml:
                return Detection(
                    UNKNOWN,
                    f"file is an Office document ({ooxml}), not a spreadsheet",
                    path,
                )

            if member:
                if member not in names:
                    return Detection(
                        ZIP,
                        f"requested member {member!r} not in archive",
                        path,
                        candidates=_zip_entry_format(archive),
                    )
                return Detection(ZIP, f"member {member!r} selected by caller", path, member=member)
            ranked = _zip_entry_format(archive)
            if not ranked:
                return Detection(
                    ZIP,
                    "zip archive holds no readable data entries",
                    path,
                    detail={"entries": len(names)},
                )
            chosen = ranked[0]
            if len(ranked) > 1:
                # More than one plausible member is an ambiguity worth recording
                # even though the best one is chosen, because the user may have
                # meant a different file.
                return Detection(
                    ZIP,
                    f"archive holds {len(ranked)} data entries; read the first",
                    path,
                    member=chosen,
                    candidates=ranked[:20],
                    detail={"ambiguous": True},
                )
            return Detection(
                ZIP,
                "single data entry in archive",
                path,
                member=chosen,
                candidates=ranked,
            )
    except zipfile.BadZipFile:
        return Detection(UNKNOWN, "file starts like a zip but is not a valid archive", path)