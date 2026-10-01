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

    0.4.0 -- the tool version moves because the ingest layer is new: content
    based format detection, adapters for XLSX/ZIP/JSON/JSONL/DBF/fixed-width,
    a schema mapper that assigns roles with a confidence and a stated basis,
    and the --ingest/--convert/--explain CLI surface. The ruleset number does
    not move, because none of this changes which findings fire, at what
    severity, or what they claim. A report produced against the same bytes by
    0.3.0 and by 0.4.0 means the same thing.

    The license verifier's embedded ed25519 public key was replaced with a
    freshly generated issuer keypair held outside the repository. Tokens issued
    under the previous key will not verify against 0.4.0.
"""

__version__ = "0.4.0"

RULESET_VERSION = "1.2.0"

TOOL = "haql-qc"
