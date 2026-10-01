"""Single source of truth for version numbers.

Kept in its own dependency-free module so ``pyproject.toml`` can read the
version without importing the package, and so the audit record, the CLI banner,
and the installed distribution metadata cannot drift apart.

The two numbers are deliberately different things:

``__version__``
    The distribution. Bumped for any change to the package.

``RULESET_VERSION``
    The meaning of the findings. Bumped whenever a rule starts or stops firing,
    changes severity, or changes what a finding claims. Audit records quote it,
    so a report stays interpretable after the code has moved on.

    1.1.0 -- QC010 now recognises produced water (``BORE_WAT_VOL``) and
    distinguishes injection, which lowers injection negatives to medium. QC015
    converts to canonical units before comparing and emits an INFO naming any
    column it could not cover.

    1.2.0 -- QC014's message now distinguishes an injected row from a produced
    row, so a shut-in claim can no longer be misread as a production claim on an
    injection well. The wording changed what a finding asserts, which is exactly
    the sort of change that invalidates an older audit record's reading, so the
    ruleset version moves even though the finding count does not.
"""

__version__ = "0.3.0"

RULESET_VERSION = "1.2.0"

TOOL = "haql-qc"
