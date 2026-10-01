"""Table extraction: bytes in, header + rows out.

Every adapter ends here. Keeping one table shape means the header detection, the
units-row split, and the schema mapper are written once and behave identically
whether the file arrived as CSV, XLSX, JSON or DBF.

The table is deliberately dumb: strings, in source order, with the original
line number where one exists. All interpretation happens downstream, where it
can be audited.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import struct
import zipfile
from dataclasses import dataclass, field
from datetime import date as _date
from datetime import datetime
from pathlib import Path
from typing import Any

from ..loader import MAX_CELL_BYTES, MAX_ROWS, is_null
from . import sniff
from .sniff import Detection

HEADER_SCAN_ROWS = 12
"""How far down to look for the real header.

Production workbooks routinely open with a title, a report date, and a blank
row before the columns do. Reading row 1 as the header in that case produces a
one-column table and a confident, entirely wrong report.
"""


class IngestError(Exception):
    """Raised when a file cannot be turned into a table at all.

    Only for failures that make the file unauditable. A malformed cell, an
    unexpected type, a missing sheet name -- those are recoverable and become
    notes, not exceptions.
    """


@dataclass
class Table:
    """An extracted table plus everything known about how it was obtained."""

    columns: list[str]
    rows: list[list[str]]
    source_format: str
    member: str | None = None
    sheet: str | None = None
    header_line: int = 1
    """1-indexed position of the header in the original file, when known."""
    notes: list[str] = field(default_factory=list)
    truncated: bool = False
    available_sheets: list[str] = field(default_factory=list)
    source_delimiter: str | None = None

    def as_dict(self) -> dict[str, list[str]]:
        """Column name -> values, ready for the schema mapper."""
        return {c: [r[i] if i < len(r) else "" for r in self.rows] for i, c in enumerate(self.columns)}

    def units_row(self) -> dict[str, str]:
        """Pull a units row out of the row immediately below the header.

        Reuses the same test the period loader uses, so a workbook units row and
        a CSV units row are judged by one rule. Returns ``{}`` when there is no
        units row, which is a legitimate answer rather than a failure.
        """
        from ..period import _looks_like_units_row

        if not self.rows:
            return {}
        first = self.rows[0]
        is_units, _ = _looks_like_units_row([str(v) if v is not None else "" for v in first])
        if not is_units:
            return {}
        out: dict[str, str] = {}
        for i, col in enumerate(self.columns):
            if i >= len(first):
                break
            token = str(first[i] or "").strip()
            if token and not is_null(token):
                out[col] = token
        return out

    def without_units_row(self) -> "Table":
        """Copy with a detected units row removed from the data."""
        if not self.units_row():
            return self
        t = Table(
            columns=list(self.columns),
            rows=[list(r) for r in self.rows[1:]],
            source_format=self.source_format,
            member=self.member,
            sheet=self.sheet,
            header_line=self.header_line,
            notes=list(self.notes),
            truncated=self.truncated,
            available_sheets=list(self.available_sheets),
            source_delimiter=self.source_delimiter,
        )
        t.notes.append(
            f"units row detected below the header (source row "
            f"{self.header_line + 1}) and excluded from the data"
        )
        return t


# --------------------------------------------------------------- text helpers


def _clean_cell(v: Any) -> str:
    """Render one cell as a string without inventing precision.

    Floats are rendered by ``repr`` so that ``0.1`` stays ``0.1`` rather than
    becoming ``0.10000000000000001``. Datetimes become ISO dates. Everything
    else becomes ``str``.
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        # repr is the shortest string that round-trips, which keeps a value the
        # operator typed from growing digits the file never had.
        return repr(v)
    if isinstance(v, int):
        return str(v)
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, _date):
        return v.isoformat()
    s = str(v)
    if len(s.encode("utf-8", errors="replace")) > MAX_CELL_BYTES:
        s = s[:MAX_CELL_BYTES]
    return s


def _select_header(grid: list[list[Any]]) -> int:
    """Index of the row that is really the header.

    Scored on three things together, because any one of them lies:

    * how many cells are filled (a title row has one)
    * how many are distinct (a units row is often repeated, a note row is one
      long sentence spread across columns)
    * whether the row's cells look like names rather than data

    A units row sits below the header and would win on filled-distinct, so it is
    explicitly demoted.
    """
    from ..period import _looks_like_units_row

    best = 0
    best_score = -1.0
    for i, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        cells = [_clean_cell(c) for c in row]
        filled = [c for c in cells if c.strip()]
        if len(filled) < 2:
            continue
        distinct = len({c.strip().lower() for c in filled})
        numeric = 0
        for c in filled:
            try:
                float(c.replace(",", ""))
                numeric += 1
            except ValueError:
                pass
        score = len(filled) * 2.0 + distinct
        if numeric:
            score -= numeric * 4.0
        # A units row looks like a header: short, non-numeric, mostly distinct.
        # Shape alone cannot tell them apart, so the demotion only applies when
        # an earlier row already has at least as many filled cells -- i.e. there
        # is a real header above it. Without this condition a dBase header of
        # four short field names is demoted out of the running and the first
        # data row becomes the header instead.
        is_units, _ = _looks_like_units_row(cells)
        if is_units and i > 0:
            earlier = [
                c for c in grid[i - 1] if _clean_cell(c).strip()
            ]
            if len(earlier) >= len(filled):
                score -= 10.0
        if score > best_score:
            best, best_score = i, score
    return best


def _normalise_header(names: list[Any]) -> list[str]:
    """Clean header names into usable, unique column keys.

    Blank cells get positional names so nothing is silently dropped, and
    duplicates get a suffix so a later column does not overwrite an earlier
    one in the column dict. Losing a column here would silently drop a finding.
    """
    out: list[str] = []
    seen: dict[str, int] = {}
    for i, raw in enumerate(names):
        name = _clean_cell(raw).strip()
        if not name:
            name = f"column_{i + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def _build(grid: list[list[Any]], det: Detection, **kw) -> Table:
    """Assemble a Table from a raw grid, handling header selection and limits."""
    if not grid:
        raise IngestError("file contains no rows")

    hidx = _select_header(grid)
    columns = _normalise_header(grid[hidx])
    body = grid[hidx + 1 :]

    # A header with no data rows is not an error in the file, it is a file that
    # cannot be audited. Accepting it would produce a report over zero rows with
    # zero findings, which reads as a clean bill of health for a file that was
    # never actually populated.
    if not body:
        raise IngestError(
            f"header found at row {hidx + 1} but no data rows follow it; "
            f"there is nothing to audit"
        )

    truncated = False
    if len(body) > MAX_ROWS:
        body = body[:MAX_ROWS]
        truncated = True

    rows = [[_clean_cell(c) for c in r] for r in body]
    # Pad short rows so every row is indexable against every column.
    width = len(columns)
    rows = [r + [""] * (width - len(r)) if len(r) < width else r[:width] for r in rows]

    return Table(
        columns=columns,
        rows=rows,
        source_format=det.format,
        member=det.member,
        header_line=hidx + 1,
        truncated=truncated,
        **kw,
    )


# --------------------------------------------------------------------- CSV


def read_csv(path: Path, det: Detection) -> Table:
    """Read delimited text, honouring a sniffed delimiter."""
    delim = det.detail.get("source_delimiter", ",") if det.format == sniff.CSV else "\t"
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    grid = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
    # utf-8-sig strips the BOM when it sits at the very start of the file, but
    # an Excel export can leave the character inside the first cell after the
    # delimiter was rewritten. A U+FEFF in front of a header name means the
    # column no longer matches any known name and the mapper silently loses it,
    # which is the worst possible failure: no error, just missing coverage.
    _strip_bom(grid)

    t = _build(grid, det, source_delimiter=delim)
    if delim != ",":
        t.notes.append(f"source delimiter was {delim!r}; normalised on write")
    return t


def _strip_bom(grid: list[list[Any]]) -> None:
    """Remove a byte-order mark from the first cell, in place."""
    if grid and grid[0] and isinstance(grid[0][0], str):
        grid[0][0] = grid[0][0].lstrip("﻿")


def read_fixed(path: Path, det: Detection) -> Table:
    """Read fixed-width text by splitting on runs of 2+ spaces.

    A real fixed-width file has column *positions*, and this does not recover
    them. It is a last resort that handles the common padded-report shape, and
    the schema mapper is what stops a bad split from being trusted: an
    identifier split in half will not repeat per period and will not win the
    well role on content.
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    grid = []
    for line in lines[:200]:
        if not line.strip():
            continue
        parts = [p for p in re.split(r" {2,}|\t", line.strip()) if p.strip()]
        grid.append(parts)
    t = _build(grid, det)
    t.notes.append(
        "fixed-width split on whitespace runs; column positions were not "
        "recovered. Check the mapping before trusting the report."
    )
    return t


# -------------------------------------------------------------------- XLSX


def read_xlsx(path: Path, det: Detection, sheet: str | None = None) -> Table:
    """Read a workbook via openpyxl, which is an optional dependency.

    ``data_only`` is deliberate. A workbook exported from a reporting tool
    usually holds formulas whose cached values are what the operator saw. With
    ``data_only=False`` every such cell reads as ``=SUM(...)``, which would
    turn a healthy file into thousands of QC003 findings.
    """
    try:
        import openpyxl  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only without dep
        raise IngestError(
            "reading .xlsx needs the optional 'openpyxl' dependency. Install it "
            "with: pip install 'haql-qc[xlsx]' (or openpyxl directly). The CSV "
            "path needs nothing."
        ) from exc

    try:
        wb = _open_workbook(openpyxl, path)
    except IngestError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"workbook could not be opened: {exc}") from exc
    try:
        names = list(wb.sheetnames)
        chosen = sheet or _best_sheet(wb, names)
        if chosen is None:
            raise IngestError(f"workbook has no readable sheet: {names}")
        if chosen not in names:
            raise IngestError(f"sheet {chosen!r} not in workbook; available: {names}")
        ws = wb[chosen]
        grid = [list(r) for r in ws.iter_rows(values_only=True)]
        det.detail["sheet"] = chosen
        det.detail["sheets"] = names
        t = _build(grid, det, sheet=chosen, available_sheets=names)
        return t
    finally:
        wb.close()


def _open_workbook(openpyxl, path: Path):
    """Load a workbook, working around openpyxl's extension check.

    openpyxl inspects the *filename suffix* before it opens the file, so a real
    workbook delivered as ``prod.csv`` is rejected even though detection already
    proved the bytes are a spreadsheet. An operator whose accounting package
    exports spreadsheets with the wrong extension is common enough that refusing
    would be a support call, so the bytes are re-staged under a real suffix.

    Detection has already established this is a workbook, so nothing is lost by
    doing this: the content check is the authoritative one and the suffix is
    only a hint the library insists on.
    """
    try:
        return openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        if "does not support" not in str(exc):
            raise
        staged = path.with_name(path.stem + ".haql_staged.xlsx")
        try:
            staged.write_bytes(path.read_bytes())
            return openpyxl.load_workbook(staged, read_only=True, data_only=True)
        finally:
            staged.unlink(missing_ok=True)


def _best_sheet(wb, names: list[str]) -> str | None:
    """Pick the sheet most likely to hold production rows.

    The first sheet is often a cover or a pivot, so candidates are scored on
    filled cells rather than position. A sheet whose first populated row looks
    like a header wins over a chart dump.
    """
    best = None
    best_score = -1
    for name in names[:12]:
        try:
            ws = wb[name]
            rows = ws.iter_rows(values_only=True, max_row=HEADER_SCAN_ROWS)
            grid = [list(r) for r in rows]
        except Exception:  # noqa: BLE001
            continue
        if not grid:
            continue
        hidx = _select_header(grid)
        header = [_clean_cell(c) for c in grid[hidx]]
        filled = sum(1 for c in header if c.strip())
        # Prefer a sheet that has many named columns: a pivot has one column.
        score = filled
        low = name.lower()
        if any(k in low for k in ("prod", "daily", "month", "volume", "data")):
            score += 5
        if score > best_score:
            best, best_score = name, score
    return best


# --------------------------------------------------------------------- ZIP


def read_zip(path: Path, det: Detection) -> Table:
    """Read the chosen member out of a zip.

    Only one member is read. Concatenating a monthly bundle would interleave
    unrelated periods under one header and produce findings that belong to no
    single month, so an ambiguous archive is surfaced by ``detect`` for the
    caller to resolve rather than merged silently here.
    """
    member = det.member
    if not member:
        raise IngestError(
            "zip archive contains no data entry; pass an explicit member"
        )
    with zipfile.ZipFile(path) as archive:
        try:
            data = archive.read(member)
        except KeyError as exc:
            raise IngestError(f"member {member!r} disappeared from {path}") from exc
        det.detail["members"] = det.candidates
    return _read_bytes(data, Path(member).name, det, det.format)


# -------------------------------------------------------------------- JSON


def read_json(path: Path, det: Detection) -> Table:
    """Read a JSON document or a JSON-lines file into a table.

    Two shapes are handled: a list of flat records, which is what an API export
    produces, and a list of records under a wrapper key. Nested objects are
    flattened one level with dotted names, because a QC rule cannot reason about
    a value it cannot name. A list of lists is treated as rows directly.
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if det.format == sniff.JSONL:
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise IngestError(f"line is not valid JSON: {exc}") from exc
    else:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IngestError(f"file is not valid JSON: {exc}") from exc
        records = _unwrap_json(doc)
        if records is None:
            raise IngestError(
                "JSON did not contain a list of records or an object with a "
                "list-valued key. Nested grid data needs a different adapter."
            )
    if not records:
        raise IngestError("JSON contained no records")

    if all(isinstance(r, list) for r in records):
        grid = [list(r) for r in records]
        grid[0] = [_clean_cell(c) for c in grid[0]]
        return _build(grid, det)

    flat = [_flatten(r) for r in records]
    columns: list[str] = []
    for rec in flat:
        for k in rec:
            if k not in columns:
                columns.append(k)
    grid = [columns] + [[rec.get(c, "") for c in columns] for rec in flat]
    return _build(grid, det)


def _unwrap_json(doc: Any) -> list | None:
    """Find the record list inside a JSON document.

    A single object that looks like one record is promoted to a one-element
    list, because that is what a single-well export usually is.
    """
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for key in ("data", "records", "rows", "results", "items", "production"):
            v = doc.get(key)
            if isinstance(v, list):
                return v
        if doc and all(not isinstance(v, (dict, list)) for v in doc.values()):
            return [doc]
    return None


def _flatten(record: dict, prefix: str = "") -> dict[str, Any]:
    """Flatten one level of nesting into dotted column names."""
    out: dict[str, Any] = {}
    for k, v in record.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, prefix=f"{name}."))
        elif isinstance(v, list):
            # A list inside a record is a nested table; its length is a number
            # we can honestly report, its contents we cannot flatten into a
            # well row.
            out[name] = str(len(v))
            out[f"{name}.count"] = len(v)
        else:
            out[name] = v
    return out


# --------------------------------------------------------------------- DBF


def read_dbf(path: Path, det: Detection) -> Table:
    """Read a dBase III/IV table with no third-party dependency.

    Legacy regulatory archives are distributed this way -- NYSDEC's pre-2000
    summary history is zipped ``.dbf`` -- and the format is small enough to
    read directly: a 32-byte header, 32-byte field descriptors, then fixed-width
    records each prefixed by a deletion flag.
    """
    data = path.read_bytes()
    if len(data) < 32:
        raise IngestError("file is too short to be a dBase table")

    version = data[0]
    n_records = struct.unpack_from("<I", data, 4)[0]
    header_len = struct.unpack_from("<H", data, 8)[0]
    record_len = struct.unpack_from("<H", data, 10)[0]

    fields: list[tuple[str, int]] = []
    off = 32
    while off + 32 <= header_len:
        if data[off] == 0x0D:
            break
        raw_name = data[off : off + 11]
        name = raw_name.split(b"\x00", 1)[0].decode("latin-1", errors="replace").strip()
        length = data[off + 16]
        if name:
            fields.append((name, length))
        off += 32
    if not fields:
        raise IngestError("dBase header declares no fields")

    if n_records and len(data) < header_len + n_records * record_len:
        det.notes.append(
            "dBase header claims more records than the file holds; truncated"
        )
        n_records = max(0, (len(data) - header_len) // record_len)

    rows: list[list[str]] = []
    for i in range(n_records):
        start = header_len + i * record_len
        if start + record_len > len(data):
            break
        rec = data[start : start + record_len]
        if rec[0:1] in (b"*", b" "):
            pass
        elif rec[0:1] == b"\x1a":
            continue
        pos = 1
        cells: list[str] = []
        for _name, length in fields:
            chunk = rec[pos : pos + length]
            pos += length
            cells.append(chunk.decode("latin-1", errors="replace").strip())
        rows.append(cells)

    det.detail["dbf_version"] = hex(version)
    det.detail["dbf_fields"] = [n for n, _ in fields]
    grid = [[n for n, _ in fields]] + rows
    if not rows:
        raise IngestError("dBase table holds no records")
    return _build(grid, det)


# ------------------------------------------------------------------ dispatch


def read_bytes_for_detection(path: Path, det: Detection) -> Table:
    """Read the table for an already-detected file."""
    fmt = det.format
    if fmt in (sniff.CSV, sniff.TSV):
        return read_csv(path, det)
    if fmt == sniff.FIXED:
        return read_fixed(path, det)
    if fmt == sniff.XLSX:
        return read_xlsx(path, det, det.detail.get("sheet"))
    if fmt == sniff.ZIP:
        return read_zip(path, det)
    if fmt in (sniff.JSON, sniff.JSONL):
        return read_json(path, det)
    if fmt == sniff.DBF:
        return read_dbf(path, det)
    if not det.supported:
        raise IngestError(det.unsupported_reason or f"unsupported format: {fmt}")
    raise IngestError(f"no adapter for format {fmt!r}")


def _read_bytes(data: bytes, name: str, det: Detection, hint: str) -> Table:
    """Read a table from raw bytes, for members inside an archive.

    The bytes are staged in a temporary file because two adapters need
    seekable paths (xlsx wants one, and the sheet picker wants to re-read).
    The suffix is taken from the *detected* content rather than the member name,
    so an openpyxl call is never handed a file called ``.csv`` -- openpyxl
    refuses on extension alone, which would turn a readable workbook into a
    confusing error about Excel.
    """
    import tempfile

    suffix = _SUFFIX_FOR_FORMAT.get(hint, ".bin")
    fd, tmp_name = tempfile.mkstemp(suffix=suffix, prefix="haql_ingest_")
    sub = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        inner = sniff.detect(sub)
        inner.member = None
        if inner.format != hint and inner.format in sniff.READABLE:
            det.notes.append(
                f"member {name!r} has extension implying {hint} but content "
                f"reads as {inner.format}"
            )
        return read_bytes_for_detection(sub, inner)
    finally:
        sub.unlink(missing_ok=True)


_SUFFIX_FOR_FORMAT = {
    sniff.CSV: ".csv",
    sniff.TSV: ".tsv",
    sniff.FIXED: ".txt",
    sniff.XLSX: ".xlsx",
    sniff.JSON: ".json",
    sniff.JSONL: ".jsonl",
    sniff.DBF: ".dbf",
}