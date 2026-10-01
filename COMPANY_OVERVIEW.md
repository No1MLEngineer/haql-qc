# haql-qc: Production Data Quality Control

Source material for video generation. Every number below comes from a real run against Equinor's public Volve field dataset, a production dataset released for research use.

---

## The problem

Before an oil and gas company can trust a number, someone has to check whether the number is possible.

A reservoir team receives a production export. It contains a year of daily records for every well: how much oil came out, how much gas, how much water, how many hours the well was actually on stream. That export then flows into reserves calculations, into a joint venture partner's numbers, and eventually into a valuation someone signs.

The checking is almost always done by hand, in a spreadsheet, by an engineer who opens the file and looks at it. Fifteen thousand rows of data is not something a person can look at. They scan for what looks strange, and what does not look strange gets a quiet pass.

This is the core risk. Not that the engineer is careless, but that inspection does not scale. A subtle contradiction sitting in row thirteen thousand looks exactly like a correct row when you are scanning.

A second problem compounds it. When an engineer does find something wrong, the natural instinct is to correct it. But which value was wrong, the uptime field or the production volume? The tool that flagged the problem usually cannot know either. So the correction gets made anyway, on a guess, and the audit trail is gone. Nobody can later prove what the data said on the day it was received.

## What haql-qc is

haql-qc is a command-line program that checks production data for physical impossibilities before the data is trusted.

It reads a CSV file. It applies eleven deterministic rules. It writes a report that names every problem, points at the exact source row, and records what the file contained when it was read.

Three design decisions separate it from a spreadsheet.

**It never edits your data.** The tool is read-only by construction. It cannot correct a value because correcting a value requires knowing which of two contradictory fields is the liar, and that is a judgment the data cannot supply. The tool tells you the two fields disagree. A qualified engineer decides which one is wrong.

**It never invents a unit.** This is the decision that matters most. A production number without a unit is not interpretable, and a confidently wrong unit is worse than no check at all. So haql-qc refuses to infer units from column names. If a column is named "BORE_WAT_VOL" and holds water production, the tool does not assume barrels per day. The operator declares the unit explicitly, and the tool records that declaration in the report. Silence is treated as more honest than a guess.

**It never calls out to a network.** No telemetry, no phone-home, no external service. Reservoir data is commercially sensitive and in many jurisdictions cannot leave the operator's infrastructure. haql-qc runs entirely on the machine it is installed on.

## The rules

The eleven rules fall into four groups.

Four rules check structure. Is every row labelled with a well? Does every row have a parseable date? Is every value in a numeric column actually a number? Does any well appear twice for the same day?

Seven rules check physics. Does any production volume come out negative? Does a cumulative total ever decrease? Are there gaps in a well's reporting history longer than its own established pattern? Is a day count outside a valid range? Can a well report zero uptime while still producing? Does any rate exceed a physical plausibility ceiling? Do the cumulative totals and the daily rates agree with each other?

The rules are deliberately boring. They encode textbook reservoir data hygiene, not clever inference. That is intentional. A check that is easy to explain is a check an auditor can accept.

Two design choices are worth calling out.

The plausibility ceiling is a physical sanity limit, not a business limit. A reading of five hundred thousand barrels per day is not "unusual," it is a decimal error. haql-qc will not tell you whether a well is performing well, because that is a reservoir question, and it refuses to answer questions outside its competence.

Date gaps are judged against each well's own established reporting cadence. A well reporting monthly is not missing data because a well reporting daily has data every day. The tool learns the pattern per well and flags only departures from it.

## What it found in real data

The Volve field is a mature offshore oil field on the Norwegian continental shelf. Its data is public. haql-qc was run against its full daily production export: fifteen thousand six hundred and thirty-four rows, seven wells.

The tool returned eighty findings.

Twelve were high severity. Thirty-four were medium. Thirty-four were informational.

**Four negative water production volumes.** Water came out of four well-days at negative volumes. The largest was minus four hundred fifty-seven point eight four cubic metres, on the thirteenth of August, 2012. A negative volume is not physically realisable. Either the sign convention is wrong or the meter failed, and either way the number cannot go into a reserves calculation as it stands.

**Four rows where a well reports zero uptime but is producing at full rate.** This is the most interesting finding, and the one that took the most care to get right.

On the seventeenth of January, 2015, well "NO 15/9-F-11 H" reports zero on-stream hours. That field, "ON_STREAM_HRS", is the export's own statement of whether the well was running. At the same time, the same row reports one thousand and twenty six point five seven cubic metres of oil. That well's own historical median is one thousand and eighty eight point eight.

The same row also reports gas at ninety-one percent and water at eighty-two percent of their medians. Three separate streams all say the well was running. The uptime field says it was not.

The well was producing. The uptime field is wrong.

The tool cannot know that on its own. What it can do is present the contradiction and let a reservoir engineer resolve it. It flags the uptime column, flags the volume column, shows the volume as a percentage of that well's own historical median, and names the exact source row.

**Thirty-eight date gaps.** The longest is twenty-nine consecutive missing days on one well, in November 2016. The report includes the cadence confidence that triggered it, so a reviewer can judge whether the pattern was stable enough for the gap to mean anything.

## A lesson in severity

Finding thirty-four shut-in contradictions sounded like a bug at first. Thirty-four is a lot of noise.

They were not all the same finding. Four of them were material, meaning the reported volume was between eighty-two and one hundred and nine percent of that well's own typical output. Those are real contradictions and they earned high severity.

The other thirty-four are trivially small against their own well's history: every one falls below two point seven percent of that well's median for that column. The smallest is zero point zero zero two eight cubic metres, on a well that normally injects about five thousand seven hundred. They are mathematically inconsistent and practically irrelevant.

The distinction was drawn by comparing each value against that same well's own median, rather than against a fixed threshold. The logic matters. Zero point zero zero three cubic metres is nothing next to a well producing six thousand cubic metres a day. The same value would be an emergency for a well producing five.

A tool that reported all thirty-eight at high severity would be dismissed by anyone who looked at it for five minutes. A tool that reported none of them would miss a genuine data error. The material-versus-trivial split is what makes the finding usable, and it is only possible because the tool looks at each well's history rather than at a global constant.

## Verification

Every claim in this document was produced by running the tool, not by describing it.

The rules are pinned by two hundred and eighteen automated tests. The full daily export and the monthly export were both audited. Peak oil, water and gas rates were recomputed independently, outside the tool, and all fell below the plausibility ceiling, confirming that the zero-breach result was correct rather than a silent skip. The report records a SHA-256 hash of the input file, so the exact bytes audited can be proven later. The monthly export, five hundred and twenty-six rows across seven wells, returned zero findings.

The audit engine runs on the standard library alone: no machine learning framework, no network call, no service to reach. Reading spreadsheets needs one optional dependency, and only when you hand the tool a spreadsheet. The program is about four thousand nine hundred lines of Python. It runs on Python three point nine through three point thirteen, verified from a clean install on both ends of that range.

That last point is deliberate. A quality control tool that cannot be installed reproducibly is not usable in an environment where auditability is the reason you are buying.

## What it does not do

This section matters more than the others.

haql-qc does not correct data. Not because correction is dangerous, but because it requires knowledge the data does not contain.

It does not judge whether a well is performing well. Plausibility is not performance.

It does not infer units. The operator declares them.

It does not model the reservoir. There is no physics simulation, no material balance, no decline curve. These are structural and physical consistency checks, not engineering analysis.

It does not learn from your data and it does not call a language model. Every rule is deterministic and inspectable. Running the same file twice gives the same answer, and you can read the source code that produced it.

Gas unit conversion from standard cubic metres to thousand cubic feet is geometric arithmetic, but the standard conditions assumed by a dataset must be verified by whoever owns that dataset. Until they are, the tool reports the number in the unit it was given and refuses to translate it into a claim.

It has been validated against one public field. One field is a starting point, not a general guarantee.

## Why it is built this way

Software used in reserves and acquisition decisions has one job above all others: it must be explainable.

That constraint drove every design choice. Read-only, because a tool that edits cannot be defended after the fact. Declared units, because a wrong unit is a silent error. Deterministic rules, because an answer that changes between runs cannot be audited. Exact source rows, because "there was a problem" is not actionable but "row thirteen thousand three" is. And refusing to guess whenever the data is genuinely ambiguous, because a confident wrong answer costs more than an admitted uncertainty.

The result is a tool that will sometimes tell you it does not know. That is the feature.

## Where it fits

haql-qc sits at the front of the data pipeline, before a dataset reaches reserves engineering, a joint venture audit, or a valuation model.

Its role is to make the incoming data defensible. Not to analyse the reservoir, and not to replace existing systems. It is the checkpoint that says the data in this file has been examined, these are the contradictions found, and here is the proof.

For an operating company, that means a defensible starting position for internal reporting and for partner submissions.

For an acquisition, it means knowing what you are actually buying, before the price is agreed.

For a joint venture partner or reserves auditor, it means being able to demonstrate that the numbers were checked by something other than a spreadsheet.

## Status

Version zero point two point zero. Ruleset one point one point zero. Rules are permanently numbered: adding a rule never renumbers an existing one, because the audit logs referencing them are already in other people's hands.

The dataset used for validation, Volve, is maintained by Equinor and released for research use. Any redistribution terms are a separate matter from the tool itself and have not been resolved.