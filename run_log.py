#!/usr/bin/env python3
"""
run_log - capture a run's channel output to a per-run evidence log file.

Every line the harness prints to the VS Code channel (via tx.progress / say) is
also written here, timestamped. So each run leaves a stage-by-stage, line-by-line
account on disk:

  - recorded as an evidence artifact (shows on the dashboard + run drill-down),
  - attachable to the Jira ticket alongside the other evidence,
  - and it does NOT bloat the ledger or the dashboard - those only link to it,
    which was the whole point: the detail lives in a file, keyed by run and time.

Files land at:  <workspace>/evidence/run-<run_id>-<YYYYMMDD-HHMMSS>.log
so runs of the same ticket, and iterations across days, are told apart by name.

Self-test (stdlib only):  python run_log.py --self-test
"""

from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

_BAR = "=" * 60


class RunLog:
    def __init__(self, fh, path, rel_path):
        self._fh = fh
        self.path = path
        self.rel_path = rel_path
        self._closed = False

    def write(self, text=""):
        """Write one say() call, timestamped per non-empty line."""
        if self._closed:
            return
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        s = "" if text is None else str(text)
        if s == "":
            self._fh.write("\n")
        else:
            for line in s.split("\n"):
                if line.strip() == "":
                    self._fh.write("\n")
                else:
                    self._fh.write(ts + "  " + line + "\n")
        self._flush()

    def stage(self, name):
        """Optional explicit stage banner, if the caller wants clear sections."""
        if self._closed:
            return
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        self._fh.write("\n" + _BAR + "\n" + ts + "  STAGE: " + str(name).upper()
                       + "\n" + _BAR + "\n")
        self._flush()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._fh.write("\n" + ("-" * 60) + "\nrun log closed "
                           + datetime.datetime.now().isoformat(timespec="seconds") + "\n")
            self._fh.close()
        except Exception:
            pass

    def _flush(self):
        try:
            self._fh.flush()
        except Exception:
            pass


def _safe(s):
    return "".join(c if (c.isalnum() or c in "-_.") else "-" for c in str(s))


def open_for(ws, run_id, ticket_id, project=None, release=None):
    """Open a per-run log under <ws>/evidence/ and write the header.

    Never raises for a logging reason the caller cannot recover from - on any
    filesystem trouble it returns a no-op sink so a run is never blocked by its
    own log.
    """
    try:
        evidence = Path(ws) / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        name = "run-{}-{}.log".format(_safe(run_id), stamp)
        path = evidence / name
        fh = open(path, "w", encoding="utf-8")
        header = [
            _BAR, "DOCKET RUN LOG",
            "ticket : {}".format(ticket_id),
            "run    : {}".format(run_id),
            "project: {}".format(project or "-"),
            "release: {}".format(release or "-"),
            "started: {}".format(datetime.datetime.now().isoformat(timespec="seconds")),
            _BAR, "",
        ]
        fh.write("\n".join(header) + "\n")
        fh.flush()
        return RunLog(fh, str(path), "evidence/" + name)
    except Exception:
        return _NullLog()


class _NullLog:
    """A sink used only if the real log could not be opened; never blocks a run."""
    rel_path = None
    path = None

    def write(self, text=""):
        pass

    def stage(self, name):
        pass

    def close(self):
        pass


def tee(progress_fn, rlog):
    """Wrap a progress/say function so every call is ALSO written to the log.

    Logging failures are swallowed - the channel output must never break because
    a log write failed.
    """
    def say(text=""):
        try:
            rlog.write(text)
        except Exception:
            pass
        if progress_fn is not None:
            return progress_fn(text)
    return say


# ==================================================================== self-test

def _self_test():
    import tempfile

    checks = []

    def ok(name, cond):
        checks.append((name, bool(cond)))

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "development" / "R1" / "OT-1"

        rl = open_for(ws, "OT-1-run-3", "OT-1", project="onetest", release="R1")
        ok("log file created under evidence/", Path(rl.path).exists())
        ok("rel_path points into evidence/", rl.rel_path.startswith("evidence/run-OT-1-run-3-"))
        ok("rel_path ends .log", rl.rel_path.endswith(".log"))

        # tee both to a captured channel and the file
        seen = []
        say = tee(lambda t="": seen.append(t), rl)
        say("lead declaring the blast radius...")
        say("")                       # blank line
        say("  MAY touch (2):\n    [file] src/a.py\n    [file] src/b.py")
        rl.stage("develop")
        say("developer writing code...")
        rl.close()

        text = Path(rl.path).read_text(encoding="utf-8")
        ok("channel still received output", seen[0] == "lead declaring the blast radius...")
        ok("header names the ticket + run", "ticket : OT-1" in text and "run    : OT-1-run-3" in text)
        ok("lines are timestamped", any(l[:8].count(":") == 2 and "lead declaring" in l
                                        for l in text.splitlines()))
        ok("multi-line say preserved", "src/a.py" in text and "src/b.py" in text)
        ok("stage banner written", "STAGE: DEVELOP" in text)
        ok("closes cleanly", "run log closed" in text)
        ok("write after close is a no-op", (rl.write("late"), "late" not in
                                            Path(rl.path).read_text(encoding="utf-8"))[1])

        # a bad path must degrade to a null sink, never raise
        bad = open_for("/nonexistent\x00/x", "R", "T")
        try:
            s2 = tee(lambda t="": None, bad)
            s2("still fine")
            bad.close()
            ok("bad path -> null sink, no raise", True)
        except Exception:
            ok("bad path -> null sink, no raise", False)

    passed = sum(1 for _, c in checks if c)
    for name, c in checks:
        print("  [{}] {}".format("ok " if c else "XX", name))
    print("\n{}/{} checks passed".format(passed, len(checks)))
    return passed == len(checks)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Docket per-run channel log")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)
    if args.self_test:
        sys.exit(0 if _self_test() else 1)
    ap.print_help()


if __name__ == "__main__":
    main()
