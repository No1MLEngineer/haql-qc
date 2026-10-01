"""Unit tests for the QC rules and audit report.

Run: python -m unittest tests.test_qc -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from volve_qc.cli import main as cli_main
from volve_qc.loader import load_production_csv
from volve_qc.rules import (
    RULES,
    _is_injection,
    _rate_kind,
    _volume_unit,
    run_all,
)
from volve_qc.schema import AuditReport, Correction, Issue, Severity
from volve_qc.units import normalise_unit_token, to_canonical_rate


HEADER = "well_id,date,oil_bbl_per_d,water_bbl_per_d,gas_mcf_per_d,oil_cum_bbl,days_on"


def write_csv(lines: list[str]) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "production.csv"
    p.write_text(HEADER + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    return p


def run(lines: list[str]):
    rows, cols, rep = load_production_csv(write_csv(lines))
    issues = run_all(rows, cols)
    return rows, cols, rep, issues


def ids(issues, rule_id=None):
    return sorted(i.rule_id for i in issues if rule_id is None or i.rule_id == rule_id)


# The real column names of the Volve production export, plus the shapes a US
# export uses. Every one of these was misclassified at some point, so the whole
# table is pinned rather than just the cases that were caught in review.
COLUMN_INTERPRETATION = [
    # (column, kind, is_injection, declared_unit)
    ("BORE_OIL_VOL", "oil", False, None),
    ("BORE_GAS_VOL", "gas", False, None),
    ("BORE_WAT_VOL", "water", False, None),
    ("BORE_WI_VOL", "water", True, None),
    ("GI", "gas", True, None),
    ("WI", "water", True, None),
    ("Oil", "oil", False, None),
    ("Gas", "gas", False, None),
    ("Water", "water", False, None),
    ("oil_bbl_per_d", "oil", False, "bbl"),
    ("water_bbl_per_d", "water", False, "bbl"),
    ("gas_mcf_per_d", "gas", False, "MCF"),
    ("oil_sm3", "oil", False, "Sm3"),
    ("gas_sm3", "gas", False, "Sm3"),
    ("water_sm3", "water", False, "Sm3"),
    ("gi_sm3", "gas", True, "Sm3"),
    ("wi_sm3", "water", True, "Sm3"),
    # A cumulative total is not a rate, whatever fluid it holds.
    ("oil_cum_bbl", None, False, "bbl"),
    ("gas_cum_mcf", None, False, "MCF"),
    # Metadata must never be read as a rate.
    ("ON_STREAM_HRS", None, False, None),
    ("Year", None, False, None),
    ("Month", None, False, None),
    ("AVG_CHOKE_UOM", None, False, None),
    ("WELL_TYPE", None, False, None),
    ("FLOW_KIND", None, False, None),
]


class TestUnitCoverageIsHonest(unittest.TestCase):
    """A skipped check must never be able to look like a clean one."""

    VOLVE_HEADER = (
        "WELL_BORE_CODE,DATEPRD,ON_STREAM_HRS,BORE_OIL_VOL,BORE_WAT_VOL,BORE_WI_VOL"
    )
    VOLVE_ROWS = [
        # 40,000 Sm3 over 24h. As Sm3/d that is 251,592 bbl/d, over the
        # 200,000 ceiling. As bbl/d the same number is comfortably under it.
        # The declared unit, and nothing else, decides the verdict.
        "NO 1/2-3,2024-01-01,24,40000,1000,0",
        "NO 1/2-3,2024-01-02,24,41000,1100,0",
        "NO 1/2-3,2024-01-03,24,39000,1200,0",
    ]

    def _load(self, lines):
        d = Path(tempfile.mkdtemp())
        p = d / "volve.csv"
        p.write_text(self.VOLVE_HEADER + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
        return load_production_csv(
            p, well_column="WELL_BORE_CODE", date_column="DATEPRD"
        )

    def test_undeclared_unit_is_reported_not_silently_skipped(self):
        rows, cols, _rep = self._load(self.VOLVE_ROWS)
        issues = run_all(rows, cols)
        qc015 = [i for i in issues if i.rule_id == "QC015_IMPLAUSIBLE_RATE"]
        # Nothing breaches the ceiling, so there is one INFO naming the gap.
        self.assertEqual(len(qc015), 1)
        info = qc015[0]
        self.assertEqual(info.severity, Severity.INFO)
        self.assertIn("BORE_OIL_VOL", info.context["skipped_columns"])
        self.assertIn("BORE_WAT_VOL", info.context["skipped_columns"])

    def test_declared_unit_converts_before_comparing(self):
        """Declaring Sm3 must compare 30,000 Sm3/d against a bbl/d ceiling."""
        rows, cols, _rep = self._load(self.VOLVE_ROWS)
        issues = run_all(
            rows,
            cols,
            unit_declarations={
                "BORE_OIL_VOL": "Sm3",
                "BORE_WAT_VOL": "Sm3",
                "BORE_WI_VOL": "Sm3",
            },
        )
        breaches = [
            i
            for i in issues
            if i.rule_id == "QC015_IMPLAUSIBLE_RATE" and i.severity != Severity.INFO
        ]
        self.assertEqual(len(breaches), 3, "40,000 Sm3/d is ~251,600 bbl/d")
        first = breaches[0]
        self.assertEqual(first.unit, "bbl/d")
        self.assertAlmostEqual(first.observed, 40000.0 * 6.289810770432105, places=1)
        self.assertEqual(first.context["canonical_unit"], "bbl")
        self.assertEqual(first.context["raw_unit"], "Sm3")
        self.assertTrue(first.context["normalised"])
        self.assertTrue(
            first.context["conversion_note"],
            "a converted reading must state the conditions it assumes",
        )

    def test_same_reading_different_declared_unit_different_verdict(self):
        """The declared unit is the only thing that decides the outcome.

        Same numbers, same ceiling: declaring the column bbl leaves the reading
        under the limit, declaring it Sm3 puts it over. If the tool ever
        defaults, this is the test that catches it.
        """
        rows, cols, _rep = self._load(self.VOLVE_ROWS)
        as_bbl = run_all(
            rows, cols, unit_declarations={c: "bbl" for c in ("BORE_OIL_VOL",)}
        )
        as_sm3 = run_all(
            rows, cols, unit_declarations={c: "Sm3" for c in ("BORE_OIL_VOL",)}
        )
        n_bbl = sum(
            1
            for i in as_bbl
            if i.rule_id == "QC015_IMPLAUSIBLE_RATE" and i.severity != Severity.INFO
        )
        n_sm3 = sum(
            1
            for i in as_sm3
            if i.rule_id == "QC015_IMPLAUSIBLE_RATE" and i.severity != Severity.INFO
        )
        self.assertEqual(n_bbl, 0)
        self.assertEqual(n_sm3, 3)

    def test_negative_produced_water_is_high_severity(self):
        """Produced water going negative is not realisable, and must not be
        softened on the grounds that injection sometimes runs negative."""
        rows, cols, _rep = self._load(
            [
                "NO 1/2-3,2024-01-01,24,30000,-5,0",
                "NO 1/2-3,2024-01-02,24,30000,10,0",
                "NO 1/2-3,2024-01-03,24,30000,12,0",
            ]
        )
        issues = [i for i in run_all(rows, cols) if i.rule_id == "QC010_NEGATIVE_RATE"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].severity, Severity.HIGH)
        self.assertEqual(issues[0].column, "BORE_WAT_VOL")
        self.assertFalse(issues[0].context["injection"])


class TestColumnInterpretation(unittest.TestCase):
    """A column's name is the only evidence of what it holds."""

    def test_fluid_injection_and_unit(self):
        for column, kind, injected, unit in COLUMN_INTERPRETATION:
            with self.subTest(column=column):
                self.assertEqual(_rate_kind(column), kind)
                self.assertEqual(_is_injection(column), injected)
                self.assertEqual(_volume_unit(column), unit)

    def test_bare_fluid_name_is_not_a_unit_declaration(self):
        """A fluid name says what is measured, not in what.

        Inferring bbl from "oil" put a wrong unit into the audit record for the
        Volve export, which is in Sm3. An undeclared column must resolve to None
        so the unit-dependent rules skip it instead of guessing.
        """
        for column in ("BORE_OIL_VOL", "BORE_GAS_VOL", "BORE_WAT_VOL", "Oil", "Gas"):
            with self.subTest(column=column):
                self.assertIsNone(_volume_unit(column))
                self.assertIsNotNone(_rate_kind(column))

    def test_wat_is_production_and_wi_is_injection(self):
        """Volve: WAT is produced water, WI is water injection.

        Conflating them downgraded four real negative-volume records to medium
        severity on the strength of a sign convention that does not apply to
        them.
        """
        self.assertFalse(_is_injection("BORE_WAT_VOL"))
        self.assertTrue(_is_injection("BORE_WI_VOL"))
        self.assertEqual(_rate_kind("BORE_WAT_VOL"), _rate_kind("BORE_WI_VOL"))

    def test_substring_does_not_fabricate_a_fluid(self):
        """Token matching keeps unrelated words from being read as fluids."""
        for column in ("SWIFT_VOLUME", "GASLESS_FLAG", "OILY_BAND", "WATERFALL_ID"):
            with self.subTest(column=column):
                self.assertIsNone(_rate_kind(column))


class TestUnitConversion(unittest.TestCase):
    def test_known_factors(self):
        # Input 1 source unit, expected value in the canonical unit.
        cases = [
            ("oil", "bbl", 1.0, 1.0),
            ("oil", "Sm3", 1.0, 6.289810770432105),
            ("water", "Sm3", 1.0, 6.289810770432105),
            ("gas", "MCF", 42.0, 42.0),
            ("gas", "scf", 42_000.0, 42.0),
            ("gas", "Sm3", 1000.0, 35.3146667215),
        ]
        for kind, unit, value, expected in cases:
            with self.subTest(kind=kind, unit=unit):
                got, canon, _note = to_canonical_rate(value, kind, unit)
                self.assertAlmostEqual(got, expected, places=6)
                self.assertEqual(canon, "bbl" if kind != "gas" else "MCF")

    def test_sm3_factor_agrees_with_independent_reference(self):
        """1 Sm3 must convert to 6.28981 stock tank barrels.

        Cross-checked against haql_schema's sm3_to_stb(1590) == 10000.8, an
        independently written implementation, so a typo in this table would
        fail here rather than silently scale every reading.
        """
        got, canon, note = to_canonical_rate(1590.0, "oil", "Sm3")
        self.assertAlmostEqual(got, 10000.8, places=1)
        self.assertEqual(canon, "bbl")
        self.assertTrue(note, "a stated conversion must name its conditions")

    def test_unconvertible_pair_returns_none(self):
        """A missing conversion must skip the check, never default to 1.0."""
        self.assertIsNone(to_canonical_rate(10.0, "oil", "MCF")[0])
        self.assertIsNone(to_canonical_rate(10.0, "gas", "bbl")[0])
        self.assertIsNone(to_canonical_rate(10.0, "nonsense", "bbl")[0])

    def test_declaration_token_normalisation(self):
        self.assertEqual(normalise_unit_token("sm3"), "Sm3")
        self.assertEqual(normalise_unit_token("SM3"), "Sm3")
        self.assertEqual(normalise_unit_token("stb"), "bbl")
        self.assertEqual(normalise_unit_token("Sm3/d"), "Sm3")
        self.assertIsNone(normalise_unit_token("barrels_per_fortnight"))
        self.assertIsNone(normalise_unit_token(""))



class TestSchema(unittest.TestCase):
    def test_issue_roundtrip(self):
        i = Issue(
            rule_id="QC010_NEGATIVE_RATE",
            well_id="A-1",
            date="2024-01-01",
            severity=Severity.HIGH,
            message="negative",
            source_row=2,
            column="oil_bbl_per_d",
            observed=-5.0,
            original_value="-5",
        )
        d = i.to_dict()
        self.assertEqual(d["severity"], "high")
        self.assertEqual(d["correction"], "none")
        self.assertEqual(d["original_value"], "-5")
        json.dumps(d)  # must be serialisable

    def test_report_json_and_jsonl(self):
        rep = AuditReport(
            tool="volve-qc",
            tool_version="0.1.0",
            input_path="/x",
            input_sha256="deadbeef",
            run_started="2024-01-01T00:00:00+00:00",
        )
        rep.add(
            Issue("QC010_NEGATIVE_RATE", "A-1", "2024-01-01", Severity.HIGH, "m", 2)
        )
        self.assertEqual(rep.summary()["issue_count"], 1)
        self.assertEqual(rep.by_rule()["QC010_NEGATIVE_RATE"], 1)
        self.assertEqual(rep.by_severity()["high"], 1)
        json.loads(rep.to_json())  # must parse
        lines = rep.to_jsonl().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["_record"], "header")
        self.assertEqual(json.loads(lines[1])["rule_id"], "QC010_NEGATIVE_RATE")


class TestStructuralRules(unittest.TestCase):
    def test_missing_well_and_date(self):
        rows, cols, rep, issues = run(
            [
                ",2024-01-01,100,10,50,1000,30",
                "A-1,,100,10,50,1000,30",
            ]
        )
        self.assertIn("QC001_MISSING_WELL", ids(issues))
        self.assertIn("QC002_MISSING_DATE", ids(issues))

    def test_duplicate_well_date(self):
        rows, cols, rep, issues = run(
            [
                "A-1,2024-01-01,100,10,50,1000,30",
                "A-1,2024-01-01,120,10,50,1120,30",
            ]
        )
        dups = [i for i in issues if i.rule_id == "QC004_DUPLICATE_WELL_DATE"]
        self.assertEqual(len(dups), 1)
        self.assertFalse(dups[0].auto_correctable)
        self.assertEqual(dups[0].context["first_line"], 2)

    def test_text_column_is_not_unparseable(self):
        """A label column is not corrupt data.

        Regression guard: on the real Volve export, QC003 fired 87,331 times
        on FLOW_KIND='production', NPD_FIELD_NAME='VOLVE' and friends. Column
        type is inferred from parse rate, so labels stay silent.
        """
        d = Path(tempfile.mkdtemp())
        p = d / "t.csv"
        p.write_text(
            "well_id,date,flow_kind,field_name,oil_bbl_per_d,days_on\n"
            + "".join(
                f"A-1,2024-01-{day:02d},production,VOLVE,100,31\n"
                for day in range(1, 8)
            ),
            encoding="utf-8",
        )
        rows, cols, rep = load_production_csv(p)
        issues = run_all(rows, cols)
        self.assertEqual(ids(issues, "QC003_UNPARSEABLE_NUMBER"), [])
        self.assertEqual(rep.config["text_columns"], ["field_name", "flow_kind"])
        self.assertEqual(
            rep.config["numeric_columns"], ["days_on", "oil_bbl_per_d"]
        )

    def test_text_in_numeric_column_is_still_flagged(self):
        """The opposite case: a real number column with a bad cell must fire."""
        d = Path(tempfile.mkdtemp())
        p = d / "t.csv"
        p.write_text(
            "well_id,date,oil_bbl_per_d,days_on\n"
            + "".join(
                f"A-1,2024-01-{day:02d},{'twelve' if day == 4 else 100},31\n"
                for day in range(1, 8)
            ),
            encoding="utf-8",
        )
        rows, cols, rep = load_production_csv(p)
        issues = run_all(rows, cols)
        bad = [i for i in issues if i.rule_id == "QC003_UNPARSEABLE_NUMBER"]
        self.assertEqual(len(bad), 1, "only the one corrupt cell is a finding")
        self.assertEqual(bad[0].column, "oil_bbl_per_d")
        self.assertEqual(bad[0].observed, "twelve")

    def test_declared_null_is_not_corruption(self):
        """NULL / N/A / - are values, not defects.

        Regression guard: the real Volve monthly export writes the literal
        string NULL for absent injection figures. QC003 fired 11 times on a
        column that was otherwise perfectly numeric.
        """
        d = Path(tempfile.mkdtemp())
        p = d / "t.csv"
        p.write_text(
            "well_id,date,oil_sm3,days_on\n"
            + "".join(
                f"A-1,2024-01-{day:02d},{'NULL' if day in (3, 5) else 1000},30\n"
                for day in range(1, 9)
            ),
            encoding="utf-8",
        )
        rows, cols, rep = load_production_csv(p)
        issues = run_all(rows, cols)
        self.assertEqual(ids(issues, "QC003_UNPARSEABLE_NUMBER"), [])
        # The null cells are still absent, not zero.
        for r in rows:
            if r.date in ("2024-01-03", "2024-01-05"):
                self.assertIsNone(r.num("oil_sm3"))
            else:
                self.assertEqual(r.num("oil_sm3"), 1000.0)
        # A column that is 75% numbers and 25% declared nulls is still
        # numeric. Nulls must not count against the parse rate.
        self.assertIn("oil_sm3", rep.config["numeric_columns"])

    def test_non_numeric_column_without_fluid_name_is_profiled(self):
        """ON_STREAM_HRS has no fluid in its name but is genuinely numeric.

        A name-based gate would skip it and hide real corruption inside it.
        """
        d = Path(tempfile.mkdtemp())
        p = d / "t.csv"
        p.write_text(
            "well_id,date,on_stream_hrs,flow_kind\n"
            + "".join(
                f"A-1,2024-01-{day:02d},24,production\n" for day in range(1, 8)
            )
            + "A-1,2024-01-08,oops-and-vines,production\n",
            encoding="utf-8",
        )
        rows, cols, rep = load_production_csv(p)
        issues = run_all(rows, cols)
        bad = [i for i in issues if i.rule_id == "QC003_UNPARSEABLE_NUMBER"]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].column, "on_stream_hrs")
        self.assertIn("on_stream_hrs", rep.config["numeric_columns"])
        self.assertIn("flow_kind", rep.config["text_columns"])

    def test_unparseable_number(self):
        rows, cols, rep, issues = run(
            ["A-1,2024-01-01,not-a-number,10,50,1000,30"]
        )
        bad = [i for i in issues if i.rule_id == "QC003_UNPARSEABLE_NUMBER"]
        self.assertTrue(bad)
        self.assertTrue(bad[0].auto_correctable)
        self.assertEqual(bad[0].original_value, "not-a-number")


class TestPhysicalRules(unittest.TestCase):
    def test_negative_rate(self):
        rows, cols, rep, issues = run(
            ["A-1,2024-01-01,-5,10,50,1000,30"]
        )
        neg = [i for i in issues if i.rule_id == "QC010_NEGATIVE_RATE"]
        self.assertEqual(len(neg), 1)
        self.assertEqual(neg[0].column, "oil_bbl_per_d")
        self.assertEqual(neg[0].expected, ">= 0")

    def test_meter_rollover(self):
        rows, cols, rep, issues = run(
            [
                "A-1,2024-01-01,100,10,50,1000,30",
                "A-1,2024-01-02,100,10,50,900,30",  # cumulative dropped
            ]
        )
        roll = [i for i in issues if i.rule_id == "QC011_METER_ROLLOVER"]
        self.assertTrue(roll)
        self.assertEqual(roll[0].context["previous_value"], 1000)

    def test_row_limit_reports_issue_not_crash(self):
        """The guard rail must not raise NameError.

        Regression guard: loader.py referenced Severity without importing it,
        so a file exceeding MAX_ROWS crashed with NameError instead of
        recording a QC000 finding. Same for the oversize-cell path.
        """
        import volve_qc.loader as loader_mod

        original = loader_mod.MAX_ROWS
        loader_mod.MAX_ROWS = 2
        try:
            lines = [f"A-1,2024-01-{day:02d},100,10,50,1000,31" for day in range(1, 9)]
            rows, cols, rep = load_production_csv(write_csv(lines))
        finally:
            loader_mod.MAX_ROWS = original

        limit = [i for i in rep.issues if i.rule_id == "QC000_ROW_LIMIT"]
        self.assertEqual(len(limit), 1, "must record the limit, not crash")
        self.assertEqual(limit[0].severity, Severity.CRITICAL)
        self.assertEqual(len(rows), 2, "file is truncated at the limit")

    def test_oversize_cell_reports_issue_not_crash(self):
        """Same guard, cell-size branch."""
        import volve_qc.loader as loader_mod

        original = loader_mod.MAX_CELL_BYTES
        loader_mod.MAX_CELL_BYTES = 16
        try:
            d = Path(tempfile.mkdtemp())
            p = d / "big.csv"
            # 16 bytes leaves room for the ISO date and well id, so only the
            # note column trips the guard.
            p.write_text(
                "well_id,date,note\n" "A-1,2024-01-01," + "x" * 64 + "\n",
                encoding="utf-8",
            )
            rows, cols, rep = load_production_csv(p)
        finally:
            loader_mod.MAX_CELL_BYTES = original

        over = [i for i in rep.issues if i.rule_id == "QC000_CELL_OVERSIZE"]
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0].column, "note")
        self.assertEqual(over[0].severity, Severity.HIGH)

    def test_datetime_object_normalises(self):
        """Spreadsheet exports hand back typed cells, not strings."""
        from datetime import datetime

        from volve_qc.loader import normalise_date

        self.assertEqual(normalise_date(datetime(2014, 4, 7, 0, 0)), "2014-04-07")
        self.assertEqual(normalise_date("2014-04-07 00:00:00"), "2014-04-07")
        self.assertIsNone(normalise_date(None))
        self.assertIsNone(normalise_date(""))

    def test_date_gap_daily(self):
        """Daily series with 3 missing days is flagged.

        01-04 -> 01-08 spans 4 days, of which 01-05..07 are absent. The rule
        must report 3 missing days, not 4 (the interval) and not 2.
        """
        rows, cols, rep, issues = run(
            [
                "A-1,2024-01-01,100,10,50,1000,31",
                "A-1,2024-01-02,100,10,50,1100,31",
                "A-1,2024-01-03,100,10,50,1200,31",
                "A-1,2024-01-04,100,10,50,1300,31",
                "A-1,2024-01-08,100,10,50,1800,31",  # 01-05..07 missing
            ]
        )
        gaps = [i for i in issues if i.rule_id == "QC012_DATE_GAP"]
        self.assertTrue(gaps)
        self.assertEqual(gaps[0].context["expected_interval_days"], 1)
        self.assertEqual(gaps[0].context["missing_days"], 3)
        self.assertEqual(gaps[0].context["observed_interval_days"], 4)

    def test_gap_counts_interior_days_only(self):
        """A one-day hiccup is not the same as a 4-day hole.

        At daily cadence, consecutive records are delta=1 with 0 interior days.
        Counting the interval instead of the interior days over-reports every
        gap by one and inflates severity.
        """
        rows, cols, rep, issues = run(
            [
                "A-1,2024-01-01,100,10,50,1000,31",
                "A-1,2024-01-02,100,10,50,1100,31",
                "A-1,2024-01-03,100,10,50,1200,31",
                "A-1,2024-01-04,100,10,50,1300,31",
                "A-1,2024-01-05,100,10,50,1400,31",
                "A-1,2024-01-09,100,10,50,1800,31",  # 01-06..08 missing
            ]
        )
        gaps = [i for i in issues if i.rule_id == "QC012_DATE_GAP"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].context["observed_interval_days"], 4)
        self.assertEqual(gaps[0].context["missing_days"], 3)
        self.assertEqual(gaps[0].context["gap_start"], "2024-01-06")
        self.assertEqual(gaps[0].context["gap_end"], "2024-01-08")

    def test_no_gap_flagged_when_only_one_day_missing(self):
        """Tolerance must absorb a single missing day at daily cadence.

        Real exports lose the odd day routinely; flagging each one buries the
        multi-week holes an operator actually needs to see.
        """
        rows, cols, rep, issues = run(
            [
                "A-1,2024-01-01,100,10,50,1000,31",
                "A-1,2024-01-02,100,10,50,1100,31",
                "A-1,2024-01-03,100,10,50,1200,31",
                "A-1,2024-01-05,100,10,50,1400,31",  # 01-04 missing
                "A-1,2024-01-06,100,10,50,1500,31",
            ]
        )
        gaps = [i for i in issues if i.rule_id == "QC012_DATE_GAP"]
        self.assertEqual(gaps, [], "one missing day is within tolerance")

    def test_date_gap_monthly_is_not_flagged(self):
        """Monthly data must not trigger a daily-coverage gap rule.

        Regression guard: the original rule assumed daily granularity and
        produced 414 false positives on the monthly haql-data-agent dataset.
        """
        rows, cols, rep, issues = run(
            [
                "A-1,2023-01-01,100,10,50,1000,30",
                "A-1,2023-02-01,100,10,50,2000,30",
                "A-1,2023-03-01,100,10,50,3000,30",
                "A-1,2023-04-01,100,10,50,4000,30",
                "A-1,2023-05-01,100,10,50,5000,30",
            ]
        )
        gaps = [i for i in issues if i.rule_id == "QC012_DATE_GAP"]
        self.assertEqual(gaps, [], "monthly cadence must not be reported as daily gaps")

    def test_date_gap_monthly_detects_skipped_month(self):
        """A genuinely skipped month is still caught at monthly cadence."""
        rows, cols, rep, issues = run(
            [
                "A-1,2023-01-01,100,10,50,1000,30",
                "A-1,2023-02-01,100,10,50,2000,30",
                "A-1,2023-05-01,100,10,50,5000,30",  # March and April skipped
                "A-1,2023-06-01,100,10,50,6000,30",
                "A-1,2023-07-01,100,10,50,7000,30",
            ]
        )
        gaps = [i for i in issues if i.rule_id == "QC012_DATE_GAP"]
        self.assertTrue(gaps)
        self.assertEqual(gaps[0].context["expected_interval_days"], 31)
        self.assertGreater(gaps[0].context["missing_days"], 45)

    def test_date_gap_ignores_irregular_series(self):
        """Without a dominant cadence, gaps are not meaningful."""
        rows, cols, rep, issues = run(
            [
                "A-1,2024-01-01,100,10,50,1000,30",
                "A-1,2024-01-04,100,10,50,1100,30",
                "A-1,2024-01-20,100,10,50,1200,30",
                "A-1,2024-03-05,100,10,50,1300,30",
            ]
        )
        gaps = [i for i in issues if i.rule_id == "QC012_DATE_GAP"]
        self.assertEqual(gaps, [], "irregular reporting cadence is not a gap")

    def test_days_on_range(self):
        rows, cols, rep, issues = run(
            ["A-1,2024-01-01,100,10,50,1000,45"]
        )
        self.assertIn("QC013_DAYS_ON_RANGE", ids(issues))

    def test_shutin_production(self):
        rows, cols, rep, issues = run(
            ["A-1,2024-01-01,100,10,50,1000,0"]  # days_on=0 but producing
        )
        shut = [i for i in issues if i.rule_id == "QC014_SHUTIN_PRODUCTION"]
        self.assertEqual(len(shut), 3)  # oil, water, gas

    def test_implausible_rate(self):
        rows, cols, rep, issues = run(
            ["A-1,2024-01-01,99999999,10,50,1000,30"]
        )
        bad = [i for i in issues if i.rule_id == "QC015_IMPLAUSIBLE_RATE"]
        self.assertTrue(bad)

    def test_cumulate_jump(self):
        rows, cols, rep, issues = run(
            [
                "A-1,2024-01-01,100,10,50,1000,30",
                "A-1,2024-01-02,100,10,50,900000,30",  # implied rate huge
            ]
        )
        jumps = [i for i in issues if i.rule_id == "QC016_CUMULATE_JUMP"]
        self.assertTrue(jumps)
        self.assertEqual(jumps[0].context["days"], 1)


class TestRawPreservation(unittest.TestCase):
    def test_raw_values_untouched(self):
        rows, cols, rep, issues = run(
            ["A-1,2024-01-01,-5,10,50,1000,30"]
        )
        self.assertEqual(rows[0].raw["oil_bbl_per_d"], "-5")
        self.assertEqual(rows[0].values["oil_bbl_per_d"], -5.0)
        self.assertEqual(rows[0].clean["oil_bbl_per_d"], "-5")


class TestVersionConsistency(unittest.TestCase):
    """Audit records quote these numbers, so they must not drift apart."""

    def test_version_consistency(self):
        import volve_qc
        from volve_qc import _version

        self.assertEqual(volve_qc.__version__, _version.__version__)
        self.assertEqual(volve_qc.TOOL_VERSION, _version.__version__)
        self.assertEqual(volve_qc.RULESET_VERSION, _version.RULESET_VERSION)
        self.assertEqual(volve_qc.TOOL, _version.TOOL)

    def test_report_defaults_to_current_ruleset(self):
        """A hand-built report must not silently claim an old ruleset."""
        import volve_qc

        rep = AuditReport(
            tool="volve-qc",
            tool_version="0.0.0",
            input_path="x",
            input_sha256="y",
            run_started="z",
        )
        self.assertEqual(rep.ruleset_version, volve_qc.RULESET_VERSION)

    def test_loaded_report_records_both_versions(self):
        import volve_qc

        rows, cols, rep = load_production_csv(
            write_csv(["A-1,2024-01-01,100,10,50,1000,1"])
        )
        payload = json.loads(rep.to_json())
        self.assertEqual(payload["tool_version"], volve_qc.__version__)
        self.assertEqual(payload["ruleset_version"], volve_qc.RULESET_VERSION)


class TestCliContract(unittest.TestCase):
    """The CLI is the documented interface, so its exits are pinned."""

    CSV = (
        "WELL_BORE_CODE,DATEPRD,ON_STREAM_HRS,BORE_OIL_VOL\n"
        "NO 1/2-3,2024-01-01,24,40000\n"
        "NO 1/2-3,2024-01-02,24,40000\n"
        "NO 1/2-3,2024-01-03,24,40000\n"
    )

    def _csv(self) -> Path:
        d = Path(tempfile.mkdtemp())
        p = d / "v.csv"
        p.write_text(self.CSV, encoding="utf-8")
        return p

    def _run(self, *args) -> tuple[int, str]:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = cli_main(list(args))
        return code, buf.getvalue()

    def test_list_rules_needs_no_input(self):
        code, out = self._run("--list-rules")
        self.assertEqual(code, 0)
        self.assertIn("QC015_IMPLAUSIBLE_RATE", out)
        self.assertEqual(len(out.strip().splitlines()), len(RULES))

    def test_missing_input_exits_two(self):
        code, out = self._run()
        self.assertEqual(code, 2)
        self.assertIn("--input is required", out)

    def test_malformed_unit_declaration_exits_two(self):
        code, out = self._run(
            "-i", str(self._csv()),
            "--well-column", "WELL_BORE_CODE",
            "--date-column", "DATEPRD",
            "--declare-unit", "BORE_OIL_VOL",
        )
        self.assertEqual(code, 2)
        self.assertIn("expected COLUMN=UNIT", out)

    def test_unknown_unit_is_rejected_with_the_supported_set(self):
        code, out = self._run(
            "-i", str(self._csv()),
            "--well-column", "WELL_BORE_CODE",
            "--date-column", "DATEPRD",
            "--declare-unit", "BORE_OIL_VOL=barrels_per_fortnight",
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown unit", out)
        self.assertIn("Sm3", out)

    def test_unknown_rule_exits_two(self):
        code, out = self._run(
            "-i", str(self._csv()),
            "--well-column", "WELL_BORE_CODE",
            "--date-column", "DATEPRD",
            "--rule", "QC015",
        )
        self.assertEqual(code, 2)
        self.assertIn("unknown rule", out)

    def test_unit_declaration_is_recorded_in_the_report(self):
        """A reviewer must be able to see which units the run assumed."""
        out_path = self._csv().parent / "audit.json"
        code, _ = self._run(
            "-i", str(self._csv()),
            "--well-column", "WELL_BORE_CODE",
            "--date-column", "DATEPRD",
            "--declare-unit", "BORE_OIL_VOL=sm3",
            "-r", str(out_path),
        )
        self.assertEqual(code, 0)
        config = json.loads(out_path.read_text(encoding="utf-8"))["config"]
        self.assertEqual(config["unit_declarations"], {"BORE_OIL_VOL": "Sm3"})

    def test_fail_on_threshold_controls_exit_code(self):
        """--fail-on is the CI gate, so its threshold must be exact."""
        base = [
            "-i", str(self._csv()),
            "--well-column", "WELL_BORE_CODE",
            "--date-column", "DATEPRD",
            "--declare-unit", "BORE_OIL_VOL=Sm3",
            "-r", str(self._csv().parent / "audit.json"),
        ]
        self.assertEqual(self._run(*base)[0], 0)
        self.assertEqual(self._run(*base, "--fail-on", "never")[0], 0)
        self.assertEqual(self._run(*base, "--fail-on", "high")[0], 0)
        self.assertEqual(self._run(*base, "--fail-on", "medium")[0], 1)


class TestRuleRegistry(unittest.TestCase):
    def test_rule_ids_unique_and_stable(self):
        for rid in RULES:
            self.assertRegex(rid, r"^QC\d{3}_[A-Z_]+$")

    def test_run_subset(self):
        rows, cols, rep, issues = run(["A-1,2024-01-01,-5,10,50,1000,30"])
        subset = run_all(rows, cols, rule_ids=["QC010_NEGATIVE_RATE"])
        self.assertEqual(ids(subset), ["QC010_NEGATIVE_RATE"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
