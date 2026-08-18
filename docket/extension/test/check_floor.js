// check_floor.js - the executed-check floor, in ONE maintained place.
//
// CORR-B / CH-13. A harness suite reports two different things and only one
// of them was ever guarded: WHAT it found (every named check) and WHETHER IT
// RAN (the tally). A suite that stops half way through prints a smaller green
// tally and exits zero, and the ladder - which reads the exit code - records
// a PASS. Measured, not theorised: an early `return` planted in
// journey_suite.js's last section printed "185/185 checks passed" and exited
// 0, seven checks gone with nothing in the output saying so.
//
// e2e_nine_stage.js already carried the first half of the answer: a pinned
// TOTAL_CHECKS asserted as the last named check of main(). That closes a
// SECTION that stops early. It does not close main() itself returning early,
// or throwing past the printer, or the process exiting before anything is
// printed at all - in each of those the floor check is one of the things that
// never ran. Measured too: the same plant, placed inside
// host_suite_mocked.js's main(), skipped its own floor and printed
// "49/49 checks passed", exit 0.
//
// So the guarantee is registered on process exit instead, where nothing in
// the suite can route around it:
//
//     installFloor({ name: "journey_suite", total: TOTAL_CHECKS,
//                    count: () => results.length });
//
// On the normal path count() === total and this is silent. On any other path
// it prints one loud line naming the shortfall and forces a non-zero exit,
// overriding an exit code already chosen (an 'exit' listener that assigns
// process.exitCode wins, which is exactly the case this has to cover -
// every one of these suites ends in process.exit(0) on green).
//
// It is a claim about the SUITE, never about the product. Keep it that way:
// nothing here may know what a gate, a run or a ticket is.
//
// Pure ASCII. No vscode, no network, no model.

"use strict";

/**
 * @param {object} opts
 * @param {string} opts.name     the suite's name, for the failure line
 * @param {number} opts.total    the pinned number of checks the suite owns
 * @param {function(): number} opts.count  how many actually ran, read at exit
 * @param {function(string): void} [opts.write]  output sink (tests inject)
 * @returns {function(): object} the guard, exposed so a harness can assert
 *   its behaviour directly instead of only through a planted truncation.
 */
function installFloor(opts) {
  const name = String(opts.name || "suite");
  const total = Number(opts.total);
  const count = opts.count;
  const write = opts.write
    || function (s) { process.stdout.write(s); };

  function verdict() {
    const ran = Number(count());
    if (!isFinite(total) || total <= 0) {
      return { ok: false, ran: ran,
               why: name + ": FLOOR NOT SET - the suite declares no check "
                    + "total, so a truncated run cannot be detected" };
    }
    if (ran === total) return { ok: true, ran: ran, why: "" };
    return {
      ok: false, ran: ran,
      why: name + ": FLOOR VIOLATED - " + ran + " of " + total + " checks "
           + "ran. A suite that stops early can never masquerade as a "
           + "shorter green one; this run is NOT evidence of anything.",
    };
  }

  process.on("exit", function () {
    const v = verdict();
    if (v.ok) return;
    write("\n  [XX] " + v.why + "\n");
    process.exitCode = 1;
  });

  return verdict;
}

module.exports = { installFloor };
