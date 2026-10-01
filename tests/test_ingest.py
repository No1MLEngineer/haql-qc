"""Ingest: arbitrary export in, canonical CSV out.

Three failure modes drive this file's tests, and none of them is "wrong
number". All three are cases where a plausible-looking report would come back
from a file the tool never really understood:

* silent format refusal -- a workbook named ``.csv``, a PDF, an empty file. A
  tool that declines loudly is safe; one that reads zero rows and reports zero
  findings is not, because a clean report on an empty read is indistinguishable
  from a clean report on a healthy field.
* silent schema error -- picking the wrong well column, or reading a cumulative
  meter as a per-day rate, still returns findings. They are just all wrong.
  So every inferred role has to be traceable to its evidence.
* silent unit loss -- Sm3 vs bbl changes a reading by 6.3x. An undeclared unit
  must stay undeclared all the way to the report rather than being guessed.

Fixtures are written by hand rather than read from the Volve archive so the
expected values are stated by the test, not by whatever the fixture happens to
contain.
"""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from haql_qc.ingest import IngestError, detect, ingest_file, load_any, sniff_file
from haql_qc.ingest.sniff import CSV, DBF, JSON, JSONL, PDF, TSV, XLSX, ZIP
from haql_qc.rules import run_all

HEADER = ["WELLBORE", "Date", "Oil", "Water", "Gas"]
ROWS = [
    ["15/9-A-1", "2024-01-01", "100", "50", "2000"],
    ["15/9-A-1", "2024-01-02", "110", "55", "2100"],
    ["15/9-A-1", "2024-01-03", "105", "52", "2050"],
    ["15/9-A-2", "2024-01-01", "200", "90", "3000"],
    ["15/9-A-2", "2024-01-02", "-5", "90", "3000"],
]


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="haql_ingest_test_"))


def write_csv(d: Path, name: str = "prod.csv", delimiter: str = ",") -> Path:
    p = d / name
    lines = [delimiter.join(HEADER)] + [delimiter.join(r) for r in ROWS]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_dbf(d: Path) -> Path:
    """A minimal dBase III table: 32-byte header, field descriptors, records."""
    # Field names are fixed to 10 characters, so DT is the date field's legal
    # name in this format. Everything else carries the header's meaning.
    fields = [
        ("WELLBORE", "C", 12),
        ("DT", "C", 10),
        ("OIL", "N", 10),
        ("WATR", "N", 10),
        ("GAS", "N", 12),
    ]
    nrec = len(ROWS)
    hlen = 32 + 32 * len(fields) + 1
    rlen = 1 + sum(f[2] for f in fields)

    hdr = bytearray([0x03, 124, 1, 1])
    hdr += struct.pack("<I", nrec)
    hdr += struct.pack("<H", hlen)
    hdr += struct.pack("<H", rlen)
    hdr += bytes(20)
    for name, typ, ln in fields:
        hdr += name.encode("latin-1").ljust(11, b"\x00")
        hdr += typ.encode("latin-1") + bytes(4) + bytes([ln, 0]) + bytes(14)
    hdr += b"\x0d"

    body = bytearray()
    for row in ROWS:
        body += b" "
        for (name, typ, ln), v in zip(fields, row):
            if typ == "C":
                body += v.encode("latin-1").ljust(ln)[:ln]
            else:
                # dBase stores numerics right-justified text and writes a whole
                # number without a decimal part, so 100 is stored as "100".
                f = float(v)
                body += (str(int(f)) if f.is_integer() else str(f)).rjust(ln).encode("latin-1")

    p = d / "prod.dbf"
    p.write_bytes(bytes(hdr) + bytes(body))
    return p


def write_xlsx(d: Path, name: str = "prod.xlsx") -> Path:
    try:
        import openpyxl  # type: ignore
    except ImportError:
        raise unittest.SkipTest("openpyxl not installed")
    wb = openpyxl.Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover["A1"] = "ACME FIELD - PRODUCTION SUMMARY"
    cover["A2"] = "generated 2024-02-01"
    sheet = wb.create_sheet("Production")
    # Title and report date sit on the data sheet too, which is how these
    # workbooks actually arrive.
    sheet["A1"] = "ACME FIELD - PRODUCTION SUMMARY"
    sheet["A2"] = "generated 2024-02-01"
    for r in [HEADER] + ROWS:
        sheet.append(r)
    p = d / name
    wb.save(p)
    return p


def write_zip(d: Path, name: str = "prod.zip", member: str = "prod_data.csv") -> Path:
    p = d / name
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", "not data, and deliberately the first entry")
        z.writestr(member, "\n".join([",".join(HEADER)] + [",".join(r) for r in ROWS]))
    return p


def write_json(d: Path) -> Path:
    records = [dict(zip(HEADER, r)) for r in ROWS]
    p = d / "prod.json"
    p.write_text(json.dumps(records), encoding="utf-8")
    return p


def write_jsonl(d: Path) -> Path:
    records = [dict(zip(HEADER, r)) for r in ROWS]
    p = d / "prod.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


# --------------------------------------------------------------- detection


class TestFormatDetection(unittest.TestCase):
    """Detection reads bytes. An extension is a claim, not evidence."""

    def test_csv_is_detected(self):
        det = detect(write_csv(tmpdir()))
        self.assertEqual(det.format, CSV)
        self.assertTrue(det.supported)

    def test_tab_delimited_is_detected_as_tsv(self):
        p = tmpdir() / "prod.tsv"
        p.write_text(
            "\n".join(["\t".join(HEADER)] + ["\t".join(r) for r in ROWS]),
            encoding="utf-8",
        )
        self.assertEqual(detect(p).format, TSV)

    def test_semicolon_is_detected_and_the_delimiter_recorded(self):
        det = detect(write_csv(tmpdir(), delimiter=";"))
        self.assertEqual(det.format, CSV)
        self.assertEqual(det.detail.get("source_delimiter"), ";")

    def test_xlsx_is_detected_as_a_workbook_not_a_zip(self):
        """An xlsx is a zip container, so detection has to go one level deeper.

        Reading it with the archive adapter would parse the workbook's internal
        XML and report on the file format.
        """
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        det = detect(write_xlsx(tmpdir()))
        self.assertEqual(det.format, XLSX)

    def test_extension_does_not_override_content(self):
        """A workbook named .csv must be read as a workbook."""
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        src = write_xlsx(tmpdir())
        misnamed = src.with_name("prod.csv")
        misnamed.write_bytes(src.read_bytes())
        self.assertEqual(detect(misnamed).format, XLSX)

    def test_docx_is_refused_rather_than_parsed_as_a_table(self):
        p = tmpdir() / "report.docx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("word/document.xml", "<document/>")
        det = detect(p)
        self.assertFalse(det.supported, "a word document is not production data")

    def test_pdf_is_refused_with_a_reason(self):
        p = tmpdir() / "paper.pdf"
        p.write_bytes(b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n")
        det = detect(p)
        self.assertEqual(det.format, PDF)
        self.assertFalse(det.supported)
        self.assertIn("CSV", det.unsupported_reason)

    def test_zip_member_choice_prefers_csv_over_a_readme(self):
        det = detect(write_zip(tmpdir()))
        self.assertEqual(det.format, ZIP)
        self.assertEqual(det.member, "prod_data.csv")

    def test_zip_with_one_data_entry_is_not_flagged_ambiguous(self):
        p = tmpdir() / "one.zip"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("data.csv", "a,b\n1,2\n")
        self.assertFalse(detect(p).detail.get("ambiguous", False))

    def test_dbf_is_detected_and_validated_structurally(self):
        self.assertEqual(detect(write_dbf(tmpdir())).format, DBF)

    def test_a_text_file_is_not_mistaken_for_dbf(self):
        """One version byte is a 1-in-256 guess; header lengths must also agree."""
        p = tmpdir() / "looks_like.csv"
        p.write_text("\x03\x7c\x01\x01" + ",".join(HEADER) + "\n", encoding="latin-1")
        self.assertNotEqual(detect(p).format, DBF)

    def test_json_and_jsonl_are_distinguished(self):
        self.assertEqual(detect(write_json(tmpdir())).format, JSON)
        self.assertEqual(detect(write_jsonl(tmpdir())).format, JSONL)

    def test_empty_file_is_refused(self):
        p = tmpdir() / "empty.csv"
        p.write_text("")
        self.assertFalse(detect(p).supported)

    def test_missing_file_is_refused_not_raised(self):
        det = detect(tmpdir() / "nope.csv")
        self.assertFalse(det.supported)
        self.assertIn("not found", det.reason)

    def test_every_detection_states_a_reason(self):
        """An unexplained detection is not auditable."""
        for maker in (write_csv, write_dbf, write_json, write_jsonl):
            det = detect(maker(tmpdir()))
            self.assertTrue(det.reason, f"{det.format} detection gave no reason")


# ---------------------------------------------------------------- ingestion


class TestIngestFormats(unittest.TestCase):
    """Every supported format must yield the same table, because the rules
    downstream cannot tell which one the data came from."""

    def assert_same_table(self, path: Path, columns: list[str] | None = None) -> None:
        r = ingest_file(path)
        self.assertEqual(r.table.columns, columns or HEADER)
        self.assertEqual(len(r.table.rows), len(ROWS))
        self.assertEqual(r.table.rows[0], ROWS[0])

    def test_csv(self):
        self.assert_same_table(write_csv(tmpdir()))

    def test_xlsx(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        self.assert_same_table(write_xlsx(tmpdir()))

    def test_zip_of_csv(self):
        self.assert_same_table(write_zip(tmpdir()))

    def test_json(self):
        self.assert_same_table(write_json(tmpdir()))

    def test_jsonl(self):
        self.assert_same_table(write_jsonl(tmpdir()))

    def test_dbf(self):
        """dBase field names are capped at 10 characters, so the date column
        arrives as DT. The values are what matter; the name is what the format
        permits."""
        self.assert_same_table(
            write_dbf(tmpdir()), ["WELLBORE", "DT", "OIL", "WATR", "GAS"]
        )

    def test_semicolon_delimited(self):
        self.assert_same_table(write_csv(tmpdir(), delimiter=";"))

    def test_tab_delimited(self):
        p = tmpdir() / "prod.tsv"
        p.write_text(
            "\n".join(["\t".join(HEADER)] + ["\t".join(r) for r in ROWS]),
            encoding="utf-8",
        )
        self.assert_same_table(p)

    def test_bom_does_not_hide_the_first_column(self):
        """An Excel export with a BOM must not lose its well column."""
        p = tmpdir() / "bom.csv"
        p.write_bytes(
            "﻿".encode("utf-8")
            + ",".join(HEADER).encode("utf-8")
            + b"\n"
            + b"\n".join(",".join(r).encode("utf-8") for r in ROWS)
        )
        r = ingest_file(p)
        self.assertEqual(r.well_column, "WELLBORE")

    def test_xlsx_title_rows_are_skipped(self):
        """A workbook opening with a report title is common; reading row 1 as
        the header would yield a one-column table and a confidently wrong report."""
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        r = ingest_file(write_xlsx(tmpdir()))
        self.assertEqual(r.table.columns, HEADER)
        self.assertGreater(r.table.header_line, 1)

    def test_correct_sheet_is_chosen_from_a_workbook(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        r = ingest_file(write_xlsx(tmpdir()))
        self.assertEqual(r.table.sheet, "Production")
        self.assertIn("Cover", r.table.available_sheets)

    def test_header_only_file_is_refused(self):
        """A header with no rows cannot be audited. Returning zero findings for
        it would look like a clean bill of health."""
        p = tmpdir() / "headeronly.csv"
        p.write_text(",".join(HEADER) + "\n")
        with self.assertRaises(IngestError):
            ingest_file(p)

    def test_empty_file_is_refused(self):
        p = tmpdir() / "empty.csv"
        p.write_text("")
        with self.assertRaises(IngestError):
            ingest_file(p)

    def test_pdf_is_refused_by_ingest_too(self):
        p = tmpdir() / "paper.pdf"
        p.write_bytes(b"%PDF-1.4\n%%EOF\n")
        with self.assertRaises(IngestError) as ctx:
            ingest_file(p)
        self.assertIn("not a table", str(ctx.exception))


# ------------------------------------------------------------ schema mapping


class TestSchemaMapping(unittest.TestCase):
    """A wrong role still produces findings, just all of them wrong. So every
    inferred role has to be traceable to evidence."""

    def test_roles_are_found_without_configuration(self):
        r = ingest_file(write_csv(tmpdir()))
        self.assertEqual(r.well_column, "WELLBORE")
        self.assertEqual(r.date_column, "Date")

    def test_fluids_are_mapped(self):
        r = ingest_file(write_csv(tmpdir()))
        self.assertEqual(r.schema.column_for("oil"), "Oil")
        self.assertEqual(r.schema.column_for("water"), "Water")
        self.assertEqual(r.schema.column_for("gas"), "Gas")

    def test_every_assignment_carries_evidence(self):
        r = ingest_file(write_csv(tmpdir()))
        for role, a in r.schema.roles.items():
            self.assertTrue(a.basis, f"role {role} has no stated basis")

    def test_operator_override_wins_and_is_marked_verified(self):
        p = write_csv(tmpdir(), delimiter=";")
        r = ingest_file(p, well_column="Date", date_column="WELLBORE")
        self.assertEqual(r.well_column, "Date")
        self.assertTrue(r.schema.roles["well"].verified)

    def test_manifest_records_the_source_and_its_hash(self):
        p = write_csv(tmpdir())
        m = ingest_file(p).manifest()
        self.assertEqual(m["source"]["sha256"], ingest_file(p).source_sha256)
        self.assertEqual(m["source"]["detected_format"], CSV)

    def test_source_file_is_never_modified(self):
        p = write_csv(tmpdir())
        before = p.read_bytes()
        ingest_file(p)
        self.assertEqual(p.read_bytes(), before, "ingest must not touch the input")


# ------------------------------------------------------- honesty properties


class TestNoSilentGuessing(unittest.TestCase):
    """The properties that stop a clean-looking report from being a lie."""

    def test_undeclared_units_stay_undeclared(self):
        """A column with a fluid name but no unit must not acquire one."""
        r = ingest_file(write_csv(tmpdir()))
        self.assertNotIn("Oil", r.units)
        self.assertEqual(r.units, {})

    def test_units_row_is_honoured_and_attributed_to_the_file(self):
        header = ["WELLBORE", "Date", "Oil_bbl_per_d", "Water_bbl_per_d", "Gas_Mcf_per_d"]
        lines = [
            ",".join(header),
            ",,bbl/d,bbl/d,Mcf/d",
            "15/9-A-1,2024-01-01,100,50,2000",
            "15/9-A-1,2024-01-02,110,55,2100",
        ]
        p = tmpdir() / "units.csv"
        p.write_text("\n".join(lines) + "\n")
        r = ingest_file(p)
        self.assertEqual(r.units["Oil_bbl_per_d"], "bbl/d")
        self.assertEqual(r.schema.unit_sources["Oil_bbl_per_d"], "file_units_row")
        self.assertNotIn("Oil_bbl_per_d", r.schema.operator_units)
        self.assertEqual(r.schema.operator_units, {})

    def test_units_row_is_not_audited_as_data(self):
        """Otherwise the units row becomes a row of unparseable numbers."""
        header = ["WELLBORE", "Date", "Oil_bbl_per_d"]
        p = tmpdir() / "units.csv"
        p.write_text(
            "\n".join([",".join(header), ",,bbl/d", "15/9-A-1,2024-01-01,100"]) + "\n",
            encoding="utf-8",
        )
        r = ingest_file(p)
        self.assertEqual(len(r.table.rows), 1)
        self.assertEqual(r.table.rows[0][0], "15/9-A-1")

    def test_operator_units_are_kept_separate_from_file_units(self):
        """A reviewer asking 'who said this was Sm3?' must get an answer."""
        r = ingest_file(write_csv(tmpdir()), unit_declarations={"Oil": "Sm3"})
        self.assertEqual(r.units["Oil"], "Sm3")
        self.assertEqual(r.schema.operator_units["Oil"], "Sm3")
        self.assertEqual(r.schema.units, {}, "operator units are not file declarations")

    def test_unknown_unit_is_refused_not_written_through(self):
        with self.assertRaises(IngestError):
            ingest_file(write_csv(tmpdir()), unit_declarations={"Oil": "furlongs"})

    def test_a_period_file_does_not_report_the_date_role_as_missing(self):
        header = ["Wellbore name", "NPDCode", "Year", "Month", "On Stream", "Oil", "Gas", "Water"]
        units = ["", "", "", "", "hrs", "Sm3", "Sm3", "Sm3"]
        rows = [
            ["15/9-F-1 C", "7405", "2014", "4", "227.5", "11142", "1597936", "0"],
            ["15/9-F-1 C", "7405", "2014", "5", "733.8", "24901", "3496229", "783"],
        ]
        p = tmpdir() / "monthly.csv"
        p.write_text(
            "\n".join([",".join(header), ",".join(units)] + [",".join(r) for r in rows]) + "\n",
            encoding="utf-8",
        )
        r = ingest_file(p)
        self.assertEqual(r.schema.cadence.granularity, "period")
        self.assertEqual(r.year_column, "Year")
        self.assertEqual(r.month_column, "Month")
        self.assertNotIn("date", r.schema.unresolved)

    def test_low_confidence_guess_is_not_reported_as_a_finding(self):
        """An ambiguous name match must surface as an open question, not a role."""
        header = ["name", "field_name", "Oil_bbl_per_d", "Gas_Mcf_per_d"]
        p = tmpdir() / "ambiguous.csv"
        p.write_text(
            "\n".join([",".join(header)] + [",".join(r) for r in ROWS]) + "\n",
            encoding="utf-8",
        )
        r = ingest_file(p)
        # 'name' is ambiguous and 'field_name' is not a well at all, so the well
        # role must not be assigned on the name alone.
        self.assertIsNone(r.well_column)
        self.assertIn("well", r.schema.unresolved)


# ------------------------------------------------------------ rules parity


class TestRulesRunOnAnything(unittest.TestCase):
    """The point of ingest is that the existing rules work on the output
    unchanged. Verified by finding the same defect regardless of format."""

    def find_negative_oil(self, path: Path) -> list[str]:
        rows, cols, _ = load_any(path)
        return [i.rule_id for i in run_all(rows, cols) if i.well_id == "15/9-A-2"]

    def test_the_negative_oil_row_is_found_in_every_format(self):
        """ROWS contains one -5 oil reading. It must be caught whatever the
        container, or a format-specific adapter is quietly losing data."""
        cases = [
            write_csv(tmpdir()),
            write_zip(tmpdir()),
            write_json(tmpdir()),
            write_jsonl(tmpdir()),
            write_dbf(tmpdir()),
        ]
        try:
            import openpyxl  # noqa: F401

            cases.append(write_xlsx(tmpdir()))
        except ImportError:
            pass

        for p in cases:
            with self.subTest(fmt=p.suffix):
                self.assertIn(
                    "QC010_NEGATIVE_RATE",
                    self.find_negative_oil(p),
                    f"negative reading not found in {p.name}",
                )

    def test_ingested_file_produces_the_expected_row_and_well_counts(self):
        rows, _cols, report = load_any(write_csv(tmpdir()))
        self.assertEqual(report.rows_read, len(ROWS))
        self.assertEqual(report.wells_seen, 2)

    def test_load_any_records_ingest_provenance_in_the_report(self):
        """A report that cannot be traced to the file it audited is not evidence."""
        p = write_csv(tmpdir())
        _rows, _cols, report = load_any(p)
        self.assertEqual(report.config["ingest"]["source"]["path"], str(p.resolve()))
        self.assertEqual(report.config["ingest"]["source"]["detected_format"], CSV)
        self.assertIn("schema", report.config["ingest"])

    def test_load_any_refuses_when_no_well_column_can_be_found(self):
        header = ["alpha", "beta", "gamma"]
        p = tmpdir() / "nowell.csv"
        p.write_text(",".join(header) + "\n" + "1,2,3\n4,5,6\n", encoding="utf-8")
        with self.assertRaises(IngestError) as ctx:
            load_any(p)
        self.assertIn("well identifier", str(ctx.exception))

    def test_load_any_refuses_when_no_date_column_can_be_found(self):
        """The well resolves but no time axis exists.

        Date-gap, shut-in and cumulate-jump are all date-scoped. Running them
        over rows with no dates would produce a report that looks complete and
        silently skipped half the ruleset, so the call is refused instead.
        """
        header = ["WELLBORE", "alpha", "beta"]
        p = tmpdir() / "nodate.csv"
        p.write_text(",".join(header) + "\n" + "W-1,1,2\nW-1,3,4\n", encoding="utf-8")
        with self.assertRaises(IngestError) as ctx:
            load_any(p)
        self.assertIn("date column", str(ctx.exception))


# ------------------------------------------------------------ determinism


class TestDeterminism(unittest.TestCase):
    """Same bytes in, same bytes and same report out. A report that cannot be
    reproduced cannot be defended in an audit."""

    def test_canonical_csv_is_byte_identical_across_runs(self):
        p = write_csv(tmpdir())
        self.assertEqual(ingest_file(p).csv_text, ingest_file(p).csv_text)

    def test_canonical_csv_is_a_valid_csv(self):
        import csv
        import io

        r = ingest_file(write_csv(tmpdir()))
        parsed = list(csv.reader(io.StringIO(r.csv_text)))
        self.assertEqual(parsed[0], HEADER)
        self.assertEqual(len(parsed), len(ROWS) + 1)

    def test_canonical_csv_uses_lf_newlines(self):
        """The output's hash goes in the audit record, so the line ending has to
        be fixed or the same file hashes differently per platform."""
        self.assertNotIn("\r", ingest_file(write_csv(tmpdir())).csv_text)


if __name__ == "__main__":
    unittest.main()