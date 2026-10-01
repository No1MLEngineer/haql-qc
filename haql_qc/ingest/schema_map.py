"""Deciding what a column *means*, and admitting when the answer is weak.

A production export is not just a table, it is a claim about roles: this column
is the well, this one is the reporting period, that one is a period volume or a
rate, this one is water injection rather than produced water. Every downstream
rule is only as good as this mapping, so the mapping is the part worth being
careful about.

Two rules govern the whole module:

*Nothing is guessed silently.* Every decision carries a confidence and the
evidence behind it. A role the tool inferred at low confidence is not applied
to the data; it is reported as an open question, because a confidently wrong
well column turns every finding in the report into a confidently wrong finding.

*Content settles what names cannot.* Name matching finds candidates; the data
decides between them. A column named ``gross`` means nothing, but a column that
only ever increases until it resets at a well change is a cumulative total no
matter what it is called.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..rules import _column_tokens, _is_cumulative, _is_injection, _rate_kind

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
NONE = "none"

_RANK = {HIGH: 3, MEDIUM: 2, LOW: 1, NONE: 0}

WELL = "well"
DATE = "date"
PERIOD_YEAR = "period_year"
PERIOD_MONTH = "period_month"
OIL = "oil"
WATER = "water"
GAS = "gas"
UPTIME = "uptime"

ROLE_KINDS = (OIL, WATER, GAS)
"""Fluid roles. All three are real business roles in production accounting."""


@dataclass
class RoleAssignment:
    """One column's proposed role, with the evidence attached."""

    role: str
    column: str
    confidence: str
    basis: str
    alternatives: list[str] = field(default_factory=list)
    verified: bool = False
    """True only when the file itself declared the role.

    A name match or a statistical inference sets this False. The unit
    declaration path makes it True, and only a declaration may mark a column
    verified: verification has to be something a person can be held to.
    """

    def to_dict(self) -> dict:
        d = {
            "role": self.role,
            "column": self.column,
            "confidence": self.confidence,
            "basis": self.basis,
            "verified": self.verified,
        }
        if self.alternatives:
            d["alternatives"] = self.alternatives
        return d


@dataclass
class Cadence:
    """Reporting granularity, and how strongly the file states it."""

    granularity: str  # daily | period | unknown
    confidence: str
    basis: str

    def to_dict(self) -> dict:
        return {
            "granularity": self.granularity,
            "confidence": self.confidence,
            "basis": self.basis,
        }


@dataclass
class SchemaMap:
    """The full mapping decision, ready to be reported and then overridden."""

    roles: dict[str, RoleAssignment] = field(default_factory=dict)
    """Keyed by column name."""
    cadence: Cadence = field(
        default_factory=lambda: Cadence("unknown", NONE, "no date or period column found")
    )
    units: dict[str, str] = field(default_factory=dict)
    """Units the file declared for itself."""
    operator_units: dict[str, str] = field(default_factory=dict)
    """Units an operator asserted explicitly.

    Kept separate from ``units`` so the audit record can always show which
    statements came from the file and which from a person. A reviewer asking
    "who said this column was in Sm3?" needs an answer, and collapsing the two
    would make the question unanswerable.
    """
    unit_sources: dict[str, str] = field(default_factory=dict)
    """column -> where the unit came from: ``file_units_row``, ``in_name``, ``operator``."""
    unresolved: list[str] = field(default_factory=list)
    """Roles that matter and could not be filled with confidence."""
    notes: list[str] = field(default_factory=list)

    def role_of(self, role: str) -> RoleAssignment | None:
        return self.roles.get(role)

    def column_for(self, role: str) -> str | None:
        a = self.roles.get(role)
        return a.column if a else None

    def apply_units_row(self, units: dict[str, str]) -> None:
        """Record units the file declared on its own units row.

        This is the one unit source treated as authoritative. It is also the
        strongest signal available for granularity and for rate-versus-total:
        a column labelled ``bbl/d`` is a rate by declaration, whatever its name.
        """
        self.units = dict(units)
        for col, token in units.items():
            self.unit_sources[col] = "file_units_row"

    @property
    def needs_confirmation(self) -> list[RoleAssignment]:
        """Assignments a person should look at before trusting the report."""
        return [
            a
            for a in self.roles.values()
            if a.verified or _RANK[a.confidence] >= _RANK[HIGH]
        ]

    def to_dict(self) -> dict:
        return {
            "roles": {
                role: a.to_dict()
                for role, a in sorted(self.roles.items())
                if a.confidence != NONE
            },
            "cadence": self.cadence.to_dict(),
            "units_declared_by_file": dict(sorted(self.units.items())),
            "units_declared_by_operator": dict(sorted(self.operator_units.items())),
            "unit_sources": dict(sorted(self.unit_sources.items())),
            "needs_confirmation": [a.to_dict() for a in self.needs_confirmation],
            "unresolved": list(self.unresolved),
            "notes": list(self.notes),
        }


# --------------------------------------------------------------- name signals

_WELL_STRONG = {
    "well", "wellid", "wellbore", "bore", "wellname", "wellnm", "npdwella",
    "npdwellb", "npdwellbore", "npdwellborecode", "npdcode", "npdid",
    "api", "apino", "apiwellno", "uin", "uwi", "sidetrack", "wellno",
}
"""Exact names that mean well identifier in NPD, Enverus and RRC exports alike."""

_WELL_WEAK = {"bore", "borecode", "wellborecode", "id", "identifier", "name"}
"""Names that could be a well, or could equally be a field, facility or county."""

_WELL_BLOCKERS = {
    "type", "status", "class", "count", "field", "facility", "county", "town",
    "operator", "county", "formation", "no", "num", "number", "total",
}
"""Tokens that turn a well-ish name into metadata about a well, not the well."""

_DATE_STRONG = {
    "date", "day", "proddate", "productiondate", "dateprod", "dateprd",
    "prddate", "reportingdate", "period", "daymonth", "monthdate",
}
_DATE_WEAK = {"date", "day", "dt", "period", "time", "stamp", "when"}
_DATE_BLOCKERS = {"end", "start", "due", "expire", "expires", "spud", "first", "last"}

_YEAR_NAMES = {"year", "yr", "yyyy", "yearprod", "prodyear"}
_MONTH_NAMES = {"month", "mo", "mon", "monthprod", "prodmonth", "periodmonth"}

_UPTIME_NAMES = {
    "onstreamhrs", "onstreamhours", "onhrs", "uptime", "uptimehrs", "hours",
    "hrs", "operatinghours", "producinghours", "runtime", "stream",
}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _score_well(name: str) -> tuple[str, str] | None:
    n = _norm(name)
    if not n:
        return None
    if n in _WELL_STRONG:
        return (HIGH, f"exact match to known well-identifier name '{n}'")
    tokens = _column_tokens(name)
    low = {t.lower() for t in tokens}
    if low & _WELL_BLOCKERS:
        return None
    if "wellbore" in low or "wellbore" in n:
        return (MEDIUM, "name contains 'wellbore'")
    if low & {"well", "wellnm", "wellname"}:
        return (MEDIUM, "name contains 'well'")
    if n in _WELL_WEAK:
        return (LOW, f"name '{n}' is ambiguous: could be a well, field or facility")
    return None


def _score_date(name: str) -> tuple[str, str] | None:
    n = _norm(name)
    if not n:
        return None
    if n in _DATE_STRONG:
        return (HIGH, f"exact match to known date name '{n}'")
    low = {t.lower() for t in _column_tokens(name)}
    if low & _DATE_BLOCKERS:
        return None
    if low & _DATE_WEAK:
        return (MEDIUM, "name contains a date token")
    return None


def _score_uptime(name: str) -> tuple[str, str] | None:
    n = _norm(name)
    if not n:
        return None
    if n in _UPTIME_NAMES:
        if "hrs" in n or "hours" in n:
            return (HIGH, f"name declares uptime in hours ('{n}')")
        return (MEDIUM, f"name matches known uptime column '{n}'")
    if "onstream" in n or "uptime" in n:
        return (HIGH, "name contains 'onstream' or 'uptime'")
    return None


# --------------------------------------------------------- content inference


def _is_date_like(values: list[str]) -> float:
    """Share of values that parse as a date under the loader's own rules.

    Reuses :func:`haql_qc.loader.normalise_date` so ingest and loader can never
    disagree about what counts as a date. A disagreement there would show up as
    a clean ingest followed by thousands of QC002 findings.
    """
    from ..loader import is_null, normalise_date

    present = [v for v in values if not is_null(v)]
    if not present:
        return 0.0
    hits = 0
    for v in present:
        s = str(v).strip()
        # A bare year or a month name is not a daily date; reject those here so
        # a Year column cannot win the date role.
        if len(s) < 6 or _norm(s).isdigit() and len(_norm(s)) == 4:
            continue
        if normalise_date(s) is not None:
            hits += 1
    return hits / len(present)


def _month_day_spacing(values: list[str]) -> str | None:
    """Distinguish daily rows from monthly rows by the spacing of the dates.

    Content, not the column name: an export called ``Period`` holding
    2024-01-31, 2024-02-29, 2024-03-31 is monthly, whatever it is called.
    """
    from ..loader import normalise_date

    seen: list[str] = []
    for v in values:
        s = str(v or "").strip()
        if not s:
            continue
        d = normalise_date(s)
        if d:
            seen.append(d)
        if len(seen) >= 40:
            break
    if len(seen) < 3:
        return None

    unique = sorted(set(seen))
    try:
        dates = [__import__("datetime").date.fromisoformat(d) for d in unique]
    except ValueError:
        return None
    if len(dates) < 3:
        return None

    gaps = [
        (b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0
    ]
    if not gaps:
        return None
    gaps.sort()
    median_gap = gaps[len(gaps) // 2]
    if median_gap <= 1:
        return "daily"
    if 25 <= median_gap <= 35:
        return "monthly"
    if 80 <= median_gap <= 100:
        return "quarterly"
    return None


def _monotonicity(values: list[float]) -> tuple[bool, int]:
    """(never decreases, how many distinct reset points were seen).

    A cumulative meter only ever goes up between resets. A period volume moves
    both ways. This is the difference that decides whether ``QC011`` meter
    rollover and ``QC016`` cumulate jump can legitimately apply.
    """
    ups = downs = 0
    resets = 0
    prev: float | None = None
    for v in values:
        if prev is not None:
            if v > prev:
                ups += 1
            elif v < prev:
                downs += 1
                if prev > 0:
                    resets += 1
        prev = v
    return (downs == 0 and ups > 0, resets)


def _repeats_within_period(table: dict[str, list[str]], period_col: str) -> list[str]:
    """Columns whose value is constant inside each period group.

    A well identifier is the same string across every day of a month. A
    pressure reading is not. This finds the identifier by behaviour when its
    name says nothing useful, which is the common case for a column literally
    called ``name`` or ``id2``.
    """
    periods = table.get(period_col) or []
    out = []
    for col, values in table.items():
        if col == period_col or len(values) != len(periods):
            continue
        buckets: dict[str, set[str]] = defaultdict(set)
        counts: dict[str, int] = defaultdict(int)
        for p, v in zip(periods, values):
            if not p:
                continue
            buckets[p].add(v)
            counts[p] += 1
        multi = [b for b, c in counts.items() if c > 1]
        if len(multi) < 3:
            continue
        if not all(len(buckets[b]) == 1 for b in multi):
            continue
        sample = next(iter(buckets[multi[0]]))
        # A numeric column that happens to be constant is not an identifier.
        if sample and not _is_numeric_string(sample):
            out.append(col)
    return out


def _is_numeric_string(s: str) -> bool:
    from ..loader import parse_number

    return parse_number(s) is not None or _norm(s).isdigit()


def _cardinality_ratio(values: list[str]) -> float:
    from ..loader import is_null

    present = [v for v in values if not is_null(v)]
    if not present:
        return 0.0
    return len(set(present)) / len(present)


# ---------------------------------------------------------------- the mapper


def map_schema(
    columns: list[str],
    table: dict[str, list[str]] | None = None,
    *,
    file_units: dict[str, str] | None = None,
    well_column: str | None = None,
    date_column: str | None = None,
    year_column: str | None = None,
    month_column: str | None = None,
) -> SchemaMap:
    """Propose roles for every column, and say how sure we are of each.

    ``table`` maps column name to its cell values and is what lets the mapper
    resolve ties by content. Without it the result is name-only and every
    fluid assignment is capped at medium confidence, because a name alone
    cannot distinguish a rate from a total.
    """
    sm = SchemaMap()
    table = table or {}

    if file_units:
        sm.apply_units_row(file_units)
        sm.notes.append(
            f"file declares units for {len(file_units)} column(s) on its own units row"
        )

    # --- period columns first: they define the time axis everything else
    #     needs, and a Year/Month pair makes the date role unnecessary.
    year_pick = _pick_exact(columns, _YEAR_NAMES, year_column)
    month_pick = _pick_exact(columns, _MONTH_NAMES, month_column)
    if year_pick:
        sm.roles[PERIOD_YEAR] = RoleAssignment(
            PERIOD_YEAR, year_pick, HIGH, "exact match to known year-column name"
        )
    if month_pick:
        sm.roles[PERIOD_MONTH] = RoleAssignment(
            PERIOD_MONTH, month_pick, HIGH, "exact match to known month-column name"
        )

    if year_pick and month_pick:
        sm.cadence = Cadence(
            "period",
            HIGH,
            f"year column '{year_pick}' plus month column '{month_pick}'",
        )
    elif year_pick or month_pick:
        sm.cadence = Cadence(
            "period",
            LOW,
            "only one of a year/month column pair was found; period likely incomplete",
        )
        sm.unresolved.append("cadence")

    # --- well identifier
    if well_column:
        sm.roles[WELL] = RoleAssignment(
            WELL, well_column, HIGH, "column named explicitly by the operator",
            verified=True,
        )
    else:
        well = _resolve(columns, table, _score_well, "well identifier", skip=(
            year_pick, month_pick,
        ))
        if well:
            sm.roles[WELL] = well

    # --- date
    if date_column:
        sm.roles[DATE] = RoleAssignment(
            DATE, date_column, HIGH, "column named explicitly by the operator",
            verified=True,
        )
    elif not (year_pick and month_pick):
        date = _resolve(columns, table, _score_date, "date", skip=(
            year_pick, month_pick, sm.column_for(WELL),
        ))
        if date:
            sm.roles[DATE] = date

    # --- fluid roles
    taken = {
        sm.column_for(WELL),
        sm.column_for(DATE),
        sm.column_for(PERIOD_YEAR),
        sm.column_for(PERIOD_MONTH),
    }
    for kind in ROLE_KINDS:
        assignment = _assign_fluid(kind, columns, table, sm, taken)
        if assignment:
            sm.roles[kind] = assignment
            taken.add(assignment.column)

    # --- uptime
    up = _resolve(columns, table, _score_uptime, "uptime", skip=())
    if up:
        sm.roles[UPTIME] = up

    # --- cadence for a date column, decided by the dates themselves
    if sm.column_for(DATE):
        spacing = _month_day_spacing(table.get(sm.column_for(DATE), []))
        if spacing == "daily":
            sm.cadence = Cadence(
                "daily",
                MEDIUM if table else LOW,
                "successive dates are one day apart",
            )
        elif spacing in ("monthly", "quarterly"):
            sm.cadence = Cadence(
                "period",
                MEDIUM if table else LOW,
                f"successive dates are {spacing} apart",
            )
            sm.notes.append(
                "dates are month- or quarter-end but the file gives no units row; "
                "read with --granularity period if the periods are not daily"
            )

    _annotate_cumulative(sm, table)

    # A date role is only unresolved if nothing else supplies a time axis.
    # A Year/Month pair is a complete period axis on its own.
    if WELL not in sm.roles:
        sm.unresolved.append(WELL)
    has_period = PERIOD_YEAR in sm.roles and PERIOD_MONTH in sm.roles
    if DATE not in sm.roles and not has_period:
        sm.unresolved.append(DATE)
    return sm


def _pick_exact(columns: list[str], names: set[str], explicit: str | None) -> str | None:
    if explicit and explicit in columns:
        return explicit
    for c in columns:
        if _norm(c) in names:
            return c
    return None


def _resolve(
    columns: list[str],
    table: dict[str, list[str]],
    scorer,
    what: str,
    skip: tuple[str | None, ...] = (None,),
) -> RoleAssignment | None:
    """Best candidate for a role, with content used to break ties."""
    candidates: list[tuple[str, str, str]] = []  # (rank, column, basis)
    skipset = {s for s in skip if s}
    for c in columns:
        if c in skipset:
            continue
        scored = scorer(c)
        if scored is None:
            continue
        conf, basis = scored
        candidates.append((_RANK[conf], c, basis))
    if not candidates:
        return None
    candidates.sort(key=lambda t: -t[0])

    top_rank, top_col, top_basis = candidates[0]
    ties = [c for r, c, _ in candidates if r == top_rank]

    if len(ties) > 1 and table:
        # Content decides: the identifier is the one that repeats per period,
        # the date is the one that mostly parses as a date.
        refined = _content_break_ties(ties, table, what)
        if refined:
            return RoleAssignment(
                what, refined, HIGH, f"content: {what} decided from data, not name",
                alternatives=[c for c in ties if c != refined],
            )
        # No content signal; a name-based tie is a coin flip, so say so.
        return RoleAssignment(
            what, top_col, LOW,
            f"{top_basis}; {len(ties)} columns match equally ({', '.join(ties[:4])})",
            alternatives=[c for c in ties if c != top_col],
        )

    if table:
        conf = HIGH if top_rank == _RANK[HIGH] else MEDIUM
        basis = f"{top_basis}; confirmed against column contents"
    else:
        conf = "medium" if top_rank == _RANK[HIGH] else top_rank_name(top_rank)
        basis = f"{top_basis}; name only, contents not inspected"
    return RoleAssignment(what, top_col, conf, basis)


def top_rank_name(rank: int) -> str:
    return {3: HIGH, 2: MEDIUM, 1: LOW}.get(rank, NONE)


def _content_break_ties(
    ties: list[str], table: dict[str, list[str]], what: str
) -> str | None:
    if what == "well identifier":
        period_col = next(
            (c for c in table if _month_day_spacing(table[c]) or _score_date(c)), None
        )
        if period_col and period_col not in ties:
            repeats = _repeats_within_period(table, period_col)
            best = [c for c in ties if c in repeats]
            if len(best) == 1:
                return best[0]
        # No period column to group by: the identifier is the most repetitive
        # string column that is not unique per row.
        scored = []
        for c in ties:
            ratio = _cardinality_ratio(table[c])
            if ratio < 0.9:
                scored.append((ratio, c))
        if scored:
            scored.sort()
            best = [c for c, r in scored if r == scored[0][0]]
            if len(best) == 1:
                return best[0]
        return None

    if what == "date":
        scored = [(_is_date_like(table[c]), c) for c in ties]
        scored.sort(reverse=True)
        if scored[0][0] >= 0.8 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
    return None


def _assign_fluid(
    kind: str,
    columns: list[str],
    table: dict[str, list[str]],
    sm: SchemaMap,
    taken: set[str | None],
) -> RoleAssignment | None:
    """Claim one fluid column for ``kind``, or say why none could be claimed.

    Three layers of evidence, weakest last: an explicit unit declaration, then
    the column name, then the numbers. Nothing here outranks a declaration, and
    a name match is never promoted above medium unless the data confirms it.
    """
    # 1. the file's own units row, e.g. a column sitting under "bbl/d"
    declared_rate = [
        c
        for c, u in (sm.units or {}).items()
        if _is_interval_unit(u) and _fluid_mentioned(u, kind)
    ]

    name_hits: list[tuple[str, str]] = []
    for c in columns:
        if c in taken:
            continue
        if _rate_kind(c) == kind:
            name_hits.append((c, "column name names the fluid"))
    # 2. a bare fluid name with no rate marker, e.g. "Oil"
    if not name_hits:
        for c in columns:
            if c in taken or c in name_hits:
                continue
            if _rate_kind(c) == kind and _explicit_interval(c):
                name_hits.append((c, "column name names fluid and a rate marker"))

    if not name_hits and not declared_rate:
        return None

    if declared_rate:
        col = declared_rate[0]
        if col in name_hits:
            col = name_hits[0][0]
            basis = "units row declares a per-day rate and the name names the fluid"
        else:
            basis = f"units row declares this column a per-day {kind} rate"
        conf = HIGH
        if len(declared_rate) > 1:
            sm.notes.append(
                f"{len(declared_rate)} columns declare a {kind} rate; "
                f"taking '{col}'"
            )
        return RoleAssignment(kind, col, conf, basis, verified=True)

    col, basis = name_hits[0]
    alternatives = [c for c, _ in name_hits[1:]]

    cumulative = _is_cumulative(col)
    interval = _explicit_interval(col)
    confidence = MEDIUM
    detail = basis

    if table:
        values = [v for v in table.get(col, []) if v not in ("", None)]
        if len(values) >= 10:
            from ..loader import parse_number

            nums = [n for n in (parse_number(v) for v in values) if n is not None]
            if len(nums) >= 10:
                mono, resets = _monotonicity(nums)
                if cumulative or (mono and resets <= len(nums) * 0.02):
                    detail += (
                        "; readings only increase between resets, which is what "
                        "a cumulative total does"
                    )
                    confidence = MEDIUM
                elif not interval:
                    detail += (
                        "; readings move in both directions and no rate marker is "
                        "present, so this reads as a volume over the reporting "
                        "period rather than a per-day rate"
                    )
                else:
                    detail += "; readings move in both directions, as a rate does"

    # Injection gets its own note: BORE_WI_VOL is water injection, not
    # produced water, and conflating them downgrades real findings.
    if _is_injection(col):
        detail += f"; '{col}' reads as injection, not produced {kind}"

    if cumulative:
        detail += "; cumulative meter, so meter-rollover and cumulate-jump apply"

    return RoleAssignment(kind, col, confidence, detail, alternatives=alternatives)


def _is_interval_unit(unit: str) -> bool:
    u = unit.strip().lower()
    return "/d" in u or u.endswith("pd") or "per day" in u or "perday" in u


def _fluid_mentioned(unit: str, kind: str) -> bool:
    """True when a units-row token itself names the fluid, e.g. ``bbl/d oil``."""
    u = unit.lower()
    words = {
        "oil": ("oil",),
        "water": ("water", "wtr"),
        "gas": ("gas",),
    }[kind]
    return any(w in u for w in words)


def _explicit_interval(column: str) -> bool:
    c = column.lower()
    return any(t in c for t in ("per_d", "_per", "/d", "bpd", "rate", "_d ", "_d,")) or c.endswith("_d")


def _annotate_cumulative(sm: SchemaMap, table: dict[str, list[str]]) -> None:
    """Mark cumulative columns so the caller knows which rules can apply."""
    for role, a in sm.roles.items():
        if role not in ROLE_KINDS:
            continue
        col = a.column
        by_name = _is_cumulative(col)
        by_data = False
        if table.get(col):
            from ..loader import parse_number

            nums = [
                n for n in (parse_number(v) for v in table[col][:400]) if n is not None
            ]
            if len(nums) >= 20:
                mono, resets = _monotonicity(nums)
                by_data = mono and resets <= len(nums) * 0.02
        if by_data and not by_name:
            a.basis += (
                "; values only ever increase, so this behaves like a cumulative "
                "total even though the name does not say so"
            )
        elif by_name and not by_data:
            a.basis += (
                "; name says cumulative but values also decrease, which a "
                "monotonic meter should not do"
            )