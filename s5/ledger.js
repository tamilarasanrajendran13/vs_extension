/**
 * Docket - ledger bridge.
 *
 * The JS harness shells out to ledger.py rather than opening SQLite directly.
 * Two reasons, both deliberate:
 *   1. VS Code's bundled Node has no sqlite module we can rely on.
 *   2. ONE write path. Two implementations drift, and the day they drift is the
 *      day the ledger stops being evidence.
 *
 * Ledger writes are tens-per-run, not thousands. A subprocess per write is fine.
 */

const { execFile } = require('child_process');

function call(cfg, command, payload = {}) {
  return new Promise((resolve, reject) => {
    execFile(
      cfg.python,
      [cfg.ledgerPy, 'cli', command, JSON.stringify(payload), '--db', cfg.ledgerDb],
      { cwd: cfg.repoRoot, timeout: 30000, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        // Exit 2 = the ledger rejected the write (bad gate outcome, missing
        // citation). That is a real answer, and it arrives as JSON on stdout.
        // Anything else = the process died, which is a different problem.
        let parsed = null;
        try {
          parsed = JSON.parse(String(stdout).trim());
        } catch (_) { /* not JSON - fall through to the error below */ }

        if (parsed && parsed.error) {
          return reject(new Error(`ledger rejected ${command}: ${parsed.error}: ${parsed.message}`));
        }
        if (err && !parsed) {
          return reject(new Error(
            `ledger ${command} failed to run: ${err.message}\n` +
            `python: ${cfg.python}\n${String(stderr).trim()}\n` +
            `If this is "command not found", pin the absolute venv path in .docket/config.json.`
          ));
        }
        if (!parsed) return reject(new Error(`ledger ${command} returned no JSON: ${stdout}`));
        resolve(parsed);
      }
    );
  });
}

const startRun = (cfg, p) => call(cfg, 'start-run', p).then((r) => r.run_id);
const log = (cfg, p) => call(cfg, 'log', p).then((r) => r.event_id);
const gate = (cfg, p) => call(cfg, 'gate', p).then((r) => r.event_id);
const endRun = (cfg, p) => call(cfg, 'end-run', p);
const writeDossier = (cfg, p) => call(cfg, 'write-dossier', p).then((r) => r.dossier_id);
const proposeLearning = (cfg, p) => call(cfg, 'propose-learning', p).then((r) => r.learning_id);
const resume = (cfg, p) => call(cfg, 'resume', p).then((r) => r.dossier);
const transcript = (cfg, p) => call(cfg, 'transcript', p).then((r) => r.events);
const search = (cfg, p) => call(cfg, 'search', p).then((r) => r.hits);
const dangerZones = (cfg, p = {}) => call(cfg, 'danger-zones', p).then((r) => r.zones);

module.exports = {
  call, startRun, log, gate, endRun, writeDossier,
  proposeLearning, resume, transcript, search, dangerZones,
};
