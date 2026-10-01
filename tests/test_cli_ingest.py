"""CLI surface for the ingest path.

The flags here change *what gets read*, so they are the ones where a mistake is
invisible in the output: a wrong well column still produces findings, they are
just all wrong. These tests pin the behaviour that makes the difference visible
-- the mapping is printed, the source file is named in the report, and an
unreadable file is refused rather than reported as clean.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from haql_qc.cli import main

HEADER = ["WELLBORE", "Date", "Oil", "Water", "Gas"]
ROWS = [
    ["15/9-A-1", "2024-01-01", "100", "50", "2000"],
    ["15/9-A-1", "2024-01-02", "110", "55", "2100"],
    ["15/9-A-1", "2024-01-03", "105", "52", "2050"],
    ["15/9-A-2", "2024-01-01", "200", "90", "3000"],
    ["15/9-A-2", "2024-01-02", "-5", "90", "3000"],
]


def workdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="haql_cli_ingest_"))


def write_csv(d: Path, header: list[str] | None = None) -> Path:
    header = header or HEADER
    p = d / "prod.csv"
    p.write_text(
        "\n".join([",".join(header)] + [",".join(r) for r in ROWS]) + "\n",
        encoding="utf-8",
    )
    return p


def write_xlsx(d: Path) -> Path:
    try:
        import openpyxl  # type: ignore
    except ImportError:
        raise unittest.SkipTest("openpyxl not installed")
    wb = openpyxl.Workbook()
    cover = wb.active
    cover.title = "Cover"
    cover["A1"] = "ACME FIELD"
    sheet = wb.create_sheet("Production")
    for r in [HEADER] + ROWS:
        sheet.append(r)
    p = d / "prod.xlsx"
    wb.save(p)
    return p


def write_pdf(d: Path) -> Path:
    p = d / "paper.pdf"
    p.write_bytes(b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n")
    return p


def run(*argv: str) -> tuple[int, str]:
    """Invoke the CLI with its console output captured.

    The CLI is expected to talk to the operator on stdout and to stderr on
    failure; capturing it keeps the test log readable and lets the refusal
    tests assert on the message instead of just the exit code.
    """
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue() + err.getvalue()


class TestConvertFlag(unittest.TestCase):
    """--convert writes the canonical CSV and stops, with no ruleset run."""

    def test_convert_writes_canonical_csv_and_exits_zero(self):
        d = workdir()
        out = d / "canon.csv"
        code, _ = run("--ingest", "-i", str(write_csv(d)), "--convert", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(out.read_text().splitlines()[0], ",".join(HEADER))

    def test_convert_reports_the_mapping_it_used(self):
        """The point of --convert is to see how the file was read before
        trusting the numbers, so the mapping and cadence go to the console."""
        d = workdir()
        code, msg = run(
            "--ingest", "-i", str(write_csv(d)), "--convert", str(d / "c.csv")
        )
        self.assertEqual(code, 0)
        self.assertIn("well=WELLBORE", msg)
        self.assertIn("date=Date", msg)
        self.assertIn("cadence=daily", msg)

    def test_convert_names_the_format_it_read(self):
        d = workdir()
        code, msg = run("--ingest", "-i", str(write_csv(d)), "--convert", str(d / "c.csv"))
        self.assertEqual(code, 0)
        self.assertTrue(msg.startswith("csv -> "))

    def test_convert_surfaces_an_unresolved_role(self):
        """A missing role is the thing the operator most needs to see, so it is
        printed rather than left in the manifest only."""
        d = workdir()
        p = d / "nodate.csv"
        p.write_text(
            "WELLBORE,Oil,Water,Gas\n15/9-A-1,100,50,2000\n15/9-A-1,110,55,2100\n",
            encoding="utf-8",
        )
        code, msg = run("--ingest", "-i", str(p), "--convert", str(d / "c.csv"))
        self.assertEqual(code, 0)
        self.assertIn("unresolved", msg)

    def test_convert_does_not_write_an_audit_report(self):
        """A conversion is not an audit; writing audit.json beside it would
        imply findings that were never computed."""
        d = workdir()
        cwd = Path.cwd()
        os.chdir(d)
        try:
            run("--ingest", "-i", str(write_csv(d)), "--convert", str(d / "c.csv"))
            self.assertFalse((d / "audit.json").exists())
        finally:
            os.chdir(cwd)

    def test_convert_from_xlsx(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        d = workdir()
        out = d / "canon.csv"
        code, _ = run("--ingest", "-i", str(write_xlsx(d)), "--convert", str(out))
        self.assertEqual(code, 0)
        self.assertEqual(out.read_text().splitlines()[0], ",".join(HEADER))


class TestExplainFlag(unittest.TestCase):
    def test_explain_prints_a_manifest_with_the_source_and_mapping(self):
        d = workdir()
        report = d / "audit.json"
        code, _ = run("--ingest", "--explain", "-i", str(write_csv(d)), "-r", str(report))
        self.assertEqual(code, 0)
        payload = json.loads(report.read_text())
        self.assertIn("ingest", payload["config"])
        self.assertIn("schema", payload["config"]["ingest"])

    def test_ingest_records_the_original_source_not_a_temporary_file(self):
        """The audit trail is only evidence if it points at the file the
        operator handed over."""
        d = workdir()
        src = write_csv(d)
        report = d / "audit.json"
        run("--ingest", "-i", str(src), "-r", str(report))
        payload = json.loads(report.read_text())
        self.assertEqual(payload["input"]["path"], str(src.resolve()))
        self.assertEqual(
            payload["config"]["ingest"]["source"]["sha256"], payload["input"]["sha256"]
        )


class TestRefusal(unittest.TestCase):
    """An unreadable file must fail. Reporting zero findings instead produces a
    clean bill of health for a file nobody read."""

    def test_pdf_exits_with_a_usage_error(self):
        d = workdir()
        code, msg = run("--ingest", "-i", str(write_pdf(d)))
        self.assertEqual(code, 2)
        self.assertIn("CSV or XLSX", msg)

    def test_missing_file_exits_with_a_usage_error(self):
        d = workdir()
        code, msg = run("--ingest", "-i", str(d / "nope.csv"))
        self.assertEqual(code, 2)
        self.assertIn("nope.csv", msg)

    def test_header_only_file_is_refused(self):
        d = workdir()
        p = d / "headeronly.csv"
        p.write_text(",".join(HEADER) + "\n")
        code, msg = run("--ingest", "-i", str(p))
        self.assertEqual(code, 2)
        self.assertIn("data rows", msg)

    def test_refusal_does_not_write_a_report(self):
        d = workdir()
        report = d / "audit.json"
        run("--ingest", "-i", str(write_pdf(d)), "-r", str(report))
        self.assertFalse(report.exists(), "a refused run must not leave a report")


class TestRulesStillApply(unittest.TestCase):
    def test_ingest_runs_the_same_ruleset_and_finds_the_negative(self):
        d = workdir()
        report = d / "audit.json"
        code, _ = run("--ingest", "-i", str(write_csv(d)), "-r", str(report))
        payload = json.loads(report.read_text())
        self.assertIn("QC010_NEGATIVE_RATE", payload["summary"]["by_rule"])
        self.assertEqual(payload["summary"]["rows_read"], len(ROWS))
        self.assertEqual(payload["summary"]["wells_seen"], 2)
        self.assertEqual(code, 0)

    def test_ingest_on_xlsx_matches_ingest_on_equivalent_csv(self):
        """Same table, same findings. If these diverge, an adapter is dropping
        or reshaping data."""
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        d = workdir()
        csv_report = d / "csv.json"
        xlsx_report = d / "xlsx.json"
        run("--ingest", "-i", str(write_csv(d)), "-r", str(csv_report))
        run("--ingest", "-i", str(write_xlsx(d)), "-r", str(xlsx_report))
        a = json.loads(csv_report.read_text())["summary"]["by_rule"]
        b = json.loads(xlsx_report.read_text())["summary"]["by_rule"]
        self.assertEqual(a, b)


class TestExplicitOverrides(unittest.TestCase):
    def test_well_and_date_overrides_are_honoured(self):
        d = workdir()
        p = write_csv(d, ["Bore", "Day", "Oil_bbl_per_d", "Water_bbl_per_d", "Gas_Mcf_per_d"])
        report = d / "audit.json"
        run("--ingest", "-i", str(p), "-r", str(report))
        payload = json.loads(report.read_text())
        self.assertEqual(payload["config"]["well_column"], "Bore")
        self.assertEqual(payload["config"]["date_column"], "Day")

    def test_override_is_recorded_as_verified(self):
        d = workdir()
        p = write_csv(d, ["Bore", "Day", "Oil_bbl_per_d", "Water_bbl_per_d", "Gas_Mcf_per_d"])
        report = d / "audit.json"
        run(
            "--ingest", "-i", str(p), "-r", str(report),
            "--well-column", "Bore", "--date-column", "Day",
        )
        payload = json.loads(report.read_text())
        roles = payload["config"]["ingest"]["schema"]["roles"]
        self.assertTrue(roles["well"]["verified"])
        self.assertTrue(roles["date"]["verified"])

    def test_default_flag_values_are_not_treated_as_overrides(self):
        """--well-column defaults to 'well_id' for the plain loader. Passing
        that default through to the mapper would mark every role verified and
        break files whose real column is called something else."""
        d = workdir()
        p = write_csv(d, ["Bore", "Day", "Oil_bbl_per_d", "Water_bbl_per_d", "Gas_Mcf_per_d"])
        report = d / "audit.json"
        run("--ingest", "-i", str(p), "-r", str(report))
        payload = json.loads(report.read_text())
        roles = payload["config"]["ingest"]["schema"]["roles"]
        self.assertFalse(roles["well"]["verified"])
        self.assertEqual(roles["well"]["column"], "Bore")

    def test_a_wrong_override_is_accepted_and_then_produces_no_findings(self):
        """This is why the mapping has to be inspectable. A wrong column name is
        not an error -- it is a silent zero. --explain exists so the operator
        can catch it."""
        d = workdir()
        p = write_csv(d, ["Bore", "Day", "Oil_bbl_per_d", "Water_bbl_per_d", "Gas_Mcf_per_d"])
        report = d / "audit.json"
        code, _ = run(
            "--ingest", "-i", str(p), "-r", str(report), "--well-column", "DoesNotExist"
        )
        payload = json.loads(report.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(payload["summary"]["wells_seen"], 0)


class TestIngestIsOptIn(unittest.TestCase):
    """--ingest stays off by default so a mistyped flag cannot quietly change
    which loader runs."""

    def test_csv_still_loads_without_the_ingest_flag(self):
        d = workdir()
        p = d / "prod.csv"
        p.write_text(
            "well_id,date,oil_bbl_per_d\nW-1,2024-01-01,100\nW-1,2024-01-02,110\n",
            encoding="utf-8",
        )
        report = d / "audit.json"
        code, _ = run("-i", str(p), "-r", str(report))
        self.assertEqual(code, 0)
        payload = json.loads(report.read_text())
        self.assertNotIn("ingest", payload["config"])


if __name__ == "__main__":
    unittest.main()