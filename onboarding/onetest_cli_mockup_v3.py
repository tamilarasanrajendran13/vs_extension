#!/usr/bin/env python3
"""
OneTest CLI mockup v3 — Slate theme, full-screen prompt_toolkit frame. Mock data only.

    pip install prompt_toolkit
    python onetest_cli_mockup_v3.py                        # full-screen app
    python onetest_cli_mockup_v3.py --ascii                # ASCII glyphs (auto on legacy Windows console)
    python onetest_cli_mockup_v3.py --anim 1.5             # shorter logo animation
    echo "run failed" | python onetest_cli_mockup_v3.py    # not a TTY → plain line output

v3 consolidates onetest_cli_mockup.py (v2) with everything that was iterated in
onetest_cli_banners.py, so the mockup is one file again:

  banner S      the logo (5x5 tiles, white "1", accent tile) animated once on startup, with name,
                version + tagline, one line of facts and the hints beside it; redraws in place when
                the engine changes or the terminal is resized
  behaviours    tab flips the engine silently (banner + input border show it); the input border
                carries the ▸ marker; a one-line session summary replaces "bye" on exit
  pickers       /tests and /groups open an arrow-key picker above the input box
                ↑↓ move · space select several · enter run · esc close · typing filters (fuzzy)
  live block    progress is the framework's 8 stages ("stage 3/8 · loading source"), not a fake %
  timestamps    run blocks and the live block stamp lines relative to the run start (+1.2s)
  fuzzy         palette and pickers match subsequences: "cmnull" finds customer_master.null_check
  accent        the brand blue #5b8cff from the logo asset replaces teal as the single accent

Layout (top to bottom, always the full terminal):
  transcript  scrollable history of everything that happened   [+ optional test sidebar]
  live block  the running test, redrawn in place
  input box   rounded frame, pinned to the bottom, never moves
  hint bar    keys on the left, clock and run state on the right

Only `runner` is mock; the sections mirror the module layout intended for the package.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import random
import shutil
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import (ConditionalContainer, Float, FloatContainer, FormattedTextControl,
                                   HSplit, Layout, VSplit, Window, WindowAlign)
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import TextArea

VERSION, PROJECT = "0.0.16", "finance-dq"
TAGLINE = "data validation for spark & polars"

# ── theme: slate, brand-blue accent ────────────────────────────────────────
ACCENT, GRAY, DIM, DIMMER, WARN, FAIL, WHITE = "#5b8cff", "#8b8f98", "#4a4e57", "#2e3138", "#fac775", "#f09595", "#ffffff"
STYLE = Style.from_dict({
    "a": f"fg:{ACCENT}", "ab": f"fg:{ACCENT} bold", "g": f"fg:{GRAY}", "d": f"fg:{DIM}", "dd": f"fg:{DIMMER}",
    "w": f"fg:{WHITE}", "wb": f"fg:{WHITE} bold", "warn": f"fg:{WARN}", "fail": f"fg:{FAIL}",
    "input": f"fg:{WHITE}", "sidebar": "bg:#191b20",
    "completion-menu": "bg:#1c1f24 #c9ccd1",
    "completion-menu.completion.current": f"bg:#2a2d33 {ACCENT}",
    "completion-menu.meta.completion": f"bg:#1c1f24 {GRAY}",
    "completion-menu.meta.completion.current": f"bg:#2a2d33 {GRAY}",
    "scrollbar.background": "bg:#1c1f24", "scrollbar.button": "bg:#2e3138",
})


def c(key: str) -> str:
    """Style class for a fragment; the empty key means unstyled."""
    return f"class:{key}" if key else ""


# ── glyphs ─────────────────────────────────────────────────────────────────
UNICODE = dict(tl="╭", tr="╮", bl="╰", br="╯", h="─", v="│", dot="●", hollow="◌", prompt="›", on="▰", off="▱",
               ok="✓", bad="✗", mid="·", dash="—", swap="↔", updown="↑↓", tri="▸", tile="■", spin="⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
ASCII = dict(tl="+", tr="+", bl="+", br="+", h="-", v="|", dot="*", hollow="o", prompt=">", on="#", off="-",
             ok="+", bad="x", mid="-", dash="-", swap="<->", updown="up/dn", tri=">", tile="#", spin="|/-\\")
G: Dict[str, str] = dict(UNICODE)  # active glyph set, swapped in main()


def detect_ascii(flag: bool) -> bool:
    """--ascii, ONETEST_ASCII=1, a legacy Windows console, or a stdout that cannot encode the glyphs."""
    if flag or os.environ.get("ONETEST_ASCII"):
        return True
    if os.name == "nt" and not any(os.environ.get(v) for v in ("WT_SESSION", "ANSICON", "ConEmuANSI", "TERM_PROGRAM")):
        return True  # classic conhost: cmd.exe or the old PowerShell window
    try:
        "".join(UNICODE.values()).encode(getattr(sys.stdout, "encoding", None) or "ascii")
    except (LookupError, UnicodeEncodeError):
        return True
    return False


def is_ascii() -> bool:
    return G["h"] == "-"


def spin(frame: int) -> str:
    return G["spin"][frame % len(G["spin"])]


# ── store: persisted status + history ──────────────────────────────────────
def default_state_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "onetest" / "cli-mockup"


class Store:
    """status.json (last run per test), history (prompt_toolkit FileHistory), reports/ (mock HTML)."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.status_file = root / "status.json"
        self.history_file = root / "history"
        self.reports_dir = root / "reports"

    def load(self) -> dict:
        try:
            return json.loads(self.status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        self.status_file.write_text(json.dumps(data, indent=1), encoding="utf-8")

    def reset(self) -> None:
        """Forget only what the tool wrote: the two state files and the mock *.html reports."""
        for p in (self.status_file, self.history_file):
            if p.exists():
                p.unlink()
        if self.reports_dir.is_dir():
            for report in self.reports_dir.glob("*.html"):
                report.unlink()
            try:
                self.reports_dir.rmdir()  # only succeeds when nothing else lives there
            except OSError:
                pass

    def newest_report(self) -> Optional[Path]:
        reports = sorted(self.reports_dir.glob("*.html")) if self.reports_dir.exists() else []
        return reports[-1] if reports else None


STORE: Optional[Store] = None


def ago(then: float, now: Optional[float] = None) -> str:
    s = int((now if now is not None else time.time()) - then)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def short(path: Optional[Path], full: bool = False) -> str:
    """`reports/<name>.html` inside the state dir; with full=True the ~-relative path."""
    if path is None:
        return ""
    if not full and STORE:
        try:
            return path.relative_to(STORE.root).as_posix()
        except ValueError:
            pass
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def stamp(rel: float) -> str:
    """Seconds since the run started, as shown in run blocks: +1.2s."""
    return f"+{rel:.1f}s"


# ── catalog (mock data) ────────────────────────────────────────────────────
STAGES = ["reading test case", "setting up engine", "loading source", "loading target",
          "validating schema", "comparing counts", "comparing data", "writing report"]


@dataclass(eq=False)
class TestCase:
    group: str
    name: str
    level: int
    outcome: str                  # mock result of a run: pass | fail
    message: str = ""
    events: list = field(default_factory=list)  # (stage 1-8, level, message) emitted during a run
    status: Optional[str] = None  # last run: pass | fail | cancelled | None = never run
    at: Optional[float] = None    # epoch seconds of the last run
    secs: Optional[float] = None  # duration of the last run
    last_message: str = ""        # failure message of the last run, if it failed

    @property
    def full(self) -> str:
        return f"{self.group}.{self.name}"

    @property
    def failing(self) -> bool:
        return self.status == "fail"


def last_label(t: TestCase) -> str:
    if t.status is None:
        return "never run"
    if t.status == "cancelled":
        return f"{G['hollow']} cancelled"
    return f"{G['ok'] if t.status == 'pass' else G['bad']} {ago(t.at)}"


def status_key(t: TestCase, never: str = "d") -> str:
    return "fail" if t.failing else "a" if t.status == "pass" else never


TESTS = [
    TestCase("customer_master", "row_count", 1, "pass", "",
             [(3, "INFO", "read source   1,204,551 rows   DB2"), (4, "INFO", "read target   1,204,551 rows   hive"),
              (6, "INFO", "counts match")]),
    TestCase("customer_master", "null_check", 2, "fail", "17 nulls in CUST_EMAIL",
             [(3, "INFO", "read source   1,204,551 rows   DB2"), (4, "INFO", "read target   1,204,538 rows   hive"),
              (7, "WARN", "null CUST_EMAIL   17 rows"), (8, "INFO", "writing report")]),
    TestCase("customer_master", "schema_drift", 3, "fail", "+1 column in target (ship_dt)",
             [(3, "INFO", "fetch schema  source  8 cols"), (4, "INFO", "fetch schema  target  9 cols"),
              (5, "ERROR", "unexpected column  ship_dt  in target")]),
    TestCase("customer_master", "pk_uniqueness", 4, "pass", "",
             [(3, "INFO", "hash CUST_ID   1,204,551 rows"), (7, "INFO", "distinct 1,204,551"), (7, "INFO", "no duplicates")]),
    TestCase("orders_daily", "row_count", 1, "pass", "",
             [(3, "INFO", "read source   8,921,004 rows   DB2"), (4, "INFO", "read target   8,921,004 rows   databricks")]),
    TestCase("orders_daily", "dup_keys", 4, "fail", "3 duplicate keys in target",
             [(3, "INFO", "read source   8,921,004 rows"), (4, "INFO", "read target   8,921,007 rows"),
              (7, "WARN", "skew detected   partition 12   4.1x"), (7, "WARN", "3 duplicate keys   ORDER_ID 10093 10094 10221")]),
    TestCase("orders_daily", "amount_sum", 3, "pass", "",
             [(3, "INFO", "sum AMOUNT source  4,102,551,930.12"), (4, "INFO", "sum AMOUNT target  4,102,551,930.12"),
              (7, "INFO", "delta 0.00")]),
    TestCase("ledger_reconcile", "balance_check", 5, "pass", "",
             [(3, "INFO", "read source   402,118 rows   oracle"), (4, "INFO", "read target   402,118 rows   databricks"),
              (7, "INFO", "cell compare  402,118 x 14"), (7, "INFO", "0 mismatches")]),
    TestCase("ledger_reconcile", "fx_rates", 6, "fail", "12 mismatched cells in FX_RATE",
             [(3, "INFO", "read source   9,214 rows"), (4, "INFO", "read target   9,214 rows"),
              (7, "ERROR", "12 mismatched cells   FX_RATE   tolerance 1e-6")]),
]
GROUPS = list(dict.fromkeys(t.group for t in TESTS))
# First-launch seed so the palette shows history on day one; afterwards status.json is the truth.
SEED = {"customer_master.row_count": ("pass", 3 * 60), "customer_master.null_check": ("fail", 2 * 3600),
        "customer_master.schema_drift": ("fail", 2 * 3600), "customer_master.pk_uniqueness": ("pass", 3 * 60),
        "orders_daily.row_count": ("pass", 86400), "orders_daily.dup_keys": ("fail", 86400),
        "orders_daily.amount_sum": ("pass", 86400)}
SPARK_NOISE = ["INFO SparkContext: Running Spark version 3.5.1", "INFO SparkEnv: Registering MapOutputTracker",
               "INFO DAGScheduler: Got job 0 with 200 output partitions", "INFO TaskSetManager: Starting task 0.0 in stage 0.0 (TID 0)",
               "INFO Executor: Finished task 0.0 in stage 0.0. 1834 bytes result sent to driver", "INFO CodeGenerator: Code generated in 42.1 ms",
               "INFO FileSourceStrategy: Pushed Filters:", "INFO MemoryStore: Block broadcast_0 stored as values in memory"]
POLARS_NOISE = ["polars: scan  streaming=true", "polars: predicate pushdown applied", "polars: projection pushdown applied",
                "polars: join strategy hash", "polars: collect  n_threads=10"]


def load_status(store: Store) -> None:
    data = store.load()
    if not data:
        now = time.time()
        data = {k: {"status": s, "at": now - age} for k, (s, age) in SEED.items()}
        store.save(data)
    for t in TESTS:
        rec = data.get(t.full)
        if rec:
            t.status, t.at, t.secs = rec.get("status"), rec.get("at"), rec.get("secs")
            t.last_message = rec.get("message", "")


def record(t: TestCase, status: str, secs: float) -> None:
    t.status, t.at, t.secs = status, time.time(), secs
    t.last_message = t.message if status == "fail" else ""
    if STORE:
        data = STORE.load()
        data[t.full] = {"status": status, "at": t.at, "secs": round(secs, 1), "message": t.last_message}
        STORE.save(data)


def group_tests(group: str) -> List[TestCase]:
    return [t for t in TESTS if t.group == group]


# ── fuzzy matching ─────────────────────────────────────────────────────────
def fuzzy(query: str, text: str) -> Optional[int]:
    """
    Subsequence match score; lower is better, None when `query` does not match.

    Consecutive characters cost nothing, a gap costs 5, a gap landing on a word start
    (after . _ - / or at the beginning) costs 3, so "cmnull" prefers customer_master.null_check.
    """
    q, t = query.lower(), text.lower()
    if not q:
        return 0
    score, pos, prev = 0, 0, -2
    for ch in q:
        pos = t.find(ch, pos)
        if pos < 0:
            return None
        if pos != prev + 1:
            score += 3 if pos == 0 or t[pos - 1] in "._-/ " else 5
        prev = pos
        pos += 1
    return score + (len(t) - len(q)) // 10


def fuzzy_sort(query: str, items, key) -> list:
    """Items matching `query` (all items when it is empty), best score first, original order for ties."""
    scored = [(fuzzy(query, key(item)), i, item) for i, item in enumerate(items)]
    return [item for score, _, item in sorted((s for s in scored if s[0] is not None), key=lambda s: (s[0], s[1]))]


def resolve(arg: str) -> List[TestCase]:
    """all · failed · a group · one or several names (exact, then substring, then best fuzzy match)."""
    a = arg.lower().strip()
    if a in ("", "all"):
        return list(TESTS)
    if a == "failed":
        return [t for t in TESTS if t.failing]
    out: List[TestCase] = []
    for token in a.split():
        hits = ([t for t in TESTS if t.full.lower() == token] or [t for t in TESTS if t.group.lower() == token]
                or [t for t in TESTS if token in t.full.lower()])
        if not hits:
            best = fuzzy_sort(token, TESTS, lambda t: t.full)
            hits = best[:1]
        out += hits
    return list(dict.fromkeys(out))


# ── transcript ─────────────────────────────────────────────────────────────
Frag = Tuple[str, str]


def fmt(frags: List[Frag]) -> List[Frag]:
    """(style key, text) → (style class, text) as stored in the transcript."""
    return [(c(k), t) for k, t in frags]


def tlen(frags: List[Frag]) -> int:
    return sum(len(t) for _, t in frags)


def pad(frags: List[Frag], to: int) -> List[Frag]:
    return frags + [("", " " * max(0, to - tlen(frags)))]


def lr(left: List[Frag], right: List[Frag], width: int) -> List[Frag]:
    return left + [("", " " * max(1, width - tlen(left) - tlen(right)))] + right


class Transcript:
    """Permanent styled lines plus a scroll offset from the bottom. `sink` prints lines in plain mode."""

    def __init__(self) -> None:
        self.lines: List[List[Frag]] = []
        self.scroll_back = 0
        self.sink = None

    def say(self, *frags: Tuple[str, str]) -> None:
        line = fmt(list(frags)) or [("", "")]
        self.lines.append(line)
        self.scroll_back = 0
        if self.sink:
            self.sink(line)

    def blank(self) -> None:
        self.say()

    def clear(self) -> None:
        self.lines.clear()
        self.scroll_back = 0

    def scroll(self, n: int) -> None:
        self.scroll_back = max(0, min(len(self.lines) - 1, self.scroll_back + n))

    def render(self) -> List[Frag]:
        out: List[Frag] = []
        cursor_line = max(0, len(self.lines) - 1 - self.scroll_back)
        for i, line in enumerate(self.lines):
            if i == cursor_line:
                out.append(("[SetCursorPosition]", ""))
            out += line
            out.append(("", "\n"))
        return out


tr = Transcript()
say, blank = tr.say, tr.blank


def cols() -> int:
    app = get_app_or_none()
    return app.output.get_size().columns if app else shutil.get_terminal_size((80, 24)).columns


def refresh() -> None:
    app = get_app_or_none()
    if app:
        app.invalidate()


def rule(width: int, left: str = "", right: str = "") -> str:
    return left + G["h"] * max(0, width - len(left) - len(right)) + right


# ── state ──────────────────────────────────────────────────────────────────
class State:
    engine = "spark"
    sidebar = False
    show_log = False
    cancel = False          # abandon the whole run / batch
    skip = False            # abandon only the current test of a batch
    skip_at = 0.0           # when ctrl+c was last pressed during a batch (second press cancels)
    quit = False
    plain = False
    running: Optional[dict] = None
    queue_len = 0
    last_selection: list = []
    last_report: Optional[Path] = None
    logs: dict = {}
    history: list = []
    ctrlc_at = 0.0
    frame = 0


st = State()
SKIP_WINDOW = 2.0  # seconds after a skip in which a second ctrl+c cancels the batch


def engine_toggle() -> List[Frag]:
    other = "polars" if st.engine == "spark" else "spark"
    return [("a", G["tri"] + " "), ("w", st.engine), ("", "   "), ("g", other)]


# ── banner: the logo, animated once, live afterwards ───────────────────────
# 0 = dim tile, 1 = white tile, 2 = accent tile. The brand asset is 5x6; this is the 5x5 cut.
LOGO = [
    (0, 1, 1, 0, 0),
    (0, 0, 1, 0, 0),
    (0, 0, 2, 0, 0),
    (0, 0, 1, 0, 0),
    (0, 1, 1, 1, 0),
]
LIT_ORDER = [(0, 1), (0, 2), (1, 2), (2, 2), (3, 2), (4, 2), (4, 1), (4, 3)]  # stroke order of the "1"
ACCENT_TILE = (2, 2)
LOGO_W = 2 * len(LOGO[0]) - 1
GAP = "     "                       # between the logo and the text block
ANIM_SECONDS = 3.0                  # total length of the startup animation; --anim overrides
LIVE: dict = {}                     # base, count, settled, width — the printed banner, for redraws


def logo_rows(lit=frozenset(LIT_ORDER), accent: bool = True, scan: Optional[int] = None) -> List[List[Frag]]:
    """The tile grid, one text row per logo row, one glyph per tile, tiles separated by a space."""
    rows: List[List[Frag]] = []
    for r, line in enumerate(LOGO):
        cells: List[Frag] = []
        for col in range(len(line)):
            tile = (r, col)
            if tile in lit:
                key = "a" if accent and tile == ACCENT_TILE else "w"
                glyph = ("*" if key == "a" else "#") if is_ascii() else G["tile"]
            else:
                key = "g" if scan == r else "dd"
                glyph = "." if is_ascii() else G["tile"]
            cells += [(key, glyph), ("", " ")]
        rows.append(cells[:-1])
    return rows


def chip_lines(chips: List[List[Frag]], avail: int, sep: Frag) -> List[List[Frag]]:
    """Join chips with `sep`, wrapping to a new line when the next chip would not fit."""
    lines: List[List[Frag]] = []
    current: List[Frag] = []
    for chip in chips:
        candidate = current + ([sep] if current else []) + chip
        if current and tlen(candidate) > avail:
            lines.append(current)
            current = list(chip)
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def banner_rows(lit, accent: bool, scan: Optional[int]) -> List[List[Frag]]:
    """The banner for one animation state: logo on the left, text block beside and below it."""
    mid = G["mid"]
    sep: Frag = ("d", f"  {mid}  ")
    indent_len = 2 + LOGO_W + len(GAP)
    avail = (cols() - 1) - indent_len
    chips = [
        [("w", PROJECT)],
        [("w", ".env"), ("g", " loaded")],
        engine_toggle(),
        [("w", str(len(TESTS))), ("g", f" tests in {len(GROUPS)} groups")],
    ]
    beside: List[List[Frag]] = [
        [("wb", "one"), ("ab" if accent else "wb", "test")],
        [("g", "v" + VERSION), ("d", f"  {mid}  {TAGLINE}")],
        [],
    ]
    beside += chip_lines(chips, avail, sep)
    beside.append([("d", f"tab switches engine  {mid}  / opens commands and tests")])
    rows: List[List[Frag]] = []
    for i, tiles in enumerate(logo_rows(lit, accent, scan)):
        line = beside[i] if i < len(beside) else []
        rows.append([("", "  ")] + tiles + [("", GAP)] + line)
    for line in beside[len(LOGO):]:
        rows.append([("", " " * indent_len)] + line)
    return rows


def welcome() -> None:
    """Print the banner. Inside the app the mark animates once (scanline, stroke reveal, accent pulse)."""
    final = frozenset(LIT_ORDER)
    animate = tr.sink is None and not st.plain
    if not animate:
        for row in banner_rows(final, True, None):
            say(*row)
        blank()
        return

    base = len(tr.lines)
    rows = banner_rows(frozenset(), False, None)
    for row in rows:
        say(*row)
    blank()
    LIVE.update(base=base, count=len(rows), settled=False, width=cols())

    def frame(lit, accent: bool, scan: Optional[int]) -> None:
        rows = banner_rows(lit, accent, scan)
        for i in range(len(LOGO)):
            tr.lines[base + i] = fmt(rows[i])
        refresh()

    # Phase weights: pause, scanline (5 steps), stroke reveal (8 steps), accent pulse (5 steps).
    unit = ANIM_SECONDS / (0.1 + 5 * 0.04 + 8 * 0.045 + 5 * 0.07)

    def run() -> None:
        try:
            time.sleep(0.1 * unit)
            for r in range(len(LOGO)):
                frame(frozenset(), False, r)
                time.sleep(0.04 * unit)
            lit: Set[Tuple[int, int]] = set()
            for tile in LIT_ORDER:
                lit.add(tile)
                frame(frozenset(lit), False, None)
                time.sleep(0.045 * unit)
            for on in (True, False, True, False, True):
                frame(frozenset(lit), on, None)
                time.sleep(0.07 * unit)
            LIVE["settled"] = True
        except IndexError:
            pass  # transcript was cleared mid-animation; nothing left to draw on

    threading.Thread(target=run, daemon=True).start()


def redraw_banner() -> None:
    """Rewrite the settled banner in place: current engine, current terminal width."""
    if not LIVE.get("settled"):
        return  # the animation thread is still drawing and reads the engine itself
    base, count = LIVE["base"], LIVE["count"]
    if base + count > len(tr.lines):
        return  # transcript was cleared; nothing to update
    lines = [fmt(row) for row in banner_rows(frozenset(LIT_ORDER), True, None)]
    tr.lines[base:base + count] = lines
    LIVE["count"] = len(lines)
    refresh()


# ── runner (mock, stage based) ─────────────────────────────────────────────
def start_run(tests: List[TestCase]) -> None:
    if st.plain:
        runner(tests)
    else:
        threading.Thread(target=runner, args=(tests,), daemon=True).start()


def runner(tests: List[TestCase]) -> None:
    st.last_selection = tests
    st.queue_len = len(tests)
    st.cancel = st.skip = False
    st.skip_at = 0.0
    if len(tests) > 1:
        say(("g", f"  {len(tests)} tests {G['mid']} {st.engine}"))
    passed = failed = skipped = 0
    t0 = time.time()
    results = []
    for t in tests:
        if st.cancel:
            break
        status, secs = run_one(t)
        results.append((t, status, secs))
        passed += status == "pass"
        failed += status == "fail"
        skipped += status == "skipped"
        st.history.append((time.strftime("%H:%M:%S"), t, status, secs))
        st.queue_len -= 1
    if results:
        st.last_report = write_report(results)
    if len(tests) > 1:
        blank()
        line = [("a", f"{passed} passed"), ("", "  "), ("fail" if failed else "g", f"{failed} failed")]
        if skipped:
            line += [("g", f"  {skipped} skipped")]
        if st.cancel and len(results) < len(tests):
            line += [("g", f"  {G['hollow']} cancelled after {len(results)} of {len(tests)}")]
        line += [("g", f"  {time.time() - t0:.1f}s   {short(st.last_report)}")]
        say(*line)
    blank()
    st.running = None
    st.show_log = False
    refresh()


def run_one(t: TestCase) -> Tuple[str, float]:
    """Walk the eight framework stages, emitting engine noise and the test's events; lines carry +seconds."""
    noise = SPARK_NOISE if st.engine == "spark" else POLARS_NOISE
    run = st.running = {"test": t, "lines": [], "scripted": [], "stage": 1, "pct": 0.0, "start": time.time()}
    total = len(STAGES)

    def emit(level: str, msg: str, scripted: bool) -> None:
        line = (time.time() - run["start"], level, msg)
        run["lines"].append(line)
        if scripted:
            run["scripted"].append(line)

    for stage in range(1, total + 1):
        if st.cancel or st.skip:
            break
        run["stage"] = stage
        chatter = random.randint(3, 9) if st.engine == "spark" else random.randint(1, 3)
        for k in range(chatter):
            if st.cancel or st.skip:
                break
            emit("INFO", random.choice(noise), False)
            run["pct"] = (stage - 1 + (k + 1) / (chatter + 1)) / total
            time.sleep(random.uniform(0.01, 0.04))
        for stage_no, level, msg in t.events:
            if stage_no == stage:
                emit(level, msg, True)
        run["pct"] = stage / total
        time.sleep(random.uniform(0.12, 0.3))
    secs = time.time() - run["start"]
    st.logs[t.full] = run["lines"]
    cancelled, skipped = st.cancel, st.skip and not st.cancel
    st.skip = False
    hidden = len(run["lines"]) - len(run["scripted"])
    say(("g", G["dot"] + " "), ("w", t.full), ("g", f"   L{t.level} {G['mid']} {st.engine}"))
    for rel, level, msg in run["scripted"]:
        if level != "INFO":
            say(("g", f"  {stamp(rel):>6}  "), ("warn" if level == "WARN" else "fail", msg))
    if hidden:
        say(("d", f"    {hidden} {st.engine} lines folded   /log {t.full}"))
    if cancelled or skipped:
        say(("g", G["hollow"] + (" skipped" if skipped else " cancelled")),
            ("d", f"  {secs:.1f}s   at stage {run['stage']}/{total} {G['mid']} {STAGES[run['stage'] - 1]}"))
        status = "skipped" if skipped else "cancelled"
    elif t.outcome == "pass":
        say(("a", G["ok"] + " passed"), ("g", f"  {secs:.1f}s"))
        status = "pass"
    else:
        say(("fail", G["bad"] + " failed"), ("g", f"  {secs:.1f}s   "), ("w", t.message))
        status = "fail"
    record(t, "cancelled" if status == "skipped" else status, secs)
    if len(st.last_selection) > 1:
        blank()
    return status, secs


def write_report(results: List[Tuple[TestCase, str, float]]) -> Optional[Path]:
    """A small mock HTML report so /report has something real to open."""
    if not STORE:
        return None
    STORE.reports_dir.mkdir(parents=True, exist_ok=True)
    path = STORE.reports_dir / f"{time.strftime('%Y-%m-%d_%H%M%S')}.html"
    rows = "".join(
        f"<tr><td>{html.escape(t.full)}</td><td class='{s}'>{s}</td><td>{secs:.1f}s</td>"
        f"<td>{html.escape(t.message if s == 'fail' else '')}</td></tr>" for t, s, secs in results)
    path.write_text(
        f"<!doctype html><meta charset='utf-8'><title>onetest {VERSION} report</title>"
        f"<style>body{{background:#0f1114;color:#c9ccd1;font:14px/1.6 -apple-system,'Segoe UI',sans-serif;padding:32px}}"
        f"h1{{color:#fff;font-weight:600}}table{{border-collapse:collapse}}td{{padding:6px 18px 6px 0;border-bottom:1px solid #2e3138}}"
        f".pass{{color:{ACCENT}}}.fail{{color:{FAIL}}}.cancelled,.g{{color:{GRAY}}}</style>"
        f"<h1>one<span style='color:{ACCENT}'>test</span> <span class=g>{VERSION} · {html.escape(PROJECT)} · {st.engine}</span></h1>"
        f"<p class=g>mock report generated {time.strftime('%Y-%m-%d %H:%M:%S')}</p><table>{rows}</table>",
        encoding="utf-8")
    return path


def open_path(path: Path) -> None:
    try:
        webbrowser.open(path.resolve().as_uri())
    except Exception:
        pass  # opener problems must never disturb the screen


# ── commands ───────────────────────────────────────────────────────────────
COMMANDS = {"run": "run a test or group  (all {mid} failed {mid} <name>)", "rerun": "run the last selection again",
            "engine": "switch spark {swap} polars", "list": "groups at a glance  (counts and status dots)",
            "tests": "pick test cases to run  (optional filter)", "groups": "pick groups to run  (optional filter)",
            "log": "unfold a test's engine log", "history": "runs this session", "report": "open the last report",
            "clear": "clear transcript", "help": "keys and commands", "quit": "exit"}
ALLOWED_WHILE_RUNNING = ("help", "quit", "exit", "q", "history", "list", "tests", "groups", "clear")
PICKER = None  # set by build_app; None in plain mode


def request_exit() -> None:
    st.quit = True
    app = get_app_or_none()
    if app:
        app.exit()


def dots(group: str) -> List[Frag]:
    frags = []
    for t in group_tests(group):
        k = status_key(t, never="dd")
        if st.plain:  # no colour without a terminal: spell the status out
            frags.append((k, G["bad"] if t.failing else G["ok"] if t.status == "pass" else G["mid"]))
        else:
            frags.append((k, G["dot"]))
    return frags


def echo(raw: str) -> None:
    """Echo the typed line; control characters are masked so piped input cannot inject escape sequences."""
    safe = "".join(ch if ch.isprintable() else "?" for ch in raw)
    say(("a", G["prompt"] + " "), ("w", safe))


def cmd_tests(arg: str) -> None:
    """Plain-mode fallback for /tests: print the list."""
    tests = fuzzy_sort(arg, TESTS, lambda t: t.full)
    if not tests:
        say(("d", f"  nothing matches {arg!r}"))
    for t in tests:
        say(("w", f"  {t.full:<34}"), ("g", f"L{t.level}   "), (status_key(t), last_label(t)))
    blank()


def cmd_groups(arg: str) -> None:
    """Plain-mode fallback for /groups: print each group with its tests."""
    groups = fuzzy_sort(arg, GROUPS, lambda g: g)
    if not groups:
        say(("d", f"  no group matches {arg!r}"))
    for g in groups:
        tests = group_tests(g)
        say(("w", f"  {g:<20}"), ("g", f"{len(tests)} tests"))
        for t in tests:
            say(("", "    "), (status_key(t, never="dd"), G["dot"]), ("w", f" {t.name:<18}"), ("g", f"L{t.level}   "),
                (status_key(t), last_label(t)))
    blank()


def handle(line: str) -> None:
    raw = line.strip()
    if not raw:
        return
    cmd, _, arg = raw.lstrip("/").partition(" ")
    cmd, arg = cmd.lower(), arg.strip()
    if st.running and cmd not in ALLOWED_WHILE_RUNNING:
        say(("d", f"  a run is in progress {G['dash']} ctrl+c cancels it"))
        return
    if cmd == "engine":  # quiet: the banner and the input border show the result
        if raw != "engine":  # typed, not the tab key: keep the echo of what was typed
            echo(raw)
        st.engine = arg if arg in ("spark", "polars") else ("polars" if st.engine == "spark" else "spark")
        redraw_banner()
        return
    if cmd in ("tests", "groups"):
        if PICKER is not None:
            PICKER.open(cmd, arg)
        else:
            echo(raw)
            (cmd_tests if cmd == "tests" else cmd_groups)(arg)
        return
    echo(raw)
    if cmd in ("quit", "exit", "q"):
        request_exit()
    elif cmd == "help":
        for name, meta in COMMANDS.items():
            say(("w", f"  /{name:<9}"), ("g", meta.format(**G)))
        m = G["mid"]
        say(("d", f"  / palette {m} tab engine {m} {G['updown']} history {m} pgup/pgdn or wheel scroll {m} ctrl+t sidebar {m} ctrl+l clear"))
        say(("d", f"  while running: l unfold log {m} ctrl+c cancels a single run, skips the current test of a batch,"
                  f" twice cancels the batch"))
        say(("d", "  at the prompt: ctrl+c twice or ctrl+d quits"))
        say(("d", f"  in a picker: {G['updown']} move {m} space select {m} enter run {m} esc close {m} typing filters"))
        blank()
    elif cmd == "list":
        for g in GROUPS:
            say(("w", f"  {g:<20}"), ("g", f"{len(group_tests(g)):>3}   "), *dots(g))
        blank()
    elif cmd == "log":
        key = arg if arg in st.logs else next((t.full for t in resolve(arg) if t.full in st.logs), None) if arg else None
        lines = st.logs.get(key) if key else None
        if not lines:
            say(("d", f"  no log for {arg!r} {G['dash']} run it first"))
        for rel, level, msg in (lines or []):
            say(("dd", f"  {stamp(rel):>6}  "), ({"WARN": "warn", "ERROR": "fail"}.get(level, "g"), msg))
        blank()
    elif cmd == "history":
        if not st.history:
            say(("d", "  nothing run yet"))
        for ts, t, status, secs in st.history:
            k, sym = {"pass": ("a", G["ok"]), "fail": ("fail", G["bad"])}.get(status, ("g", G["hollow"]))
            note = f"   {status}" if status in ("skipped", "cancelled") else ""
            say(("d", f"  {ts}  "), (k, sym), ("w", f" {t.full:<34}"), ("g", f"{secs:.1f}s{note}"))
        blank()
    elif cmd == "report":
        path = st.last_report or (STORE.newest_report() if STORE else None)
        if not path:
            say(("d", f"  no report yet {G['dash']} run something first"))
        else:
            say(("g", f"  {short(path, full=True)}"), ("d", "   opening with the default browser"))
            threading.Thread(target=open_path, args=(path,), daemon=True).start()
        blank()
    elif cmd == "clear":
        tr.clear()
    elif cmd in ("run", "rerun"):
        tests = st.last_selection if cmd == "rerun" else resolve(arg)
        if not tests:
            say(("d", f"  nothing matches {arg!r}" if cmd == "run" else "  nothing to rerun yet"))
            blank()
            return
        start_run(tests)
    else:
        say(("d", f"  unknown command {cmd!r} {G['dash']} /help"))
        blank()


# ── palette (the / completer, fuzzy) ───────────────────────────────────────
class OneTestCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        body = text.lstrip("/")
        word = text.rsplit(" ", 1)[-1].lower()
        if text.startswith("/") and " " not in text:
            word = text[1:].lower()  # the palette: everything typed after the slash
            for name, meta in COMMANDS.items():
                if name.startswith(word):
                    yield Completion(f"/{name} ", -len(text), display=f"/{name}", display_meta=meta.format(**G))
            yield from self.targets(word, -len(text), "/run ")
        elif body.startswith("run "):
            for kw, meta in (("all", f"{len(TESTS)} tests"), ("failed", "everything red")):
                if word and kw.startswith(word):
                    yield Completion(kw, -len(word), display_meta=meta)
            yield from self.targets(word, -len(word))
        elif body.startswith("engine "):
            for e in ("spark", "polars"):
                if e.startswith(word):
                    yield Completion(e, -len(word), display_meta="current" if e == st.engine else "")
        elif body.startswith("log "):
            for k in fuzzy_sort(word, list(st.logs), lambda k: k):
                yield Completion(k, -len(word), display_meta=f"{len(st.logs[k])} lines")
        elif body.startswith("groups "):
            for g in fuzzy_sort(word, GROUPS, lambda g: g):
                yield Completion(g, -len(word), display_meta=f"{len(group_tests(g))} tests")
        elif body.startswith("tests "):
            yield from self.targets(word, -len(word))

    def targets(self, word: str, start: int, prefix: str = ""):
        """Tests and groups matching `word` fuzzily, best matches first."""
        candidates = [(t.full, f"L{t.level}   {last_label(t)}") for t in TESTS]
        candidates += [(g, f"{len(group_tests(g))} tests") for g in GROUPS]
        for name, meta in fuzzy_sort(word, candidates, lambda x: x[0]):
            yield Completion(prefix + name, start, display=name, display_meta=meta)


# ── picker: /tests and /groups ─────────────────────────────────────────────
class Picker:
    """
    Arrow-key picker shown above the input box, styled like the palette.

    ↑↓ (pgup/pgdn) move the highlight, space toggles a selection, enter runs the selection
    (or the highlighted item when nothing is selected), esc closes. The input box is the
    filter field while the picker is open; matching is fuzzy.
    """

    PAGE = 12

    def __init__(self, buf) -> None:
        self.buf = buf
        self.kind: Optional[str] = None      # "tests" | "groups" | None (closed)
        self.index = 0
        self.selected: Set[str] = set()
        buf.complete_while_typing = Condition(lambda: self.kind is None)  # no palette while picking

    @property
    def is_open(self) -> bool:
        return self.kind is not None

    def open(self, kind: str, query: str = "") -> None:
        self.kind, self.index = kind, 0
        self.selected.clear()
        if query:  # the input is reset right after the command is accepted; apply the query afterwards
            app = get_app_or_none()
            if app:
                app.loop.call_soon(lambda: setattr(self.buf, "text", query))

    def close(self) -> None:
        self.kind = None
        self.selected.clear()
        self.buf.text = ""

    def items(self) -> list:
        query = self.buf.text.strip()
        if self.kind == "groups":
            return fuzzy_sort(query, GROUPS, lambda g: g)
        return fuzzy_sort(query, TESTS, lambda t: t.full)

    @staticmethod
    def name(item) -> str:
        return item if isinstance(item, str) else item.full

    def move(self, delta: int) -> None:
        n = len(self.items())
        if n:
            self.index = (self.index + delta) % n

    def toggle(self) -> None:
        items = self.items()
        if items:
            key = self.name(items[min(self.index, len(items) - 1)])
            self.selected ^= {key}

    def run_selected(self) -> None:
        items = self.items()
        if not items:
            return
        names = [self.name(i) for i in items if self.name(i) in self.selected] if self.selected \
            else [self.name(items[min(self.index, len(items) - 1)])]
        self.close()
        handle("run " + " ".join(names))

    def bind(self, kb: KeyBindings) -> None:
        is_open = Condition(lambda: self.is_open)
        kb.add("up", filter=is_open)(lambda e: self.move(-1))
        kb.add("down", filter=is_open)(lambda e: self.move(1))
        kb.add("pageup", filter=is_open)(lambda e: self.move(-self.PAGE))
        kb.add("pagedown", filter=is_open)(lambda e: self.move(self.PAGE))
        kb.add("space", filter=is_open)(lambda e: self.toggle())
        kb.add("enter", filter=is_open)(lambda e: self.run_selected())
        kb.add("escape", filter=is_open, eager=True)(lambda e: self.close())
        kb.add("c-c", filter=is_open)(lambda e: self.close())

    def render(self) -> List[Frag]:
        items = self.items()
        n = len(items)
        self.index = min(self.index, max(0, n - 1))
        top = 0 if n <= self.PAGE else min(max(0, self.index - self.PAGE // 2), n - self.PAGE)
        m = G["mid"]
        count = f"   {len(self.selected)} selected" if self.selected else ""
        header = f" {self.kind}{count}   {G['updown']} move {m} space select {m} enter run {m} esc close "

        rows: List[Tuple[List[Frag], bool]] = []
        for i, item in enumerate(items):
            current = i == self.index
            marker = G["tri"] if current else " "
            tick = G["dot"] if self.name(item) in self.selected else " "
            frags: List[Frag] = [("", f" {marker} "), ("a", tick), ("", " ")]
            if self.kind == "groups":
                tests = group_tests(item)
                frags += [("name", f"{item:<20}"), ("meta", f"{len(tests)} tests   ")]
                frags += [(status_key(t, never="dd"), G["dot"]) for t in tests]
            else:
                frags += [("name", f"{item.full:<34}"), ("meta", f"L{item.level}   "), (status_key(item), last_label(item))]
            frags.append(("", " "))
            rows.append((frags, current))
        if not rows:
            rows.append(([("meta", "   no match ")], False))
        preview = self.preview(items[self.index]) if items else []

        width = max([len(header)] + [tlen(f) for f, _ in rows] + [tlen(preview)])
        out: List[Frag] = [("class:completion-menu.meta.completion", header.ljust(width)), ("", "\n")]
        for frags, current in rows[top:top + self.PAGE]:
            base = "class:completion-menu.completion.current" if current else "class:completion-menu"
            for key, text in pad(frags, width):
                if key in ("", "name"):
                    style = base
                elif key == "meta":
                    style = base if current else "class:completion-menu.meta.completion"
                else:
                    style = f"{base},{key}"  # status colours and the selection dot keep their colour
                out.append((style, text))
            out.append(("", "\n"))
        if preview:
            for key, text in pad(preview, width):
                out.append(("class:completion-menu.meta.completion" if key in ("", "meta") else f"class:completion-menu,{key}", text))
            out.append(("", "\n"))
        return out[:-1]

    def preview(self, item) -> List[Frag]:
        """One dim line under the list describing the highlighted item's last run."""
        m = G["mid"]
        if isinstance(item, str):  # a group
            tests = group_tests(item)
            ran = [t for t in tests if t.at]
            red = sum(t.failing for t in tests)
            frags: List[Frag] = [("meta", f"   {len(tests)} tests")]
            if red:
                frags += [("meta", f"  {m}  "), ("fail", f"{red} failing")]
            if ran:
                latest = max(t.at for t in ran)
                frags += [("meta", f"  {m}  last run {ago(latest)}")]
            else:
                frags += [("meta", f"  {m}  never run")]
            return frags + [("", " ")]
        if item.status is None:
            return [("meta", "   never run "), ("", " ")]
        frags = [("meta", "   last run "), (status_key(item), last_label(item))]
        if item.secs:
            frags += [("meta", f"  {m}  {item.secs:.1f}s")]
        if item.last_message:
            frags += [("meta", f"  {m}  "), ("fail", item.last_message)]
        return frags + [("", " ")]


# ── views ──────────────────────────────────────────────────────────────────
class TranscriptControl(FormattedTextControl):
    def mouse_handler(self, ev):
        if ev.event_type == MouseEventType.SCROLL_UP:
            tr.scroll(3)
        elif ev.event_type == MouseEventType.SCROLL_DOWN:
            tr.scroll(-3)
        else:
            return NotImplemented


def live_text() -> List[Frag]:
    r = st.running
    if not r:
        return []
    t = r["test"]
    st.frame += 1
    m = G["mid"]
    total = len(STAGES)
    out = [(c("a"), f"{spin(st.frame)} "), (c("wb"), t.full),
           (c("g"), f"   L{t.level} {m} {st.engine} {m} {time.time() - r['start']:05.2f}s")]
    if st.queue_len > 1:
        out.append((c("d"), f"   +{st.queue_len - 1} queued"))
    out.append(("", "\n"))
    out += [(c("g"), f"  stage {r['stage']}/{total} {m} "), (c("w"), STAGES[r["stage"] - 1]), ("", "\n")]
    visible = r["lines"][-12:] if st.show_log else r["scripted"]
    for rel, level, msg in visible:
        out += [(c("g"), f"  {stamp(rel):>6}  "), (c({"WARN": "warn", "ERROR": "fail"}.get(level, "w")), msg), ("", "\n")]
    hidden = len(r["lines"]) - len(r["scripted"])
    if st.show_log:
        out += [(c("dd"), "    l fold"), ("", "\n")]
    elif hidden:
        out += [(c("dd"), f"    {hidden} {st.engine} lines folded   l unfold"), ("", "\n")]
    w = max(10, min(cols() - 12, 40))
    n = int(w * r["pct"])
    out += [(c("a"), G["on"] * n), (c("dd"), G["off"] * (w - n)), (c("g"), f"  {r['stage']}/{total}")]
    return out


def sidebar_text() -> List[Frag]:
    out = [(c("d"), " tests"), ("", "\n")]
    for g in GROUPS:
        out += [(c("w"), f" {g}"), ("", "\n")]
        for t in group_tests(g):
            cur = st.running is not None and st.running["test"] is t
            k = "a" if cur else status_key(t, never="dd")
            sym = spin(st.frame) if cur else G["dot"]
            out += [(c(k), f"   {sym} "), (c("w" if cur else "g"), f"{t.name:<16}"), (c("d"), f"L{t.level}"), ("", "\n")]
    return out


def border_top() -> List[Frag]:
    w = cols()
    if LIVE.get("settled") and LIVE.get("width") != w:  # terminal resized: re-flow the banner
        LIVE["width"] = w
        redraw_banner()
    return [(c("dd"), G["tl"] + rule(w - 2) + G["tr"])]


def border_bottom() -> List[Frag]:
    label = f" {G['tri']} {st.engine} {G['h']}"
    return [(c("dd"), G["bl"] + G["h"] * max(0, cols() - 2 - len(label)) + " "), (c("a"), G["tri"]),
            (c("g"), " " + st.engine), (c("dd"), " " + G["h"] + G["br"])]


def hints_text() -> List[Frag]:
    if st.running:
        if time.time() - st.skip_at < SKIP_WINDOW and st.queue_len > 1:
            return [(c("w"), " ctrl+c again cancels the whole batch"), ("", " " * max(1, cols() - 60)),
                    (c("a"), f"{G['dot']} skipping   {time.strftime('%H:%M')}")]
        what = "skip test" if st.queue_len > 1 else "cancel run"
        left = f"l unfold log   ctrl+c {what}   pgup/pgdn scroll"
        right = f"{G['dot']} running {time.time() - st.running['start']:04.1f}s"
    else:
        left = f"/ commands   tab engine   {G['updown']} history   ctrl+t tests   ctrl+c quit"
        right = "idle"
    right += "   " + time.strftime("%H:%M")
    pad_ = max(1, cols() - len(left) - len(right) - 2)
    return [(c("d"), " " + left), ("", " " * pad_), (c("a") if st.running else c("d"), right)]


# ── keys + app ─────────────────────────────────────────────────────────────
def build_app(store: Store) -> Application:
    global PICKER
    inp = TextArea(multiline=False, completer=OneTestCompleter(), complete_while_typing=True,
                   history=FileHistory(str(store.history_file)), style="class:input",
                   prompt=[(c("ab"), G["prompt"] + " ")], accept_handler=lambda buf: (handle(buf.text), False)[1])
    inp.buffer.on_text_changed += lambda _: setattr(tr, "scroll_back", 0)
    PICKER = picker = Picker(inp.buffer)

    running = Condition(lambda: st.running is not None)
    input_empty = Condition(lambda: not inp.text)
    picker_closed = Condition(lambda: not picker.is_open)

    transcript_win = Window(TranscriptControl(tr.render), wrap_lines=True, always_hide_cursor=True)
    sidebar_win = Window(FormattedTextControl(sidebar_text), width=30, style="class:sidebar")
    live_win = Window(FormattedTextControl(live_text), wrap_lines=True, dont_extend_height=True, always_hide_cursor=True)
    picker_win = Window(FormattedTextControl(picker.render), dont_extend_width=True, dont_extend_height=True,
                        style="class:completion-menu")

    root = FloatContainer(
        HSplit([
            VSplit([transcript_win, ConditionalContainer(sidebar_win, Condition(lambda: st.sidebar))]),
            ConditionalContainer(HSplit([Window(height=1), live_win, Window(height=1)]), running),
            Window(FormattedTextControl(border_top), height=1),
            VSplit([Window(FormattedTextControl([(c("dd"), G["v"] + " ")]), width=2), inp,
                    Window(FormattedTextControl([(c("dd"), " " + G["v"])]), width=2, align=WindowAlign.RIGHT)]),
            Window(FormattedTextControl(border_bottom), height=1),
            Window(FormattedTextControl(hints_text), height=1),
        ]),
        floats=[Float(left=3, bottom=4, content=CompletionsMenu(max_height=12, scroll_offset=1)),
                Float(left=3, bottom=4, content=ConditionalContainer(picker_win, Condition(lambda: picker.is_open)))],
    )

    kb = KeyBindings()

    @kb.add("c-c")
    def _(e):
        if st.running:
            batch = st.queue_len > 1
            if not batch or time.time() - st.skip_at < SKIP_WINDOW:
                st.cancel = True              # single run, or second press during a batch
            else:
                st.skip = True                # first press during a batch: skip the current test
                st.skip_at = time.time()
        elif inp.text:
            inp.text = ""
        elif time.time() - st.ctrlc_at < 1.5:
            request_exit()
        else:
            st.ctrlc_at = time.time()
            say(("d", "  ctrl+c again to quit"))

    @kb.add("c-d")
    def _(e):
        request_exit()

    @kb.add("tab", filter=input_empty & picker_closed)
    def _(e):
        handle("engine")

    @kb.add("c-t")
    def _(e):
        st.sidebar = not st.sidebar

    @kb.add("c-l")
    def _(e):
        tr.clear()

    @kb.add("l", filter=running & input_empty & picker_closed)
    def _(e):
        st.show_log = not st.show_log

    @kb.add("escape", eager=True)
    def _(e):
        inp.text = ""

    @kb.add("pageup")
    def _(e):
        tr.scroll(12)

    @kb.add("pagedown")
    def _(e):
        tr.scroll(-12)

    picker.bind(kb)  # added last, so while a picker is open its keys win

    return Application(layout=Layout(root, focused_element=inp), key_bindings=kb, style=STYLE,
                       full_screen=True, mouse_support=True, refresh_interval=0.1)


# ── plain (not a TTY) ──────────────────────────────────────────────────────
def run_plain() -> int:
    """Same transcript as plain lines; commands come from stdin, one per line."""
    st.plain = True
    tr.sink = lambda line: print("".join(t for _, t in line).rstrip())
    welcome()
    try:
        for raw in sys.stdin:
            handle(raw.rstrip("\r\n"))
            if st.quit:
                break
    except KeyboardInterrupt:
        st.cancel = True
    return 0


def session_summary() -> None:
    """One line on exit instead of a bare 'bye'."""
    runs = st.history
    sep: Frag = ("d", f"  {G['mid']}  ")
    frags: List[Frag] = [("wb", "one"), ("ab", "test"), sep]
    if not runs:
        frags.append(("d", "no runs this session"))
    else:
        failed = sum(status == "fail" for _, _, status, _ in runs)
        passed = sum(status == "pass" for _, _, status, _ in runs)
        secs = sum(duration for _, _, _, duration in runs)
        frags += [("g", f"{len(runs)} run{'s' if len(runs) != 1 else ''}"), sep,
                  ("fail" if failed else "g", f"{failed} failed"), sep, ("g", f"{passed} passed"), sep, ("g", f"{secs:.1f}s")]
        if st.last_report:
            frags += [sep, ("g", short(st.last_report))]
    print_formatted_text(FormattedText(fmt(frags)), style=STYLE)


# ── main ───────────────────────────────────────────────────────────────────
def main(argv: Optional[List[str]] = None) -> int:
    global STORE, ANIM_SECONDS
    ap = argparse.ArgumentParser(description="OneTest CLI mockup v3 (mock data)")
    ap.add_argument("--ascii", action="store_true", help="ASCII glyphs; auto-detected on legacy Windows consoles")
    ap.add_argument("--engine", choices=("spark", "polars"), default="spark", help="engine shown at start")
    ap.add_argument("--plain", action="store_true", help="plain line output; automatic when stdout is not a TTY")
    ap.add_argument("--state-dir", type=Path, default=default_state_dir(), metavar="DIR",
                    help="where last-run status, history and mock reports live (default: %(default)s)")
    ap.add_argument("--reset", action="store_true", help="forget persisted status and history first")
    ap.add_argument("--anim", type=float, metavar="SECONDS", help=f"length of the logo animation (default {ANIM_SECONDS:g})")
    args = ap.parse_args(argv)

    G.clear()
    G.update(ASCII if detect_ascii(args.ascii) else UNICODE)
    st.engine = args.engine
    if args.anim is not None:
        ANIM_SECONDS = max(0.0, args.anim)
    STORE = Store(args.state_dir)
    if args.reset:
        STORE.reset()
    load_status(STORE)

    if args.plain or not (sys.stdout.isatty() and sys.stdin.isatty()):
        return run_plain()
    app = build_app(STORE)
    welcome()
    app.run()
    session_summary()
    return 0


if __name__ == "__main__":
    sys.exit(main())
