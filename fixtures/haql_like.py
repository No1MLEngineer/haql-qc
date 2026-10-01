"""Volve-shaped fixture with deliberate defects.

Column names follow the Volve production schema (well, date, oil, water, gas,
cumulative totals, days_on) so the ruleset is exercised against the real
layout before any real data is touched. Every defect below is intentional and
listed in EXPECTED_FINDINGS.

Defects:
  A-1  daily, clean except a rollover in oil_cum on 01-05
  A-2  daily, 4-day gap 01-06..01-09
  B-1  monthly, no gaps (guards against the daily-coverage false positive)
  C-1  days_on = 0 while oil is non-zero
  C-2  negative gas rate
  C-3  genuinely corrupt water value (not a declared null)
  C-4  oil rate above plausibility ceiling
  D-1  duplicate well/date
  E-1  declared NULLs in a numeric column; must produce zero findings
"""

HEADER = (
    "well,date,oil_bbl_per_d,water_bbl_per_d,gas_mcf_per_d,"
    "oil_cum_bbl,water_cum_bbl,gas_cum_mcf,days_on"
)

ROWS = [
    # A-1: clean daily, cumulative oil drops on 01-05 (QC011)
    "A-1,2024-01-01,100.0,10.0,50.0,1000.0,200.0,900.0,1",
    "A-1,2024-01-02,101.0,11.0,51.0,1101.0,211.0,951.0,1",
    "A-1,2024-01-03,102.0,12.0,52.0,1203.0,223.0,1003.0,1",
    "A-1,2024-01-04,103.0,13.0,53.0,1306.0,236.0,1056.0,1",
    "A-1,2024-01-05,104.0,14.0,54.0,900.0,250.0,1110.0,1",
    "A-1,2024-01-06,105.0,15.0,55.0,1005.0,265.0,1165.0,1",
    "A-1,2024-01-07,106.0,16.0,56.0,1111.0,281.0,1221.0,1",
    # A-1 resumes 01-10: gap of 3 days (01-08, 01-09, 01-10) (QC012)
    "A-1,2024-01-11,107.0,17.0,57.0,1218.0,298.0,1278.0,1",
    "A-1,2024-01-12,108.0,18.0,58.0,1326.0,316.0,1336.0,1",
    "A-1,2024-01-13,109.0,19.0,59.0,1435.0,335.0,1395.0,1",
    # B-1: monthly cadence, must NOT trigger QC012 (QC012 regression guard)
    "B-1,2024-01-01,500.0,20.0,900.0,500.0,20.0,900.0,31",
    "B-1,2024-02-01,505.0,21.0,910.0,1005.0,41.0,1810.0,29",
    "B-1,2024-03-01,498.0,22.0,905.0,1503.0,63.0,2715.0,31",
    "B-1,2024-04-01,492.0,23.0,900.0,1995.0,86.0,3615.0,30",
    "B-1,2024-05-01,488.0,24.0,895.0,2483.0,110.0,4510.0,31",
    # C-1: shut-in day reporting production (QC014)
    "C-1,2024-01-01,0.0,0.0,0.0,0.0,0.0,0.0,0",
    "C-1,2024-01-02,150.0,5.0,200.0,150.0,5.0,200.0,0",
    "C-1,2024-01-03,151.0,6.0,201.0,301.0,11.0,401.0,1",
    # C-2: negative gas (QC010)
    "C-2,2024-01-01,80.0,4.0,-15.0,80.0,4.0,-15.0,1",
    "C-2,2024-01-02,81.0,5.0,60.0,161.0,9.0,45.0,1",
    "C-2,2024-01-03,82.0,6.0,61.0,243.0,15.0,106.0,1",
    # C-3: genuinely corrupt water value (QC003).
    # Null markers (n/a, NULL, unknown, -) are values, not defects, so this
    # uses a word that is plainly not a number and not a null.
    "C-3,2024-01-01,90.0,elevenish,70.0,90.0,0.0,70.0,1",
    "C-3,2024-01-02,91.0,3.0,71.0,181.0,3.0,141.0,1",
    "C-3,2024-01-03,92.0,4.0,72.0,273.0,7.0,213.0,1",
    # C-4: oil above plausibility ceiling (QC015)
    "C-4,2024-01-01,999999.0,3.0,70.0,999999.0,3.0,70.0,1",
    "C-4,2024-01-02,100.0,4.0,71.0,1000099.0,7.0,141.0,1",
    "C-4,2024-01-03,101.0,5.0,72.0,1000200.0,12.0,213.0,1",
    # D-1: duplicate well/date (QC004)
    "D-1,2024-01-01,70.0,2.0,50.0,70.0,2.0,50.0,1",
    "D-1,2024-01-01,999.0,2.0,50.0,1069.0,4.0,100.0,1",
    "D-1,2024-01-02,71.0,3.0,51.0,1140.0,7.0,151.0,1",
    "D-1,2024-01-03,72.0,4.0,52.0,1212.0,11.0,203.0,1",
    # E-1: declared nulls in an otherwise numeric column must stay silent.
    # Mirrors the real Volve monthly export, which writes literal NULL.
    "E-1,2024-01-01,10.0,NULL,20.0,NULL,20.0,10.0,NULL,1",
    "E-1,2024-01-02,11.0,NULL,21.0,NULL,21.0,11.0,NULL,1",
    "E-1,2024-01-03,12.0,NULL,22.0,NULL,22.0,12.0,NULL,1",
]

EXPECTED_FINDINGS = {
    "QC003_UNPARSEABLE_NUMBER": 1,
    "QC004_DUPLICATE_WELL_DATE": 1,
    "QC010_NEGATIVE_RATE": 1,
    "QC011_METER_ROLLOVER": 1,
    "QC012_DATE_GAP": 1,
    "QC014_SHUTIN_PRODUCTION": 3,
    "QC015_IMPLAUSIBLE_RATE": 1,
}
