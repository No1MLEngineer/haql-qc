"""Monthly / period-granularity ingestion.

The monthly export is where the tool is most likely to fail quietly, because
the file declares its own units in a second header line and carries its period
in two columns instead of a date. A loader that misses either one still runs,
still returns rows, and still reports zero findings -- which is exactly the
failure mode these tests exist to prevent.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from haql_qc.period import PeriodError, detect_units_row, load_period_csv, month_end_date
from haql_qc.rules import run_all

MONTHLY_HEADER = "Wellbore name,NPDCode,Year,Month,On Stream,Oil,Gas,Water,GI,WI"
MONTHLY_UNITS = ",,,,hrs,Sm3,Sm3,Sm3,Sm3,Sm3"
MONTHLY_ROWS = [
    "15/9-F-1 C,7405,2014,4,227.5,11142.47,1597936.65,0,NULL,NULL",
    "15/9-F-1 C,7405,2014,5,733.83334,24901.95,3496229.65,783.48,NULL,NULL",
    "15/9-F-1 C,7405,2014,6,720.0,21000.0,3000000.0,500.0,NULL,NULL",
]


def write_monthly(lines: list[str], units_row: str | None = MONTHLY_UNITS) -> Path:
    d = Path(tempfile.mkdtemp())
    p = d / "monthly.csv"
    body = [MONTHLY_HEADER]
    if units_row is not None:
        body.append(units_row)
    body.extend(lines)
    p.write_text("\n".join(body) + "\n", encoding="utf-8")
    return p


def load(lines: list[str], units_row: str | None = MONTHLY_UNITS):
    return load_period_csv(
        write_monthly(lines, units_row), "Wellbore name", "Year", "Month"
    )


class TestUnitsRow(unittest.TestCase):
    def test_units_are_read_from_the_second_line(self):
        _, _, units = load(MONTHLY_ROWS)
        self.assertEqual(units["Oil"], "Sm3")
        self.assertEqual(units["Gas"], "Sm3")
        self.assertEqual(units["Water"], "Sm3")
        self.assertEqual(units["On Stream"], "hrs")

    def test_units_row_is_not_loaded_as_data(self):
        rows, _, _ = load(MONTHLY_ROWS)
        self.assertEqual(len(rows), 3, "the units row must be skipped, not audited")

    def test_no_units_row_means_no_units(self):
        _, _, units = load(MONTHLY_ROWS, units_row=None)
        self.assertEqual(units, {}, "absence of a units row must not be filled in")

    def test_a_data_row_is_never_mistaken_for_a_units_row(self):
        line, _ = detect_units_row(write_monthly(MONTHLY_ROWS, None), [])
        self.assertEqual(line, 0)

    def test_nulls_in_the_units_row_are_not_units(self):
        _, _, units = load(MONTHLY_ROWS, units_row=",,,,hrs,Sm3,Sm3,Sm3,,NULL")
        self.assertNotIn("GI", units)
        self.assertNotIn("WI", units)

    def test_prose_under_the_header_is_not_a_units_row(self):
        prose = "this file was exported by hand, do not trust,,,,,,,,,"
        _, _, units = load(MONTHLY_ROWS, units_row=prose)
        self.assertEqual(units, {}, "a long note is not a unit declaration")


class TestPeriodDates(unittest.TestCase):
    def test_month_end_dates(self):
        self.assertEqual(month_end_date(2014, 4), "2014-04-30")
        self.assertEqual(month_end_date(2014, 12), "2014-12-31")
        self.assertEqual(month_end_date(2014, 1), "2014-01-31")

    def test_leap_february(self):
        self.assertEqual(month_end_date(2024, 2), "2024-02-29")
        self.assertEqual(month_end_date(2023, 2), "2023-02-28")

    def test_row_dates_land_inside_the_period(self):
        rows, _, _ = load(MONTHLY_ROWS)
        self.assertEqual([r.date for r in rows], ["2014-04-30", "2014-05-31", "2014-06-30"])

    def test_bad_year_or_month_yields_no_date(self):
        self.assertIsNone(month_end_date("", "4"))
        self.assertIsNone(month_end_date(2014, "13"))
        self.assertIsNone(month_end_date(2014, "0"))
        self.assertIsNone(month_end_date("notayear", 4))


class TestMonthlyRules(unittest.TestCase):
    """The monthly result is quoted in the README. It must be earned."""

    def _load_and_run(self):
        rows, cols, units = load(MONTHLY_ROWS)
        return rows, cols, units, run_all(rows, cols, unit_declarations=units)

    def test_clean_monthly_data_yields_no_findings(self):
        _, _, _, issues = self._load_and_run()
        self.assertEqual(issues, [], "clean monthly rows must not manufacture findings")

    def test_negative_volume_is_caught_on_monthly_data(self):
        rows, cols, units = load(MONTHLY_ROWS)
        rows[0].values["Water"] = -5.0
        issues = run_all(rows, cols, unit_declarations=units)
        neg = [i for i in issues if i.rule_id == "QC010_NEGATIVE_RATE"]
        self.assertEqual(len(neg), 1, "a negative monthly volume must be reported")

    def test_zero_findings_is_not_a_silent_skip(self):
        """Zero findings only means something if the rules demonstrably run.

        This is the check that would have caught a loader reading the wrong
        columns: inject a fault and confirm it surfaces.
        """
        rows, cols, units = load(MONTHLY_ROWS)
        rows[1].values["Water"] = -1.0
        issues = run_all(rows, cols, unit_declarations=units)
        self.assertTrue(
            [i for i in issues if i.rule_id == "QC010_NEGATIVE_RATE"],
            "rules must be live on monthly data, not silently skipping",
        )

    def test_shut_in_contradiction_is_caught_on_monthly_data(self):
        rows, cols, units = load(MONTHLY_ROWS)
        rows[2].values["On Stream"] = 0.0
        issues = run_all(rows, cols, unit_declarations=units)
        shut = [i for i in issues if i.rule_id == "QC014_SHUTIN_PRODUCTION"]
        self.assertTrue(shut, "zero on-stream hours with a monthly volume must be flagged")

    def test_injection_contradiction_says_injecting(self):
        """An injection column must not be described as production."""
        rows, cols, units = load(MONTHLY_ROWS)
        rows[2].values["On Stream"] = 0.0
        rows[2].values["WI"] = 5000.0
        rows[0].values["WI"] = 4000.0
        rows[1].values["WI"] = 4200.0
        issues = run_all(rows, cols, unit_declarations=units)
        wi = [i for i in issues if i.column == "WI"]
        self.assertTrue(wi, "an injection contradiction must still be reported")
        for i in wi:
            self.assertIn("injecting", i.message)
            self.assertNotIn("producing", i.message)

    def test_production_contradiction_says_producing(self):
        rows, cols, units = load(MONTHLY_ROWS)
        rows[2].values["On Stream"] = 0.0
        rows[2].values["Oil"] = 50000.0
        issues = run_all(rows, cols, unit_declarations=units)
        oil = [i for i in issues if i.rule_id == "QC014_SHUTIN_PRODUCTION" and i.column == "Oil"]
        self.assertTrue(oil)
        for i in oil:
            self.assertIn("producing", i.message)

    def test_uptime_column_is_not_itself_a_production_rate(self):
        rows, cols, units = load(MONTHLY_ROWS)
        rows[2].values["On Stream"] = -48.0
        issues = run_all(rows, cols, unit_declarations=units)
        # A negative uptime is nonsense, but it is not a *production* volume, so
        # QC010 must not claim it caught a negative rate.
        neg = [i for i in issues if i.rule_id == "QC010_NEGATIVE_RATE"]
        self.assertEqual(neg, [], "uptime hours must not be audited as a fluid rate")

    def test_well_and_period_columns_are_not_measurements(self):
        rows, cols, units = load(MONTHLY_ROWS)
        for r in rows:
            self.assertNotIn("Year", r.values)
            self.assertNotIn("Month", r.values)
            self.assertNotIn("Wellbore name", r.values)

    def test_source_lines_survive_the_skipped_units_row(self):
        """A finding must point at the real line in the file the user has."""
        rows, _, _ = load(MONTHLY_ROWS)
        self.assertEqual([r.line for r in rows], [3, 4, 5])


class TestRealMonthlyExport(unittest.TestCase):
    """The 526-row / 0-finding claim in the README, against the real file."""

    REAL = Path("/tmp/opencode/volve/volve_monthly.csv")

    def setUp(self):
        if not self.REAL.exists():
            self.skipTest("Volve monthly export not present")

    def test_real_export_matches_the_published_claim(self):
        rows, cols, units = load_period_csv(
            self.REAL, "Wellbore name", "Year", "Month"
        )
        self.assertEqual(len(rows), 526)
        self.assertEqual(len({r.well_id for r in rows}), 7)
        self.assertEqual(units["Oil"], "Sm3")

        issues = run_all(rows, cols, unit_declarations=units)
        self.assertEqual(issues, [], "real monthly export is clean")

    def test_real_export_has_no_negative_or_contradictory_rows(self):
        rows, cols, _ = load_period_csv(self.REAL, "Wellbore name", "Year", "Month")
        neg = [
            (r.line, c)
            for r in rows
            for c in ("Oil", "Gas", "Water", "GI", "WI")
            if r.values.get(c) is not None and r.values[c] < 0
        ]
        self.assertEqual(neg, [], "the real monthly file has no negative volumes")

        contradictions = [
            r.line
            for r in rows
            if r.values.get("On Stream") == 0
            and any(r.values.get(c) for c in ("Oil", "Gas", "Water", "GI", "WI"))
        ]
        self.assertEqual(contradictions, [], "no zero-uptime rows carry volume")


class TestPeriodErrors(unittest.TestCase):
    def test_missing_column_raises(self):
        with self.assertRaises(PeriodError):
            load_period_csv(write_monthly(MONTHLY_ROWS), "Wellbore name", "Year", "Nope")

    def test_missing_file_raises(self):
        with self.assertRaises(PeriodError):
            load_period_csv(Path("/nonexistent.csv"), "w", "Year", "Month")


if __name__ == "__main__":
    unittest.main(verbosity=2)