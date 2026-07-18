# Where these files go

Your ledger lives directly under `docket/` — `docket/ledger.py`,
`docket/ledger.db`. The report is a reader of that ledger, so it lives right
next to it. No `workbench/`, no second copy.

```
docket/
├── ledger.py                 ← existing, the sanctioned write path
├── ledger.db                 ← existing, created by ledger.py --init
├── schema.sql                ← existing (if present)
├── report.py                 ← ★ new
├── payload_builder.py        ← ★ new — the ONLY file that reads the db
├── serve.py                  ← ★ new — live localhost view
├── ledger_survey.py          ← ★ new — one-off, tells you your real schema
├── _demo_ledger.py           ← ★ new — synthetic data; delete once real runs exist
└── dashboard/                ← ★ new — the frontend, one folder
    ├── bundle.html
    ├── app.css
    └── app.js
```

That is the whole change: six files and one `dashboard/` folder, all dropped
beside `ledger.py`.

## Copy commands

From the folder where you unpacked `docket-scripts.tar.gz` (the files are under
`docket/scripts/` in the archive — they move up one level into your flat
`docket/`):

```bash
cp docket/scripts/report.py \
   docket/scripts/payload_builder.py \
   docket/scripts/serve.py \
   docket/scripts/ledger_survey.py \
   docket/scripts/_demo_ledger.py \
   /path/to/your/docket/

cp -r docket/scripts/dashboard /path/to/your/docket/
```

`dashboard/` must stay a folder next to the scripts — `report.py` reads
`dashboard/bundle.html`, `app.css` and `app.js` from there and inlines them.

## Run it

```bash
cd /path/to/your/docket

# works right now, no ledger needed — synthetic data
python report.py --demo --out /tmp/demo.html

# the real thing
python report.py --out report.html       # --db defaults to ./ledger.db
python serve.py                          # http://127.0.0.1:8787, live

# before trusting the four curated panels, check my column-name guesses
python ledger_survey.py  --db ledger.db
python payload_builder.py --db ledger.db --doctor
```

Because you run from inside `docket/`, the `--db ledger.db` default already
points at your ledger — drop the flag.

## Two files that are NOT like the others

**`payload_builder.py` reads `ledger.db` directly, not through `ledger.py`.**
Every other script writes via `ledger.py` and never touches raw SQL — that rule
stands. This one only ever *reads*, and read-only via `mode=ro`, so it cannot
corrupt anything. Longer term the reads should move behind `ledger.py` too, once
the schema stops moving. Until then, `ledger_survey.py --doctor` catches a column
rename before it turns a panel into em-dashes.

**`_demo_ledger.py` is the one file you throw away.** Nothing imports it except
the self-tests and the `--demo` flags. The day a real ticket runs end to end,
delete it and repoint the tests at a fixture cut from the real ledger.

## What does NOT change

- `ledger.py`, `schema.sql`, `ledger.db` — unchanged; the report only reads.
- Whatever extension / `package.json` you have — the report never touches the
  extension host, because nothing here calls `vscode.lm`.
