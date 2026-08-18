Issue: DATACMP-0
Type: Task
Priority: Low
Summary: Shakedown ticket - honor the declared CSV source encoding in the polars engine.

=== Acceptance Criteria (source: local file) ===

1. A CSV file whose bytes are latin-1 encoded and whose test case source declares `encoding: latin-1` compares EQUAL to a UTF-8 target with identical logical content: `summary.mismatched_rows == 0` and `passed` is true, and the value read from the source is the real text (e.g. the name "Jose" with an acute accent on the e), never a Unicode replacement character (U+FFFD).
2. A CSV file read with the default encoding (utf-8, undeclared) behaves exactly as before: same columns, same rows, same verdict - the existing ASCII/UTF-8 comparison behavior stays green with no change.
3. A declared encoding that Python does not recognize (e.g. `encoding: not-a-codec`) fails the run as a harness error with a clear message naming the bad codec - never a silent lossy read.

=== Description ===

This is Docket's SHAKEDOWN ticket: the smallest real change the pipeline
can run end to end (comprehension through mutation) against data_project.
It exists to validate Docket itself cheaply after pipeline changes - run
this BEFORE any DATACMP-3-class replay. Target: well under 150k recorded
tokens, all nine stages reached.

THE DEFECT (proven by a deterministic pristine probe, 2026-08-05):
`PolarsEngine.read_csv` passes `encoding=source.encoding if
source.encoding == "utf8" else "utf8-lossy"` to `pl.read_csv`. The config
default is the string "utf-8" (with a dash), so the declared encoding is
NEVER honored: a test case that says `encoding: latin-1` (or cp1252 - the
usual legacy-export encodings) has its bytes force-decoded as lossy UTF-8.
Every non-ASCII character becomes U+FFFD, and the comparison then reports
FALSE MISMATCHES against a correct target ("Jos<U+FFFD>" != "Jose" with
accent). The tool's one job is to tell matching data from mismatched data;
this makes it cry wolf on correct data.

Owning production area: `src/datacompare/engines/polars_engine.py`,
function `PolarsEngine.read_csv` ONLY. Polars' own reader accepts only
utf8/utf8-lossy, so honoring another declared codec means decoding the
file's bytes with the declared codec and handing polars clean UTF-8
(e.g. read bytes, `bytes.decode(source.encoding)`, re-encode utf-8,
`io.BytesIO`). Keep type inference and the existing reader options
(delimiter, has_header, infer_schema_length) exactly as they are.

Implementation notes (ratified decisions, not up for re-litigation):
- One production file: `src/datacompare/engines/polars_engine.py`. No
  reader-signature changes (`readers/csv.py` stays a delegate), no config
  schema changes (`Source.encoding` already exists and is already
  documented as the declared encoding).
- utf-8 and utf8 declarations keep the current fast path (pass through to
  polars); only a NON-utf8 declared codec takes the decode path.
- `Source.path` is a `str` in the real YAML/CLI path. Convert it with
  `Path(source.path)` (or use `open(source.path, "rb")`) before reading bytes;
  unit tests must construct `Source` with the same string type the loader uses.
- An unknown codec raises a clear error that the CLI's existing harness
  error handling surfaces as exit 2 (AC3). Do not invent a new error
  class if a ValueError/LookupError with a clear message suffices.
- Tests create the latin-1 source bytes and matching UTF-8 target at runtime
  under `tmp_path`. They must use ASCII-only Python source (for example,
  escaped byte/code-point literals) so the repository's ASCII guard remains
  green. No checked-in encoded data fixture is required or allowed.
- In the CLI JSON result, `passed` is a TOP-LEVEL `ComparisonResult` field;
  `summary` contains row counts and `match_pct` only. Acceptance assertions
  use `data["passed"]`, never `data["summary"]["passed"]`.

Out of scope (do not touch):
- `engines/spark_engine.py` (Spark has its own encoding option; separate
  ticket if ever needed).
- `readers/xml.py` and everything XML.
- Encoding AUTODETECTION of any kind (chardet etc.) - the declared value
  is the only authority.
- UTF-16/UTF-32 support beyond what the declared-codec decode gives for
  free; no new config knobs; no BOM special-casing (the standard decoders
  already handle BOMs).
- The HTML report, checks, keys detection, CLI flags.
- `sample_data/**` and `testcases/**`; do not add source/target CSV fixtures
  or YAML cases for this fix. The unit and frozen acceptance tests own their
  temporary inputs.

Expected baseline-red reason (for freeze qualification): the frozen
acceptance test for AC1 fails on pristine code with an ASSERTION-level
failure - `summary.mismatched_rows` is 1 (expected 0) because the
latin-1 source value decoded lossily to a replacement character. This is
the intended product reason, not a fixture or import error. AC2's shape
is a preservation test (declare it as such). AC3 is feature-red: pristine
code silently lossy-reads instead of raising.

Expected final verification command (from the data_project root, its
venv): `venv/bin/python -m pytest test/acceptance tests -q`
(the frozen acceptance suite plus the existing regression suite; all
green after implementation).
