/* ==========================================================================
   Docket dashboard - renderer.

   A pure function of the payload. It does not know SQLite exists, it cannot
   reach a model, and it has no dependencies - because it has to run in three
   places that agree on nothing else:

     1. a VS Code webview      payload arrives by postMessage, live
     2. a self-contained .html payload inlined by report.py, emailed to a VP
     3. a read-only server     if that ever earns its keep

   Hence: no framework, no CDN, no fetch. An emailed report that needs the
   network is a report that renders as a blank page on the one laptop that
   matters.

   THE ONE RULE: null is not zero.
   If the ledger did not record it, this prints an em-dash and says why on
   hover. It never prints a 0 it invented. A zero is a claim, and a dashboard
   that makes up claims is worse than no dashboard.
   ========================================================================== */

(function () {
  "use strict";

  var GATE_ABBR = {
    comprehension: "COMP", frozen_tests: "SPEC", unit_tests: "DEV",
    blind_review: "REV", security_snyk: "SEC", qa_e2e: "QA", mutation: "MUT"
  };

  // What each non-curated table in the ledger is FOR, in plain English. Keyed by
  // table name. Shown on the Ledger tab so a discovered table reads as something
  // meaningful, not a bare bar chart. A table with no entry here still shows its
  // rows and columns - it just says "no description on file yet".
  var TABLE_INFO = {
    checkpoints: "A snapshot of the code taken before each task, so any change can be undone. The checkpointer writes one per task; rollback restores from these.",
    dossier: "The cartographer's map of the code around a ticket - the files, symbols, and boundaries the pipeline reasons over. Read by the planner and the lead.",
    edges: "Dependency links between files or symbols, used to work out a change's blast radius. Feeds the boundary the lead declares.",
    escaped_defects: "Bugs that reached production and were traced back to the run that shipped the code. This is how a gate's real-world miss rate is measured - the number that makes gates earn their cost.",
    learnings: "Lessons the retro agent proposed and you ratified, stored per agent. They become part of that agent's memory on the next run.",
    rollbacks: "Every time a ticket was restored to an earlier checkpoint - what was rolled back, when, and to which point.",
    governor_decisions: "Every allow / ask / deny the governor made on an agent's action. Denials cluster where an agent tried to reach outside its blast radius.",
    tool_calls: "Every tool an agent invoked - grep, read, shell - with its result. The raw trace behind what an agent actually did.",
    events: "The per-step record of every model turn - already shown, per ticket, in the Runs drill-down.",
    prompts: "Prompt text and versions - surfaced on the Prompts tab, correlated with merge rate."
  };

  var state = { payload: null, filter: "all", open: null, openGate: null,
                // V4.4 Runs: the attempt filters and the SELECTED attempt.
                // view = {issue, run} scopes the drill-down to one attempt;
                // null means the latest (the ticket row's own fields).
                runsQ: "", runsWf: "", runsStage: "", runsDate: "",
                view: null,
                // V4.4 Cost: the call-explorer workbench. callF holds the
                // six filter axes ("" = all), callQ the search text, and
                // usageSel the one selected breakdown bar ("dim:value" or
                // null) - selecting a bar filters the explorer, selecting
                // it again clears.
                callQ: "", usageSel: null,
                callF: { actor: "", stage: "", model: "", ok: "",
                         priced: "", cache: "" },
                // V4.4 Prompts: the three filter axes ("" = all).
                promptF: { agent: "", stage: "", model: "" },
                // V4.4 Agents: filter chip, search, sort and view mode.
                agentF: "all", agentQ: "", agentSort: "pipeline",
                agentMode: "cards",
                // V4.4 Artifacts: the evidence-browser filters.
                artKind: "", artTicket: "", artQ: "" };

  // ---- the two sentences the findings surface is allowed to say when it has
  // no numbers. They are DIFFERENT facts and they get different words:
  //
  //   payload.findings === null   this ledger has no findings table at all.
  //                               Nothing was measured. Saying "0 findings"
  //                               here would be inventing a measurement.
  //   findings.by_status === {}   the table is there and nothing is in it.
  //                               That IS a measurement, and it reads zero.
  //
  // Neither string may contain a digit: the moment "unavailable" carries a 0
  // it starts reading as a count, which is the exact lie this file exists to
  // refuse. report.py --self-test pins both, and pins that they differ.
  var FINDINGS_UNAVAILABLE =
    "Unavailable - this ledger has no findings table, so nothing was measured.";
  var FINDINGS_EMPTY =
    "No findings recorded - the findings table is here and empty.";
  var VERDICT_UNAVAILABLE = "No verdict recorded for this run";

  // ---- formatters. every one of them handles null first. ----------------

  function unk(why) {
    var s = document.createElement("span");
    s.className = "unk";
    s.textContent = "-";
    s.title = why || "not recorded in the ledger";
    return s;
  }

  function money(v) {
    if (v === null || v === undefined) return null;
    return "$" + Number(v).toFixed(2);
  }

  function num(v) {
    if (v === null || v === undefined) return null;
    return Number(v).toLocaleString();
  }

  function pct(v) {
    if (v === null || v === undefined) return null;
    return Math.round(v * 100) + "%";
  }

  function hours(v) {
    if (v === null || v === undefined) return null;
    if (v < 1) return Math.round(v * 60) + "m";
    if (v < 48) return (Math.round(v * 10) / 10) + "h";
    return (Math.round(v / 24 * 10) / 10) + "d";
  }

  function put(el, text, why) {
    el.textContent = "";
    if (text === null || text === undefined) el.appendChild(unk(why));
    else el.textContent = text;
    return el;
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  // ---- now-line: the four-authority liveness rule -----------------------
  //
  // ACTIVE is earned, never inferred: the host process must be alive, the
  // host's own projection must name the run, the payload must record that
  // run as running, and its workflow must be undecided. Every other
  // combination gets its own honest sentence. The vocabulary (which
  // workflow states are decided) arrives IN the payload - this file only
  // intersects the authorities it is handed, it invents none of them.
  function nowLineModel(p, host) {
    var lv = (p && p.liveness) || { recorded_running: [] };
    var rec = lv.recorded_running || [];
    var project = (p && p.scope && p.scope.project) || null;
    function m(state, sentence) {
      return { state: state, sentence: sentence, project: project,
               recorded: rec.length };
    }
    if (!host) {
      if (rec.length) {
        return m("unverified", rec.length + " recorded running row" +
          (rec.length === 1 ? "" : "s") + " - this host cannot verify a " +
          "live process, so they are shown as history, not activity");
      }
      return m("idle", "idle - nothing is recorded running (liveness not " +
               "checkable in this host)");
    }
    if (!host.live) {
      if (rec.length) {
        return m("stale", "idle - " + rec.length + " recorded running row" +
          (rec.length === 1 ? "" : "s") + " with no live process behind " +
          (rec.length === 1 ? "it" : "them") + "; historical, not active");
      }
      return m("idle", "idle" + (project ? " - " + project : "") +
               " - no live process and nothing recorded running");
    }
    if (!host.run || !host.run.run_id) {
      return m("starting", "a process is alive; the run's identity is " +
               "pending its first event");
    }
    var rid = host.run.run_id;
    var row = null;
    for (var i = 0; i < rec.length; i++) {
      if (rec[i].run_id === rid) row = rec[i];
    }
    if (!row) {
      return m("pending", "a process is alive for run " + rid + " but " +
               "this payload does not record it running yet - waiting " +
               "for the ledger");
    }
    if (row.workflow_decided) {
      return m("decided", "run " + rid + " has a live process but its " +
               "workflow is decided (" + row.workflow_state + ") - " +
               "shown as settled, not active");
    }
    if (host.run.state && host.run.state !== "running") {
      return m("settling", "run " + rid + " - the live view reports " +
               host.run.state + "; not presented as active");
    }
    return m("active", "ACTIVE - " + (row.ticket_id || "?") + " - run " +
             rid + " - live process verified" +
             (project ? " - " + project : ""));
  }

  function renderNowLine(p) {
    var el = document.getElementById("nowline");
    if (!el) return;
    var v = nowLineModel(p, window.DOCKET_HOST || null);
    el.textContent = v.sentence;
    el.className = "wrap nowline nl-" + v.state;
  }

  // ---- Needs You: the decisions only a human can take --------------------
  //
  // Derived from the workflow authority - the latest workflow per ticket,
  // (created_at, workflow_id) tie-break, the approved V4.2 identity rule -
  // plus the folded run verdicts. One row per ticket and the workflow
  // authority wins; an older BLOCKED workflow is excluded ONLY when a
  // strictly newer workflow exists, and it is counted, never silently
  // dropped. A comprehension halt may claim questions only when question
  // evidence is actually retained in the ledger.
  function needsYouModel(p) {
    var tickets = (p && p.tickets) || [];
    var kernel = p && p.kernel;
    var wfs = (kernel && kernel.workflows) || [];
    var trans = (kernel && kernel.transitions) || [];
    var byTicket = {};
    tickets.forEach(function (t) { byTicket[t.issue] = t; });
    var latest = {};
    var perTicket = {};
    wfs.forEach(function (w) {
      if (!(w.ticket_id in byTicket)) return;
      perTicket[w.ticket_id] = (perTicket[w.ticket_id] || 0) + 1;
      var cur = latest[w.ticket_id];
      if (!cur
          || (w.created_at || "") > (cur.created_at || "")
          || ((w.created_at || "") === (cur.created_at || "")
              && (w.workflow_id || "") > (cur.workflow_id || ""))) {
        latest[w.ticket_id] = w;
      }
    });
    var superseded = 0;
    wfs.forEach(function (w) {
      if (!(w.ticket_id in byTicket)) return;
      if (w.state === "BLOCKED" && latest[w.ticket_id]
          && latest[w.ticket_id].workflow_id !== w.workflow_id) {
        superseded++;
      }
    });
    function blockReason(wfId) {
      var last = null;
      trans.forEach(function (x) {
        if (x.workflow_id === wfId && x.to_state === "BLOCKED") last = x;
      });
      return last ? (last.reason || null) : null;
    }
    function badge(t) {
      var w = latest[t];
      if (!w || !w.workflow_id) return null;
      return { id8: String(w.workflow_id).slice(-8),
               count: perTicket[t] || 1 };
    }
    var rows = [];
    var seen = {};
    Object.keys(latest).forEach(function (t) {
      var w = latest[t];
      var tk = byTicket[t];
      var v = tk && tk.verdict;
      if (w.state === "READY") {
        rows.push({ kind: "ready", ticket_id: t, at: w.created_at || "",
                    workflow_id: w.workflow_id,
                    ident: (tk && tk.run) || t,
                    eyebrow: "ready / delivery is manual",
                    headline: (v && v.headline) || "workflow READY",
                    badge: badge(t), verdict: v,
                    // Ship rides the workflow authority that owns this
                    // row: READY exists only through the pipeline's own
                    // success, and delivery is deliberately manual.
                    actions: [{ label: "Ship Run",
                      title: "records the merge; creates branch, commit "
                        + "and PR body - delivery is deliberately "
                        + "manual" }],
                    artifacts: (tk && tk.artifacts) || [] });
        seen[t] = true;
      } else if (w.state === "BLOCKED") {
        var why = blockReason(w.workflow_id);
        var bHead = (v && v.headline)
          || (why ? "workflow BLOCKED - " + why
                 : "workflow BLOCKED - the attempt's verdict does not "
                   + "flag a human, but the delivery state needs "
                   + "intervention");
        // The typed failure is data honesty: when the verdict headline
        // does not already carry the recorded transition reason, the row
        // says both.
        if (why && bHead.indexOf(why) === -1) {
          bHead += " (" + why + ")";
        }
        rows.push({ kind: "blocked", ticket_id: t, at: w.created_at || "",
                    workflow_id: w.workflow_id,
                    ident: t,
                    eyebrow: ((v && v.at) || "workflow")
                      + " / blocked - needs intervention",
                    headline: bHead,
                    badge: badge(t), verdict: v,
                    actions: verbFor(v),
                    artifacts: (tk && tk.artifacts) || [] });
        seen[t] = true;
      }
    });
    tickets.forEach(function (t) {
      var v = t.verdict;
      if (!v || !v.needs_human || seen[t.issue]) return;
      var lw = latest[t.issue];
      // A strictly newer DECIDED workflow supersedes a verdict halt.
      if (lw && (lw.state === "COMPLETED" || lw.state === "CANCELLED")) {
        return;
      }
      var row = { kind: "halt", ticket_id: t.issue, at: "",
                  ident: t.issue,
                  eyebrow: (v.at || "pipeline") + " / "
                    + (v.state === "blocked"
                       ? "blocked - needs intervention"
                       : "awaiting a human"),
                  headline: v.headline || "a human is needed",
                  badge: null, verdict: v,
                  actions: verbFor(v),
                  artifacts: (t.artifacts || []) };
      if (v.at === "comprehension") {
        var arts = (t.artifacts || []).slice();
        (t.runs || []).forEach(function (r) {
          (r.artifacts || []).forEach(function (a) { arts.push(a); });
        });
        var q = arts.some(function (a) {
          return /question/i.test(String(a.rel_path || ""));
        });
        row.questions = q
          ? { present: true, note: "clarifying questions are recorded" }
          : { present: false,
              note: "question evidence was not retained in the ledger" };
      }
      rows.push(row);
    });
    rows.sort(function (a, b) {
      if ((a.at || "") !== (b.at || "")) {
        return (a.at || "") > (b.at || "") ? -1 : 1;
      }
      if ((a.workflow_id || "") !== (b.workflow_id || "")) {
        return (a.workflow_id || "") > (b.workflow_id || "") ? -1 : 1;
      }
      return 0;
    });
    return {
      state: "ok",
      rows: rows,
      superseded_blocked: superseded,
      // The approved V4.4 header explanation - human words only; the
      // deterministic identity rule is spelled out on the supersession
      // note below the card, where the mockup carries it.
      basis: kernel
        ? "derived from actionable workflow states: questions and halts "
          + "awaiting a human, blocked attempts needing intervention, "
          + "READY awaiting delivery"
        : "workflow tables are not recorded in this ledger, so only the "
          + "recorded run endings can ask for you here",
    };
  }

  // The verb a row offers is a MODEL decision (Resume for a resumable
  // attempt, Review questions for an author-blocked one); the renderer
  // only draws it.
  function verbFor(v) {
    if (v && v.resumable) {
      return [{ label: "Resume Run",
        title: "a resume re-pays only the stages that never passed" }];
    }
    if (v && v.needs_human) {
      return [{ label: "Review questions",
        title: "the run is waiting on the ticket author" }];
    }
    return [];
  }

  // The approved V4.4 actions, in the design's one .act treatment. The
  // dashboard stays a read-only projection: these are the affordances the
  // approved design shows, each explaining itself in its title.
  function nyActions(row) {
    var out = (row.actions || []).map(function (a) {
      return '<button class="act" title="' + esc(a.title) + '">'
        + esc(a.label) + "</button>";
    });
    var hasFlow = (row.artifacts || []).some(function (a) {
      return a.kind === "report";
    });
    if (hasFlow) {
      out.push('<button class="act">Open flow report</button>');
    } else {
      out.push('<button class="act" aria-disabled="true" title="this '
        + "attempt recorded no flow report of its own - the report is "
        + "written at run end and authorization never borrows another "
        + 'attempt\'s artifacts">Open flow report</button>');
    }
    return out.join(" ");
  }

  function renderNeedsYou(p) {
    var host = $(".needs-you");
    if (!host) return;
    var basisEl = $(".needs-you-basis");
    var superHost = $(".needs-you-superseded");
    var m = needsYouModel(p);
    if (basisEl) basisEl.textContent = m.basis;
    host.textContent = "";
    if (superHost) superHost.textContent = "";
    if (!m.rows.length) {
      host.appendChild(el("div", "empty",
        "Nothing is waiting on you in this scope."));
    }
    m.rows.forEach(function (r) {
      var cls = r.kind === "blocked" ? " failed"
        : (r.kind === "halt"
           ? (r.verdict && r.verdict.state === "blocked"
              ? " failed" : " halted")
           : "");
      var row = el("div", "tax-row" + cls);
      row.appendChild(el("div", "count", "1"));
      var body = el("div");
      body.appendChild(el("div", "gate", r.eyebrow));
      var reason = el("div", "reason");
      var h = "<b>" + esc(r.ident) + "</b> - " + esc(r.headline) + ".";
      if (r.badge) {
        h += ' <span class="snapnote" title="the deterministically-latest '
          + "workflow for this ticket: created_at, then workflow_id as "
          + 'the stable tie-breaker">workflow ' + esc(r.badge.id8)
          + " - latest of " + r.badge.count + "</span>";
      }
      h += " " + nyActions(r);
      if (r.questions) {
        h += '<span class="q">' + esc(r.questions.note) + "</span>";
      }
      reason.innerHTML = h;
      body.appendChild(reason);
      row.appendChild(body);
      host.appendChild(row);
    });
    if (m.superseded_blocked > 0 && superHost) {
      var n = m.superseded_blocked;
      var one = n === 1;
      var note = el("p", "tab-intro");
      note.innerHTML = n + " older BLOCKED workflow" + (one ? "" : "s")
        + (one ? " is" : " are")
        + " superseded by newer attempts of the same tickets and "
        + (one ? "is" : "are") + " excluded here - each is counted "
        + "superseded ONLY because the same ticket has a strictly newer "
        + "workflow (created_at, then workflow_id as the stable "
        + 'tie-breaker). Their journeys are on <a class="act" '
        + 'href="#/findings">Findings</a> (transitions).';
      superHost.appendChild(note);
    }
  }

  // The folded verdict line: every run's ending through the ONE verdict
  // fold (run_verdict), rendered beside the raw outcome column it corrects.
  function renderVerdictLine(p) {
    var host = $(".verdict-line");
    if (!host) return;
    host.textContent = "";
    var t = (p && p.totals) || {};
    var vc = t.run_verdict_counts || null;
    if (!vc || !Object.keys(vc).length) {
      host.appendChild(el("span", "vl-note",
        "folded run verdicts unavailable - the verdict fold needs the " +
        "ledger beside it"));
      return;
    }
    host.appendChild(el("span", "vl-label", "Folded run verdicts"));
    Object.keys(vc).sort().forEach(function (k) {
      var chip = el("span", "vl-chip vl-" + k);
      chip.appendChild(el("b", null, String(vc[k])));
      chip.appendChild(document.createTextNode(" " + k));
      host.appendChild(chip);
    });
    host.appendChild(el("span", "vl-note",
      "the one verdict fold (run_verdict), not the raw outcome column"));
  }

  // ---- masthead + lead --------------------------------------------------

  function renderLead(p) {
    var scope = p.scope || {};
    var bits = [];
    if (scope.project) bits.push(scope.project);
    if (scope.release) bits.push(scope.release);
    $(".scope").textContent = bits.length ? bits.join(" / ") : "all releases";
    $(".stamp").textContent = "generated " + (p.generated_at || "").replace("T", " ")
      + "\n" + (p.generated_by || "");

    var t = p.totals || {};
    var h = p.hero;

    // The hero is whatever --hero says it is. This function used to know it was
    // cost; now it knows nothing except how to render one number well.
    var eyebrow = $(".lead-figure .eyebrow");
    var v = $(".lead-figure .value");
    var note = $(".lead-figure .note");
    v.textContent = "";

    if (!h || h.value === null || h.value === undefined) {
      if (eyebrow) eyebrow.textContent = (h && h.label) || "-";
      v.appendChild(unk("this ledger records nothing to compute it from"));
      note.textContent = h ? h.note : "";
    } else {
      if (eyebrow) eyebrow.textContent = h.label;
      var s = FMT[h.format](h.value);
      // Split the trailing fraction so the big number stays a shape, not a
      // wall of digits. Works for $0.28 and for 25% alike.
      var m = /^(.*?)([.,]\d+|%)$/.exec(s);
      if (m && m[2] === "%") {
        v.appendChild(document.createTextNode(m[1]));
        v.appendChild(el("span", "cents", "%"));
      } else if (m) {
        v.appendChild(document.createTextNode(m[1]));
        v.appendChild(el("span", "cents", m[2]));
      } else {
        v.textContent = s;
      }

      var arc = "";
      if (h.first !== null && h.first !== undefined && h.first_release) {
        arc = " Was " + FMT[h.format](h.first) + " in " + h.first_release + ".";
      }
      note.textContent = h.note + arc;
    }

    // Build the figures from the outcomes actually PRESENT, not a fixed four.
    // If the ledger has 'escalated' and 'ambiguous', they get tiles too - the
    // whole point of the review that prompted this: nothing goes uncounted.
    // THE verdict decides what a ticket IS. runs.outcome is the word end_run
    // wrote, and on a run whose end_run never got to write it still says
    // "running" long after the workflow reached READY - so counting it here
    // put a delivered ticket in the "running" column of the Overview while
    // the row one tab across read Complete. payload.totals.verdict_counts
    // folds the same verdict every other surface folds; the raw counts stay
    // in the run-status strip below, where they are labelled as raw.
    var counts, order, LABELS, CLS, basis;
    if (t.verdict_counts) {
      counts = t.verdict_counts;
      order = t.verdicts || Object.keys(counts);
      LABELS = { halted: "awaiting a human", unrecorded: "no verdict recorded" };
      CLS = { halted: "is-halt", stopped: "is-fail" };
      basis = "counted by the run verdict, not the raw ledger outcome";
    } else {
      counts = t.outcome_counts || {};
      order = t.outcomes ||
        ["merged", "completed", "halted", "failed", "running"];
      LABELS = { halted: "awaiting a human" };
      CLS = { halted: "is-halt", failed: "is-fail" };
      basis = "recorded ledger outcome; this payload folds no verdict";
    }
    var figs = [["tickets", num(t.tickets), "", ""]];
    order.forEach(function (o) {
      figs.push([LABELS[o] || o, num(counts[o] != null ? counts[o] : t[o]),
                 CLS[o] || "", basis]);
    });
    // A rate needs its divisor stated or it is a number with no meaning. A
    // run still in flight has decided nothing and is not in the denominator.
    var decidedNote = t.tickets_decided != null
      ? "over the " + t.tickets_decided + " ticket" +
        (t.tickets_decided === 1 ? "" : "s") + " that have finished; " +
        "tickets still running are not counted"
      : "";
    figs.push(["completion", pct(t.completion_rate), "", decidedNote]);
    figs.push(["first pass", pct(t.first_pass_rate), "",
               decidedNote ? "completed on the first attempt, " + decidedNote
                           : ""]);
    figs.push(["median cycle", hours(t.median_cycle_hours), "", ""]);
    var host = $(".figures");
    host.textContent = "";
    figs.forEach(function (f) {
      var d = el("div", "figure");
      if (f[3]) d.title = f[3];
      d.appendChild(put(el("div", "n" + (f[2] ? " " + f[2] : "")), f[1]));
      d.appendChild(el("div", "l", f[0]));
      host.appendChild(d);
    });

    // Run-level status strip. The figures above count TICKETS by their latest
    // status; this counts every RUN, so statuses that only ever appear mid-
    // ticket (escalated retries, ambiguous attempts) are never hidden by
    // grouping. Only shown when runs outnumber tickets - otherwise it just
    // repeats the figures.
    var runStrip = $(".run-status-strip");
    if (runStrip) {
      var rc = t.run_outcome_counts || {};
      var ro = t.run_outcomes || [];
      var rtot = t.run_total || 0;
      if (rtot > (t.tickets || 0) && ro.length) {
        runStrip.hidden = false;
        runStrip.textContent = "";
        var lead = el("span", "rss-lead",
          rtot + " run attempt" + (rtot === 1 ? "" : "s") + " across "
          + t.tickets + " ticket" + (t.tickets === 1 ? "" : "s")
          + " (raw recorded outcomes, labeled raw):");
        runStrip.appendChild(lead);
        ro.forEach(function (o) {
          var chip = el("span", "rss-chip v-" + o);
          chip.appendChild(el("span", "rss-dot", ""));
          // The raw axis stays raw: the recorded outcome word, marked
          // (raw) as the approved design labels it - verdict words live
          // in the figures above, never here.
          chip.appendChild(document.createTextNode(" " + o + " (raw) "));
          chip.appendChild(el("span", "rss-n", String(rc[o])));
          runStrip.appendChild(chip);
        });
      } else {
        runStrip.hidden = true;
      }
    }
  }

  // ---- the gate walk ----------------------------------------------------

  // ---- V4.4 Runs: the attempt filters, the all-attempts lens, and the
  // one attempt-selection action. runsFilterModel is the pure seam: given
  // the payload and a filter set it answers which attempts and which
  // tickets are in view, plus the option lists DERIVED from the data.
  function runsFilterModel(p, f) {
    f = f || {};
    var q = String(f.q || "").toLowerCase();
    var optWf = {};
    var optStage = {};
    var optDate = {};
    var attempts = [];
    var tickets = [];
    (p && p.tickets || []).forEach(function (t) {
      var runs = (t.runs && t.runs.length ? t.runs : [t]);
      var any = false;
      runs.forEach(function (r) {
        var v = r.verdict || {};
        if (v.workflow_state) optWf[v.workflow_state] = 1;
        if (r.stopped_at) optStage[r.stopped_at] = 1;
        if (r.started) optDate[String(r.started).slice(0, 7)] = 1;
        var hay = (String(t.issue || "") + " " + String(r.run || "")
                   + " " + String(v.workflow_id || "")).toLowerCase();
        var ok = (!q || hay.indexOf(q) >= 0)
          && (!f.wf || v.workflow_state === f.wf)
          && (!f.stage || r.stopped_at === f.stage)
          && (!f.date || String(r.started || "").slice(0, 7) === f.date);
        if (ok) {
          attempts.push(r);
          any = true;
        }
      });
      if (any) tickets.push(t.issue);
    });
    attempts.sort(function (a, b) {
      return String(b.started || "").localeCompare(String(a.started || ""));
    });
    return { attempts: attempts, tickets: tickets,
             options: { wf: Object.keys(optWf).sort(),
                        stage: Object.keys(optStage).sort(),
                        date: Object.keys(optDate).sort() } };
  }

  function currentRunsFilters() {
    return { q: state.runsQ, wf: state.runsWf, stage: state.runsStage,
             date: state.runsDate };
  }

  function updateRunsCount() {
    var elc = $(".runs-count");
    if (!elc || !state.payload) return;
    var all = runsFilterModel(state.payload, {});
    var now = runsFilterModel(state.payload, currentRunsFilters());
    elc.textContent = "showing " + now.attempts.length + " of "
      + all.attempts.length + " attempts across " + now.tickets.length
      + " ticket" + (now.tickets.length === 1 ? "" : "s");
  }

  function rerenderRuns() {
    updateRunsCount();
    renderWalk(state.payload);
    renderRunsAttempts(state.payload);
  }

  function renderRunsToolbar(p) {
    var host = $(".runs-toolbar");
    if (!host) return;
    host.classList.add("tux-bar");
    host.textContent = "";
    var all = runsFilterModel(p, {});
    host.appendChild(el("span", "tux-level",
      "filters - counts are run attempts"));
    var q = el("input", "tux-q runs-q");
    q.type = "search";
    q.value = state.runsQ || "";
    q.placeholder = "ticket / run id / workflow id";
    q.setAttribute("aria-label",
                   "Search runs by ticket, run id or workflow id");
    q.addEventListener("input", function () {
      state.runsQ = q.value;
      rerenderRuns();
    });
    host.appendChild(q);
    function addSel(label, key, opts, current) {
      var s = el("select", "runs-sel runs-sel-" + key);
      s.setAttribute("aria-label", label);
      var o0 = el("option", null, label + ": all");
      o0.value = "";
      s.appendChild(o0);
      opts.forEach(function (v) {
        var o = el("option", null, v);
        o.value = v;
        if (current === v) o.selected = true;
        s.appendChild(o);
      });
      s.addEventListener("change", function () {
        if (key === "wf") state.runsWf = s.value;
        else if (key === "stage") state.runsStage = s.value;
        else state.runsDate = s.value;
        rerenderRuns();
      });
      host.appendChild(s);
    }
    addSel("workflow state", "wf", all.options.wf, state.runsWf);
    addSel("stopped at", "stage", all.options.stage, state.runsStage);
    addSel("started month", "date", all.options.date, state.runsDate);
    host.appendChild(el("span", "tux-count runs-count"));
    updateRunsCount();
  }

  function renderRunsAttempts(p) {
    var host = $(".runs-attempts");
    if (!host) return;
    host.textContent = "";
    var fm = runsFilterModel(p, currentRunsFilters());
    if (!fm.attempts.length) {
      host.appendChild(el("div", "empty",
                          "No attempts match this filter."));
      return;
    }
    var head = el("div", "run-row head attempt-head");
    ["run", "ticket", "status", "workflow", "tokens in", "started"]
      .forEach(function (h) { head.appendChild(el("span", null, h)); });
    host.appendChild(head);
    fm.attempts.forEach(function (r) {
      var viewing = state.view && state.view.run === r.run;
      var row = el("div", "run-row attempt-row" + (viewing ? " viewing" : ""));
      row.setAttribute("role", "button");
      row.tabIndex = 0;
      row.setAttribute("aria-label", "open attempt " + (r.run || "?"));
      var rv = verdictView(r);
      row.appendChild(el("span", "run-when att-id", r.run || ""));
      row.appendChild(el("span", "att-issue", r.issue || ""));
      var stEl = el("span", "run-status v-" + rv.status,
                    rv.label || "\u2014");
      if (rv.state === "recorded") stEl.title = rv.headline;
      row.appendChild(stEl);
      var v = r.verdict || {};
      row.appendChild(el("span", "att-wf", v.workflow_id
        ? v.workflow_id + " (" + (v.workflow_state || "?") + ")"
        : "no recorded link"));
      var tok = el("span", "att-tok");
      put(tok, r.tokens_in != null ? num(r.tokens_in) : null,
          "tokens not recorded for this attempt");
      row.appendChild(tok);
      row.appendChild(el("span", "run-when",
        (r.started || "").replace("T", " ").slice(0, 16)));
      function openIt() { openAttempt(r.issue, r.run); }
      row.addEventListener("click", openIt);
      row.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openIt();
        }
      });
      host.appendChild(row);
    });
  }

  // The one attempt-selection action: opens the ticket and scopes the
  // drill-down to exactly that attempt. Exported for the host harness.
  function openAttempt(issue, run) {
    state.open = issue;
    state.view = { issue: issue, run: run };
    renderWalk(state.payload);
    renderRunsAttempts(state.payload);
  }

  function renderWalk(p) {
    var head = $(".walk-head .gate-cols");
    head.textContent = "";
    (p.gate_order || []).forEach(function (g) {
      var info = (p.gate_info || {})[g];
      var c = el("div", "eyebrow", GATE_ABBR[g] || g.slice(0, 4).toUpperCase());
      c.title = info ? info.label + " - " + info.desc : g;
      head.appendChild(c);
    });

    var host = $(".walk");
    host.textContent = "";

    // V4.4: the toolbar's search and axis filters intersect the status
    // chips - a ticket stays in view when ANY of its attempts matches.
    var fm = runsFilterModel(p, currentRunsFilters());
    var inView = {};
    fm.tickets.forEach(function (n) { inView[n] = true; });
    var rows = (p.tickets || []).filter(function (t) {
      if (!inView[t.issue]) return false;
      if (state.filter === "all") return true;
      if (state.filterLevel === "run") {
        // a ticket matches if ANY of its runs had this status
        var runs = t.runs || [t];
        return runs.some(function (r) { return r.outcome === state.filter; });
      }
      return t.outcome === state.filter;
    });

    if (!rows.length) {
      host.appendChild(el("div", "empty", "No runs match this filter."));
      return;
    }

    rows.forEach(function (t, i) {
      var row = el("button", "walk-row");
      row.type = "button";
      row.style.animationDelay = Math.min(i * 22, 400) + "ms";
      row.setAttribute("aria-expanded", state.open === t.issue ? "true" : "false");

      var iss = el("div", "issue");
      iss.appendChild(el("span", "id", t.issue));
      if (t.run_count && t.run_count > 1) {
        var badge = el("span", "runs-badge", t.run_count + " runs");
        badge.title = t.run_count + " runs of this ticket; the row shows the latest";
        iss.appendChild(badge);
      }
      var liveHost = window.DOCKET_HOST || null;
      var liveId = (liveHost && liveHost.live && liveHost.run_id) || null;
      var ghostN = (t.runs || []).filter(function (rr) {
        return rr.outcome === "running" && rr.run !== liveId;
      }).length;
      if (ghostN) {
        var gb = el("span", "runs-badge ghost", ghostN + " unconfirmed");
        gb.title = "attempts recorded as running with no live process "
          + "behind their run ids - diagnostics, never activity";
        iss.appendChild(gb);
      }
      iss.appendChild(el("span", "sum", t.summary || ""));
      row.appendChild(iss);

      var track = el("div", "track");
      (t.gates || []).forEach(function (g) {
        var cell = el("div", "cell");
        var mk = el("span", "mark " + (g.halt ? "halt" : g.result));
        // The state, what the gate found, and - for the three states that
        // found nothing - WHY it found nothing. "skipped" and "never_reached"
        // used to hover as a bare word, which reads as a shrug; the payload
        // records the sentence and this shows it.
        mk.title = g.name + ": " + (g.halt ? "awaiting a human" : g.result) +
          (g.detail ? " - " + g.detail : "") +
          (g.why && g.why !== g.detail ? " - " + g.why : "");
        cell.appendChild(mk);
        track.appendChild(cell);
      });
      row.appendChild(track);

      var disp = el("div", "disposition");
      var outcome = t.outcome || "unknown";
      // THE verdict outranks the run row. A ledger row can still say "running"
      // long after the workflow went READY (the run-13 zombie); run_verdict
      // folds that contradiction once, and every surface repeats the fold
      // rather than each making up its own mind. Where there is no verdict -
      // a legacy per-ticket ledger with no run ids - the recorded outcome is
      // all there is, and it is shown as what it is.
      var vv = verdictView(t);
      var chip = el("span", "verdict " + vv.status, vv.label || "unknown");
      chip.title = vv.state === "recorded"
        ? vv.headline + "  (recorded outcome: " + outcome + ")"
        : "recorded outcome; this ledger folds no verdict for the row";
      disp.appendChild(chip);

      // "clean run" is only true if every gate actually answered. A run that
      // merged with Snyk unreachable and mutmut timed out is not clean, it is
      // unmeasured, and a dashboard that calls it clean is doing the exact
      // thing the hollow marks exist to prevent.
      var mute = (t.gates || []).filter(function (g) { return g.result === "unknown"; });
      var unmeasured = mute.length
        ? mute.length + (mute.length === 1 ? " gate" : " gates") + " unmeasured: " +
          mute.map(function (g) { return g.name; }).join(", ")
        : "";
      var why = t.reason;
      var whyCls = "why";
      if (vv.state === "recorded") {
        // one sentence, the ledger's own, in the place the reader already
        // looks for "why".
        why = vv.headline;
        whyCls = "why verdict-why";
      } else if (!why && outcome === "merged") {
        if (unmeasured) {
          why = "merged with " + unmeasured;
          whyCls = "why unanswered";
        } else {
          why = "every gate answered";
        }
      }
      disp.appendChild(el("span", whyCls, why || ""));
      // How a run ENDED and how much of it was MEASURED are two facts. Giving
      // the verdict the "why" slot must not bury "Snyk never answered" - an
      // unmeasured gate is the one thing a reader must not skim past.
      if (unmeasured && whyCls.indexOf("unanswered") < 0) {
        var um = el("span", "why unanswered unmeasured-note", unmeasured);
        um.title = "these gates ran and could not decide; nothing was proved " +
          "about them either way";
        disp.appendChild(um);
      }
      var cost = el("span", "cost");
      if (t.run_count && t.run_count > 1) {
        // latest run's cost, with the all-attempts total beside it - "this
        // ticket has burned $10 over 22 runs" is the number that matters.
        var latest = money(t.cost_latest);
        var total = money(t.cost_total);
        if (latest === null && total === null) {
          cost.appendChild(unk("no run recorded a cost"));
        } else {
          cost.textContent = (latest || "-");
          // A total is a total only when every attempt was priced. When one
          // was not, `cost_total` is null and this must not print the priced
          // subtotal in the word "total" - it prints a dash and states, on
          // hover, exactly how much of the ticket the money figures cover.
          var priced = t.runs_priced;
          var tot = el("span", "cost-total", " / " + (total || "-") + " total");
          if (total === null && priced != null) {
            var sub = money(t.cost_priced_subtotal);
            tot.title = "no total: only " + priced + " of " + t.run_count +
              " runs recorded a price" +
              (sub ? " (" + sub + " across those " + priced + ")" : "");
          } else {
            tot.title = "total across all " + t.run_count + " runs" +
              (priced != null ? " (" + priced + " of " + t.run_count +
                                " priced)" : "");
          }
          cost.appendChild(tot);
        }
      } else {
        put(cost, money(t.cost_usd), "no cost recorded for this run");
      }
      disp.appendChild(cost);
      row.appendChild(disp);

      row.addEventListener("click", function () {
        state.open = state.open === t.issue ? null : t.issue;
        // Closing a ticket drops its attempt selection - reopening it
        // starts from the latest, never from a stale historical view.
        if (state.open !== t.issue && state.view
            && state.view.issue === t.issue) {
          state.view = null;
        }
        renderWalk(state.payload);
        renderRunsAttempts(state.payload);
      });
      host.appendChild(row);

      if (state.open === t.issue) host.appendChild(detail(t));
    });
  }

  function detail(t) {
    var d = el("div", "detail");

    // V4.4 attempt isolation: the drill-down is scoped to ONE attempt.
    // Default is the latest (whose fields are flattened onto the ticket
    // row); a click in either attempts table selects a historical one,
    // and NOTHING below may then borrow the newest attempt's verdict,
    // gates, timeline or artifacts. The conversation and related-tables
    // blocks stay deliberately ticket-wide and are labeled so.
    var sel = null;
    if (state.view && state.view.issue === t.issue) {
      (t.runs || []).forEach(function (r) {
        if (r.run === state.view.run) sel = r;
      });
    }
    var isLatest = !sel || sel.run === t.run;
    var dv = sel || t;

    // ===== attempt identity - which attempt this drill-down is about ====
    var ab = el("div", "attempt-banner");
    ab.appendChild(el("span", "ab-run", "attempt " + (dv.run || "?")));
    ab.appendChild(el("span", "ab-tag " + (isLatest ? "latest" : "hist"),
      isLatest ? "latest attempt" : "historical attempt"));
    var abv = dv.verdict || {};
    if (abv.workflow_id) {
      var wfBit = el("span", "ab-wf", "workflow " + abv.workflow_id
        + (abv.workflow_state ? " (" + abv.workflow_state + ")" : ""));
      ab.appendChild(wfBit);
      // Resume lineage, DERIVED from the shared workflow - runs of this
      // ticket carrying the same workflow id are one resumable chain.
      var sibs = (t.runs || []).filter(function (r) {
        return r.run !== dv.run
          && (r.verdict || {}).workflow_id === abv.workflow_id;
      }).map(function (r) { return r.run; });
      if (sibs.length) {
        ab.appendChild(el("span", "ab-lineage",
          "resume lineage (derived from the shared workflow): "
          + sibs.join(", ")));
      }
    } else {
      ab.appendChild(el("span", "ab-wf", "workflow: no recorded link"));
    }
    // The attempt's OWN flow report, when it recorded one.
    var flowArt = (dv.artifacts || []).filter(function (a) {
      return a.kind === "report"
        || /flow-[0-9a-f]+\.html$/.test(String(a.rel_path || ""));
    })[0];
    ab.appendChild(el("span", "ab-flow", flowArt
      ? "flow report: " + flowArt.rel_path
      : "no flow report recorded for this attempt"));
    d.appendChild(ab);

    // ===== 0. THE VERDICT - the typed answer, ahead of the prose =====
    d.appendChild(verdictBlock(dv));

    // ===== 1. WHAT HAPPENED - the sentence you read first =====
    // The narrative is written about the ticket's LATEST state; showing it
    // under a historical attempt would put the newest words in an old
    // attempt's mouth.
    if (dv.narrative && isLatest) {
      var narr = el("div", "narrative");
      // The dot sits directly under the verdict block, so it has to agree
      // with it. Colouring it from runs.outcome put a "still running" grey
      // dot under a BLOCKED verdict.
      var nv = verdictView(dv);
      narr.appendChild(el("span", "narr-dot v-" + nv.status, ""));
      narr.appendChild(el("span", "narr-text", dv.narrative));
      d.appendChild(narr);
    }

    // key facts as a compact row of chips
    var facts = el("div", "facts");
    function fact(label, val, cls) {
      if (val === null || val === undefined || val === "") return;
      var f = el("span", "fact" + (cls ? " " + cls : ""));
      f.appendChild(el("span", "fact-l", label));
      f.appendChild(el("span", "fact-v", val));
      facts.appendChild(f);
    }
    fact("iterations", dv.iterations != null ? String(dv.iterations) : null);
    fact("cost", money(dv.cost_usd));
    if (dv.budget_usd) {
      var pctUsed = dv.cost_usd != null ? Math.round((dv.cost_usd / dv.budget_usd) * 100) : null;
      fact("budget", "$" + dv.budget_usd.toFixed(2) + (pctUsed != null ? " (" + pctUsed + "% used)" : ""),
           pctUsed != null && pctUsed > 80 ? "warn" : "");
    }
    fact("tokens", dv.tokens_in != null ? num(dv.tokens_in) + " in / " + (num(dv.tokens_out) || "?") + " out" : null);
    fact("cycle", hours(dv.cycle_hours));
    if (dv.git_sha_start) fact("commit", dv.git_sha_start.slice(0, 8));
    if (dv.pr_url) {
      var prf = el("span", "fact");
      prf.appendChild(el("span", "fact-l", "PR"));
      var a = el("a", "fact-v fact-link", dv.pr_url.replace(/^https?:\/\//, ""));
      a.href = dv.pr_url; a.target = "_blank"; a.rel = "noopener";
      prf.appendChild(a);
      facts.appendChild(prf);
    }
    if (facts.children.length) d.appendChild(facts);

    // ===== 2. THE GATE JOURNEY - a real table, score vs threshold =====
    var ran = (dv.gates || []).filter(function (g) { return g.result !== "never_reached"; });
    if (ran.length) {
      d.appendChild(el("div", "sub-head", "Gate journey"));
      var gt = el("div", "gate-journey");
      var gh = el("div", "gj-row head");
      ["Gate", "Verdict", "Score", "", "Took", "What it found"].forEach(function (h) {
        gh.appendChild(el("span", null, h));
      });
      gt.appendChild(gh);
      (dv.gates || []).forEach(function (g) {
        var row = el("div", "gj-row" + (g.result === "never_reached" ? " dim" : ""));
        row.appendChild(el("span", "gj-name", g.name));

        var verdict = g.halt ? "awaiting human" : g.result;
        row.appendChild(el("span", "gj-verdict v-" + (g.halt ? "halt" : g.result), verdict));

        // score + bar vs threshold
        if (g.score != null && g.threshold != null) {
          row.appendChild(el("span", "gj-score", (Math.round(g.score*100)/100) + " / " + g.threshold));
          var barWrap = el("span", "gj-bar");
          var fill = el("span", "gj-fill" + (g.result === "fail" ? " fail" : ""));
          fill.style.width = Math.min(100, Math.round((g.score / g.threshold) * 100)) + "%";
          barWrap.appendChild(fill);
          // threshold marker
          var mark = el("span", "gj-thresh");
          mark.style.left = "100%";
          barWrap.appendChild(mark);
          row.appendChild(barWrap);
        } else {
          row.appendChild(el("span", "gj-score", g.result === "never_reached" ? "-" : ""));
          row.appendChild(el("span", "gj-bar", ""));
        }

        row.appendChild(el("span", "gj-took",
          g.duration_ms != null ? _ms(g.duration_ms) : (g.result === "never_reached" ? "-" : "")));

        row.appendChild(el("span", "gj-found", _gateFound(g)));
        gt.appendChild(row);
      });
      d.appendChild(gt);
    }

    // ===== 3. WHO DID WHAT - the timeline, legible =====
    // First, pull out the human-facing exchange (Jira Q&A, approvals) and show
    // it as a readable conversation. This spans ALL runs of the ticket, not just
    // the latest - the dialogue with the author is about the ticket, and usually
    // happens on the first comprehension attempt, so showing only the latest
    // run's events would hide the whole conversation.
    var convSource = [];
    (t.runs || [t]).forEach(function (r) {
      (r.timeline || []).forEach(function (e) { convSource.push(e); });
    });
    var convo = convSource.filter(function (e) { return _convoText(e) !== null; });
    convo.sort(function (a, b) { return String(a.at || "").localeCompare(String(b.at || "")); });
    if (convo.length) {
      d.appendChild(el("div", "sub-head", "Conversation & approvals"));
      var cv = el("div", "convo");
      convo.forEach(function (e) {
        var who = (e.actor || "").toLowerCase();
        var isHuman = who === "author" || who === "human" || who === "reviewer" ||
                      /reply|grant|approv/.test((e.kind || "").toLowerCase());
        var row = el("div", "cv-msg" + (isHuman ? " them" : " agent"));
        var head = el("div", "cv-head");
        head.appendChild(el("span", "cv-who", e.actor || "?"));
        head.appendChild(el("span", "cv-kind", _convoKind(e.kind)));
        head.appendChild(el("span", "cv-at", (e.at || "").replace("T", " ").slice(5, 16)));
        row.appendChild(head);
        row.appendChild(el("div", "cv-text", _convoText(e)));
        cv.appendChild(row);
      });
      d.appendChild(cv);
    }

    if (dv.timeline && dv.timeline.length) {
      d.appendChild(el("div", "sub-head", "Who did what - " + dv.timeline.length + " events"
        + (isLatest ? "" : " (this attempt)")));
      var tl = el("div", "timeline");
      dv.timeline.forEach(function (e) {
        var row = el("div", "tl-row");
        row.appendChild(el("span", "tl-at", (e.at || "").replace("T", " ").slice(5, 16)));
        row.appendChild(el("span", "tl-actor", e.actor || "?"));
        var act = el("span", "tl-what");
        var verb = e.kind || "";
        var tgt = e.target ? " " + e.target : "";
        act.textContent = verb + tgt;
        row.appendChild(act);
        row.appendChild(el("span", "tl-model", e.model || ""));
        var tok = el("span", "tl-tok");
        tok.textContent = (e.tokens_in == null) ? "" :
          num(e.tokens_in) + "\u2192" + (num(e.tokens_out) || "?");
        row.appendChild(tok);
        row.appendChild(put(el("span", "tl-cost"), money(e.cost_usd)));
        tl.appendChild(row);
      });
      d.appendChild(tl);
      if (dv.timeline_truncated) {
        d.appendChild(el("div", "tl-more", "+ " + dv.timeline_truncated + " more events (capped)."));
      }
    }

    // ===== 4. ARTIFACTS - what the run produced, grouped by run =====
    // A ticket run 14 times has 14 sets of artifacts. Showing them flat makes
    // 'context' appear 14 times with no way to tell which run made which. Group
    // under each run instead, newest first.
    var runsForArts = sel ? [dv]
      : ((t.runs && t.runs.length > 1) ? t.runs : [t]);
    var anyArts = runsForArts.some(function (r) { return (r.artifacts || []).length; });
    if (anyArts) {
      d.appendChild(el("div", "sub-head", "Artifacts produced"));
      runsForArts.forEach(function (r, idx) {
        var arts = r.artifacts || [];
        if (!arts.length) return;
        if (runsForArts.length > 1) {
          var lbl = el("div", "art-run-label");
          lbl.appendChild(el("span", "art-run-when",
            (r.started || "").replace("T", " ").slice(5, 16)));
          // same rule as everywhere else on this page: the folded verdict is
          // what a run's status IS; runs.outcome is the raw row behind it.
          var av = verdictView(r);
          var astEl = el("span", "art-run-status v-" + av.status,
            av.label || "");
          if (av.state === "recorded") astEl.title = av.headline;
          lbl.appendChild(astEl);
          lbl.appendChild(el("span", "art-run-n", arts.length + " files"));
          d.appendChild(lbl);
        }
        var ar = el("div", "timeline art-group");
        arts.forEach(function (a) {
          var row = el("div", "tl-row art");
          row.appendChild(el("span", "tl-actor", a.kind || "?"));
          // rel_path is documented "relative to workspace_path". A row whose
          // path would climb OUT of the ticket workspace is shown - hiding it
          // would hide the tampering - but it is shown as flagged inert text.
          // Nothing on this page ever turns an artifact path into a link, and
          // this is the row where that would matter most.
          var what = el("span", "tl-what", a.rel_path || "");
          if (a.escapes_workspace) {
            what.className = "tl-what art-unsafe";
            what.title = "this path leaves the ticket workspace " +
              "(development/<release>/<ticket>/). It is shown exactly as the " +
              "ledger recorded it and is deliberately not openable.";
            row.appendChild(what);
            var warn = el("span", "art-unsafe-flag", "outside the workspace");
            row.appendChild(warn);
          } else {
            row.appendChild(what);
          }
          row.appendChild(el("span", "tl-model", a.actor || ""));
          row.appendChild(put(el("span", "tl-cost"), bytes(a.bytes)));
          ar.appendChild(row);
        });
        d.appendChild(ar);
      });
    }

    // ===== 5. related discovered tables (governor, etc.) =====
    var rel = t.related || {};
    Object.keys(rel).sort().forEach(function (name) {
      if (rel[name] && rel[name].length) d.appendChild(relatedBlock(name, rel[name]));
    });

    // ===== 6. all runs of this ticket (if grouped) - at the bottom, it's history =====
    if (t.runs && t.runs.length > 1) {
      d.appendChild(el("div", "sub-head", "All " + t.runs.length + " runs of " + t.issue));
      var rt = el("div", "runs-table");
      var head = el("div", "run-row head");
      ["when", "status", "gate", "why", "cost", "iters"].forEach(function (h) {
        head.appendChild(el("span", null, h));
      });
      rt.appendChild(head);
      t.runs.forEach(function (r) {
        var viewingThis = dv.run === r.run;
        var row = el("div", "run-row"
          + (viewingThis ? " viewing" : ""));
        // V4.4: history is openable - selecting a row scopes the whole
        // drill-down above to THAT attempt, with its own verdict.
        row.setAttribute("role", "button");
        row.tabIndex = 0;
        row.setAttribute("aria-label", "view attempt " + (r.run || "?"));
        row.addEventListener("click", function () {
          openAttempt(t.issue, r.run);
        });
        row.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openAttempt(t.issue, r.run);
          }
        });
        var rv = verdictView(r);
        row.appendChild(el("span", "run-when", (r.started || "").replace("T", " ").slice(5, 16)));
        // Same rule as the collapsed row: the folded verdict outranks the run
        // row, and history is exactly where a stale 'running' misleads most.
        var stEl = el("span", "run-status v-" + rv.status, rv.label || "\u2014");
        stEl.title = rv.state === "recorded"
          ? rv.headline + "  (recorded outcome: " + (r.outcome || "-") + ")"
          : "recorded outcome; no verdict folded for this run";
        row.appendChild(stEl);
        row.appendChild(el("span", "run-gate", r.stopped_at || ""));
        var whyEl = el("span", "run-why",
          rv.state === "recorded" ? rv.headline : (r.reason || ""));
        if (rv.state === "recorded") whyEl.title = rv.headline;
        row.appendChild(whyEl);
        var c = el("span", "run-cost");
        put(c, money(r.cost_usd));
        row.appendChild(c);
        row.appendChild(el("span", "run-iters", r.iterations != null ? String(r.iterations) : "\u2014"));
        rt.appendChild(row);
      });
      d.appendChild(rt);
    }

    return d;
  }

  function _convoText(e) {
    // Return readable text if this event carries human-facing content, else null.
    var raw = e.payload;
    if (raw == null || raw === "") return null;
    if (typeof raw === "string") {
      if (raw.charAt(0) === "{" || raw.charAt(0) === "[") {
        try {
          var o = JSON.parse(raw);
          return o.text || o.message || o.question || o.answer || o.reply ||
                 o.comment || o.body || null;
        } catch (e2) { return raw; }
      }
      return raw;
    }
    if (typeof raw === "object") {
      return raw.text || raw.message || raw.question || raw.answer || raw.reply ||
             raw.comment || raw.body || null;
    }
    return null;
  }

  function _convoKind(kind) {
    var k = (kind || "").toLowerCase();
    if (/question/.test(k)) return "asked";
    if (/reply|answer/.test(k)) return "answered";
    if (/approval_request|ask/.test(k)) return "approval needed";
    if (/grant|approv/.test(k)) return "approved";
    if (/deny|reject/.test(k)) return "denied";
    return kind || "";
  }

  function _ms(ms) {
    if (ms == null) return "";
    if (ms < 1000) return ms + "ms";
    return (ms / 1000).toFixed(1) + "s";
  }

  function _gateFound(g) {
    // turn details_json / detail into a readable phrase
    if (g.result === "never_reached") return "run stopped upstream";
    var raw = g.detail;
    if (raw == null || raw === "") {
      return g.result === "pass" ? "passed" : (g.result === "fail" ? "failed" : "");
    }
    // details_json is often JSON; pull a human field out
    if (typeof raw === "string" && raw.charAt(0) === "{") {
      try {
        var o = JSON.parse(raw);
        return o.reason || o.note || o.detail || o.message ||
               Object.values(o).filter(function (v) { return typeof v === "string"; })[0] || raw;
      } catch (e) { /* fall through */ }
    }
    return String(raw);
  }

  // ---- why runs stop ----------------------------------------------------

  function renderTaxonomy(p) {
    Array.prototype.forEach.call(document.querySelectorAll(".tax"),
      function (h) { taxInto(h, p); });
  }

  function taxInto(host, p) {
    if (!host) return;
    host.textContent = "";
    var rows = p.taxonomy || [];
    if (!rows.length) {
      host.appendChild(el("div", "empty", "Nothing stopped. Either a very good week, or the gates are not running."));
      return;
    }
    rows.forEach(function (r) {
      var row = el("div", "tax-row " + r.outcome);
      row.appendChild(el("div", "count", String(r.count)));
      var b = el("div");
      // Gate and disposition on one line. The earlier draft printed "the gate
      // worked - a human owes an answer" under every halted row, which on a
      // week with four halts said the same sentence four times. Once is
      // information; four times is wallpaper.
      b.appendChild(el("div", "gate", r.gate + " / " +
        (r.outcome === "halted" ? "awaiting a human" : "failed")));
      b.appendChild(el("div", "reason", r.reason));
      row.appendChild(b);
      host.appendChild(row);
    });
  }

  // ---- what Docket found, and how each run ended ------------------------
  //
  // Two payload fields that nothing rendered until now: payload.findings (the
  // finding ledger, the table Docket's primary metric is computed FROM - the
  // counts here are lifecycle states, not a defect count) and each run's
  // payload.tickets[].verdict - run_verdict.py's ONE terminal projection, the
  // same fold the channel summary and the sidebar speak.
  //
  // findingsView / verdictView decide WHAT to say and touch no element; the
  // functions under them only paint it. That split is not decoration: it is
  // what lets report.py --self-test execute these decisions under node with no
  // DOM, so the three findings states are proven rather than grepped for.
  //
  // Neither of them derives a verdict. verdictView copies fields out of the
  // ledger's verdict object and stops - a renderer that decides how a run
  // ended is a second opinion, and the whole point of run_verdict.py is that
  // there is exactly one. findingsView never names a status either: the rows
  // are whatever statuses the ledger used, so a ledger that grows a status
  // renders it on the next build with no edit here.

  // one class per status, so six taxonomy labels are six different things on
  // the page and not six identical grey rows.
  function tally(counts) {
    var by = counts || {};
    return Object.keys(by).sort().map(function (s) {
      return {
        status: s,
        count: by[s],
        cls: "fs-" + String(s).toLowerCase().replace(/[^a-z0-9]+/g, "_")
      };
    });
  }

  function findingsView(findings) {
    if (findings === null || findings === undefined) {
      return { state: "unavailable", message: FINDINGS_UNAVAILABLE,
               rows: [], verdicts: [], confirmed: null, proposed: null };
    }
    var rows = tally(findings.by_status);
    // by_status is the LIFECYCLE (how far a claim got through triage);
    // by_verdict is the TAXONOMY (what the claim is). Two vocabularies over
    // the same rows: they are tallied side by side and never summed, because
    // adding them would count every finding twice.
    var verdicts = tally(findings.by_verdict);
    var conf = findings.confirmed;
    var prop = findings.proposed;
    var any = rows.length || verdicts.length;
    return {
      state: any ? "counts" : "empty",
      message: any ? "" : FINDINGS_EMPTY,
      rows: rows,
      verdicts: verdicts,
      confirmed: conf === undefined ? null : conf,
      proposed: prop === undefined ? null : prop
    };
  }

  // The only word this file translates. "halted" means a gate stopped to ask a
  // human, which is the product working, and "halted" alone does not say that.
  // Every other token reads as itself; none is ever spoken in another's word.
  function statusLabel(token) {
    return token === "halted" ? "awaiting human" : token;
  }

  function verdictView(run) {
    var outcome = (run && run.outcome) || null;
    var v = (run && run.verdict) || null;
    if (!v || !v.headline) {
      return {
        state: "unavailable", message: VERDICT_UNAVAILABLE, headline: null,
        run_state: null, display_state: null, reason: null,
        workflow_state: null, flags: [],
        status: outcome || "unknown",
        label: outcome ? statusLabel(outcome) : null
      };
    }
    function flag(label, value) {
      // true / false / not recorded are three answers, not two.
      return { label: label,
               value: (value === true || value === false) ? value : null };
    }
    // The word and the colour come from run_state - the verdict's OWN
    // vocabulary, which keeps blocked, failed and halted apart. display_state
    // exists to give the runs row a four-word vocabulary and pays for that by
    // folding all three of those into "halted" (run_verdict.display_state), so
    // choosing a label from it paints a harness death as "awaiting human" -
    // invariant 8 run backwards, and it collapses three of the six things the
    // Runs tab has to keep distinguishable. display_state is still shown, in
    // the verdict block, labelled as what it is; it is simply never what a
    // status word or a colour is chosen from.
    var token = v.state || v.display_state || outcome || "unknown";
    return {
      state: "recorded",
      message: "",
      headline: v.headline,                       // verbatim. never rewritten.
      run_state: v.state || null,
      display_state: v.display_state || null,
      reason: v.reason || null,
      workflow_state: v.workflow_state || null,
      status: token,
      label: statusLabel(token),
      flags: [flag("succeeded", v.is_success), flag("terminal", v.is_terminal),
              flag("needs a human", v.needs_human),
              flag("resumable", v.resumable)]
    };
  }

  function renderFindings(p) {
    Array.prototype.forEach.call(document.querySelectorAll(".findings"),
      function (h) { findingsInto(h, p); });
  }

  function findingsInto(host, p) {
    if (!host) return;
    host.textContent = "";
    var view = findingsView(p ? p.findings : null);
    if (view.state !== "counts") {
      // Deliberately NOT hidden by data-needs: "we never measured this" is a
      // thing the reader has to be told, and a section that quietly vanishes
      // tells them nothing.
      host.appendChild(el("div", "empty findings-" + view.state, view.message));
      return;
    }

    var head = el("div", "f-head");
    var conf = el("div", "f-hero");
    conf.appendChild(el("div", "f-hero-l", "confirmed"));
    var cv = el("div", "f-hero-n");
    put(cv, view.confirmed === null ? null : num(view.confirmed),
        "this ledger did not record a confirmed count");
    conf.appendChild(cv);
    head.appendChild(conf);
    var prop = el("div", "f-hero");
    prop.appendChild(el("div", "f-hero-l", "proposed"));
    var pv = el("div", "f-hero-n");
    put(pv, view.proposed === null ? null : num(view.proposed),
        "this ledger did not record a proposed count");
    prop.appendChild(pv);
    head.appendChild(prop);
    host.appendChild(head);

    function paint(rows, label) {
      if (!rows.length) return;   // no rows is no claim; say nothing
      if (label) host.appendChild(el("div", "f-group", label));
      rows.forEach(function (r) {
        var row = el("div", "f-row " + r.cls);
        row.appendChild(el("div", "count", String(r.count)));
        row.appendChild(el("div", "f-status", r.status));
        host.appendChild(row);
      });
    }
    // Labelled, because the two tallies count the SAME findings under two
    // vocabularies. Unlabelled and stacked they would read as one list of
    // twelve findings where there are six.
    paint(view.rows, view.verdicts.length ? "lifecycle" : "");
    paint(view.verdicts, "taxonomy verdict");
  }

  // The run's terminal verdict, as its own block at the top of the drill-down.
  // The narrative underneath still reads as prose; this is the typed answer.
  function verdictBlock(t) {
    var v = verdictView(t);
    var box = el("div", "verdict-block vb-" +
      (v.state === "recorded" ? (v.run_state || "unknown") : "none"));
    if (v.state !== "recorded") {
      box.appendChild(el("div", "vb-head", v.message));
      box.appendChild(el("div", "vb-note",
        "This row carries no run id, so there is nothing for run_verdict to " +
        "fold. The recorded outcome below is the raw ledger fact and is not " +
        "a verdict."));
      return box;
    }
    box.appendChild(el("div", "vb-head", v.headline));
    var meta = el("div", "vb-meta");
    function bit(label, value) {
      if (value === null || value === undefined || value === "") return;
      var s = el("span", "vb-bit");
      s.appendChild(el("span", "vb-l", label));
      s.appendChild(el("span", "vb-v", String(value)));
      meta.appendChild(s);
    }
    bit("state", v.run_state);
    bit("runs row reads", v.display_state);
    bit("workflow", v.workflow_state);
    bit("because", v.reason);
    v.flags.forEach(function (f) {
      var s = el("span", "vb-bit vb-flag" +
        (f.value === true ? " on" : f.value === false ? " off" : " unk"));
      s.appendChild(el("span", "vb-l", f.label));
      if (f.value === null) s.appendChild(unk("not recorded in this verdict"));
      else s.appendChild(el("span", "vb-v", f.value ? "yes" : "no"));
      meta.appendChild(s);
    });
    box.appendChild(meta);
    return box;
  }

  // ---- gate ledger ------------------------------------------------------

  // V4.4: the pure roster seam - type/activity counts, the nine filters,
  // search and the sorts, over every actor the payload returns. Types and
  // capability booleans come from the roster (agent_info), never inferred
  // from whether historical model rows happen to exist.
  function agentsModel(p, f) {
    f = f || {};
    var ags = (p && p.agents) || [];
    var counts = { total: ags.length, activity: 0, zero: 0,
                   unclassified: 0, model: 0, hybrid: 0, det: 0,
                   human: 0, system: 0 };
    function active(a) { return a.calls != null && a.calls > 0; }
    ags.forEach(function (a) {
      if (active(a)) counts.activity += 1;
      if (a.does && !active(a)) counts.zero += 1;
      if (a.type === "unclassified" || !a.does) counts.unclassified += 1;
      if (a.type === "model") counts.model += 1;
      else if (a.type === "hybrid") counts.hybrid += 1;
      else if (a.type === "deterministic") counts.det += 1;
      else if (a.type === "human") counts.human += 1;
      else if (a.type === "system") counts.system += 1;
    });
    var filt = f.filter || "all";
    var rows = ags.filter(function (a) {
      if (filt === "all") return true;
      if (filt === "model") return a.type === "model";
      if (filt === "hybrid") return a.type === "hybrid";
      if (filt === "det") return a.type === "deterministic";
      if (filt === "system") return a.type === "system";
      if (filt === "human") return a.type === "human";
      if (filt === "active") return active(a);
      if (filt === "unused") return !!a.does && !active(a);
      if (filt === "unclassified") {
        return a.type === "unclassified" || !a.does;
      }
      return true;
    });
    var q = String(f.q || "").toLowerCase();
    if (q) {
      rows = rows.filter(function (a) {
        return (String(a.role || "") + " " + String(a.title || "") + " "
                + String(a.does || "")).toLowerCase().indexOf(q) >= 0;
      });
    }
    var sorts = {
      activity: function (x, y) { return (y.calls || 0) - (x.calls || 0); },
      tin: function (x, y) {
        return (y.tokens_in || 0) - (x.tokens_in || 0);
      },
      tout: function (x, y) {
        return (y.tokens_out || 0) - (x.tokens_out || 0);
      },
      name: function (x, y) { return x.role.localeCompare(y.role); },
    };
    if (sorts[f.sort]) rows = rows.slice().sort(sorts[f.sort]);
    return { rows: rows, counts: counts };
  }

  function renderAgentsStats(p) {
    var host = document.querySelector(".agents-stats");
    if (!host) return;
    host.textContent = "";
    if (!p.agents || !p.agents.length) return;
    var c = agentsModel(p, {}).counts;
    var grid = el("div", "agents-stat-grid");
    [["roles returned", c.total],
     ["has recorded activity", c.activity],
     ["configured, zero activity", c.zero],
     ["unclassified ledger actors", c.unclassified],
     ["model-backed", c.model],
     ["hybrid (model + deterministic)", c.hybrid],
     ["deterministic", c.det],
     ["human", c.human]].forEach(function (s) {
      var box = el("div", "astat");
      box.appendChild(el("span", "astat-v", String(s[1])));
      box.appendChild(el("span", "astat-l", s[0]));
      grid.appendChild(box);
    });
    host.appendChild(grid);
    host.appendChild(el("div", "legend",
      "system machinery: " + c.system + " actors. Counts are ledger "
      + "facts in scope; a measured zero is 0."));
  }

  var agentsCountEl = null;

  function currentAgentFilters() {
    return { filter: state.agentF, q: state.agentQ,
             sort: state.agentSort };
  }

  function updateAgentsCount(p) {
    if (!agentsCountEl || !p) return;
    var m = agentsModel(p, currentAgentFilters());
    agentsCountEl.textContent = "showing " + m.rows.length + " of "
      + ((p.agents || []).length);
  }

  function renderAgentsBar(p) {
    var host = document.querySelector(".agents-bar");
    if (!host) return;
    host.classList.add("tux-bar");
    host.textContent = "";
    host.appendChild(el("span", "tux-level", "filters - counts are roster agents"));
    if (!p.agents || !p.agents.length) return;
    var q = el("input", "tux-q agents-q");
    q.type = "search";
    q.value = state.agentQ || "";
    q.placeholder = "search role, title, description";
    q.setAttribute("aria-label", "Search agents");
    q.addEventListener("input", function () {
      state.agentQ = q.value;
      updateAgentsCount(state.payload);
      renderAgentRoster(state.payload);
    });
    host.appendChild(q);
    [["all", "All"], ["model", "Model-backed"], ["hybrid", "Hybrid"],
     ["det", "Deterministic"], ["system", "System"], ["human", "Human"],
     ["active", "Has activity"], ["unused", "Configured, unused"],
     ["unclassified", "Unclassified"]].forEach(function (x) {
      var chip = el("button", "chip", x[1]);
      chip.dataset.afilter = x[0];
      chip.setAttribute("aria-pressed",
        state.agentF === x[0] ? "true" : "false");
      chip.addEventListener("click", function () {
        state.agentF = x[0];
        renderAgentsBar(state.payload);
        renderAgentRoster(state.payload);
      });
      host.appendChild(chip);
    });
    var sort = el("select", "percall-sel agents-sort");
    sort.setAttribute("aria-label", "Sort agents");
    [["pipeline", "Pipeline order"], ["activity", "Activity"],
     ["tin", "Tokens in"], ["tout", "Tokens out"],
     ["name", "Name"]].forEach(function (x) {
      var o = el("option", null, x[1]);
      o.value = x[0];
      if (state.agentSort === x[0]) o.selected = true;
      sort.appendChild(o);
    });
    sort.addEventListener("change", function () {
      state.agentSort = sort.value;
      renderAgentRoster(state.payload);
    });
    host.appendChild(sort);
    [["cards", "Cards"], ["table", "Table"]].forEach(function (x) {
      var btn = el("button", "chip", x[1]);
      btn.dataset.amode = x[0];
      btn.setAttribute("aria-pressed",
        state.agentMode === x[0] ? "true" : "false");
      btn.addEventListener("click", function () {
        state.agentMode = x[0];
        renderAgentsBar(state.payload);
        renderAgentRoster(state.payload);
      });
      host.appendChild(btn);
    });
    agentsCountEl = el("span", "percall-count");
    host.appendChild(agentsCountEl);
    updateAgentsCount(p);
  }

  function renderAgentRoster(p) {
    var host = document.querySelector(".agent-grid");
    if (!host) return;
    host.textContent = "";
    var m = agentsModel(p, currentAgentFilters());
    if (state.agentMode === "table") {
      renderAgentTable(host, m.rows);
      return;
    }
    m.rows.forEach(function (a) {
      var card = el("div", "agent-card panel");
      var head = el("div", "agent-head");
      var role = el("span", "agent-role", a.title || a.role);
      role.appendChild(el("span", "agent-raw", " " + a.role));
      head.appendChild(role);
      var typ = el("span", "agent-stage", a.type || "unclassified");
      typ.title = "roster type: " + (a.type || "unclassified");
      head.appendChild(typ);
      card.appendChild(head);

      var tags = el("div", "agent-tags");
      if (a.stage) tags.appendChild(el("span", "gate-optin",
        "stage: " + a.stage));
      if (a.does && !(a.calls != null && a.calls > 0)) {
        tags.appendChild(el("span", "gate-optin",
          "configured, zero recorded activity"));
      }
      if (a.type === "unclassified" || !a.does) {
        var un = el("span", "gate-optin", "unclassified ledger actor");
        un.title = "an actor string found in ledger events with no "
          + "roster entry - historical or ad-hoc";
        tags.appendChild(un);
      }
      if (tags.kids ? tags.kids.length : tags.childNodes.length) {
        card.appendChild(tags);
      }

      if (a.does) {
        card.appendChild(el("div", "agent-does", a.does));
      } else {
        card.appendChild(el("div", "agent-does agent-undesc",
          "In the ledger as '" + a.role + "' - no description on file. Add it to AGENT_INFO."));
      }

      var io = el("div", "agent-io");
      var rd = el("div", "aio");
      rd.appendChild(el("span", "aio-l", "reads"));
      rd.appendChild(put(el("span", "aio-v"), a.reads, "not on file"));
      io.appendChild(rd);
      var wr = el("div", "aio");
      wr.appendChild(el("span", "aio-l", "writes"));
      wr.appendChild(put(el("span", "aio-v"), a.writes, "not on file"));
      io.appendChild(wr);
      card.appendChild(io);

      // live stats from the ledger - measured zeros render 0.
      var stats = el("div", "agent-stats");
      function stat(label, val, unkMsg) {
        var s = el("div", "astat");
        s.appendChild(put(el("span", "astat-v"), val, unkMsg));
        s.appendChild(el("span", "astat-l", label));
        stats.appendChild(s);
      }
      stat("recorded events", num(a.calls),
        "events not counted for this actor");
      stat("failed", num(a.failed_calls), "failed calls not counted");
      stat("duration",
        a.duration_ms == null ? null
          : Math.round(a.duration_ms / 1000) + "s",
        "duration not recorded");
      stat("tokens in", num(a.tokens_in), "no tokens recorded");
      stat("tokens out", num(a.tokens_out), "no tokens recorded");
      stat("cost", money(a.cost_usd), "no cost recorded for this agent");
      card.appendChild(stats);

      // capability pills - roster booleans, stated as capabilities.
      var caps = el("div", "agent-models");
      if (a.human) caps.appendChild(el("span", "amodel", "human"));
      if (a.uses_model) {
        var um = el("span", "amodel", "uses model");
        um.title = "this role can invoke the model transport";
        caps.appendChild(um);
      }
      if (a.deterministic_tools) {
        var dt = el("span", "amodel", "deterministic tools");
        dt.title = "this role drives deterministic engines/tools";
        caps.appendChild(dt);
      }
      if (a.orchestration) {
        var oc = el("span", "amodel", "orchestration");
        oc.title = "system/orchestration machinery";
        caps.appendChild(oc);
      }
      if (caps.kids ? caps.kids.length : caps.childNodes.length) {
        card.appendChild(caps);
      }

      // effective vs requested models, separated honestly.
      var mo = el("div", "agent-models");
      if (a.models && a.models.length) {
        a.models.forEach(function (mm) {
          var t = el("span", "amodel", mm);
          t.title = "effective model recorded on this actor's events";
          mo.appendChild(t);
        });
      } else {
        var none = el("span", "amodel");
        none.appendChild(unk("no effective model recorded on this "
          + "actor's events"));
        mo.appendChild(none);
      }
      var req = el("span", "amodel");
      if (a.models_requested && a.models_requested.length) {
        req.textContent = "requested: " + a.models_requested.join(", ");
        req.title = "requested model, as the transport recorded it";
      } else {
        req.textContent = "requested: not recorded";
        req.title = "requested model not recorded on these rows - "
          + "older events carry only the effective id or the role";
      }
      mo.appendChild(req);
      card.appendChild(mo);
      host.appendChild(card);
    });
  }

  function renderAgentTable(host, rows) {
    var wrap = el("div", "gate-scroll");
    var table = el("table", "grid");
    table.appendChild(el("caption", "srx", "Agent roster"));
    var thead = el("thead");
    var hr = el("tr");
    ["Actor (raw)", "Type", "Title", "Stage", "Recorded events",
     "Failed", "Duration", "Tok in", "Tok out", "Cost",
     "Effective models", "Requested"].forEach(function (h) {
      hr.appendChild(el("th", null, h));
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (a) {
      var tr = el("tr");
      tr.appendChild(el("td", null, a.role));
      tr.appendChild(el("td", "txt", a.type || "unclassified"));
      tr.appendChild(put(el("td", "txt"), a.title, "no title on file"));
      tr.appendChild(put(el("td", "txt"), a.stage, "no stage on file"));
      tr.appendChild(put(el("td"), num(a.calls),
        "events not counted for this actor"));
      tr.appendChild(put(el("td"), num(a.failed_calls),
        "failed calls not counted"));
      tr.appendChild(put(el("td"),
        a.duration_ms == null ? null
          : Math.round(a.duration_ms / 1000) + "s",
        "duration not recorded"));
      tr.appendChild(put(el("td"), num(a.tokens_in),
        "no tokens recorded"));
      tr.appendChild(put(el("td"), num(a.tokens_out),
        "no tokens recorded"));
      tr.appendChild(put(el("td"), money(a.cost_usd),
        "no cost recorded for this agent"));
      tr.appendChild(put(el("td", "txt"),
        (a.models && a.models.length) ? a.models.join(", ") : null,
        "no effective model recorded on this actor's events"));
      tr.appendChild(put(el("td", "txt"),
        (a.models_requested && a.models_requested.length)
          ? a.models_requested.join(", ") : null,
        "requested model not recorded on these rows"));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    host.appendChild(wrap);
  }


  // ==================================================================
  // V4.4: the desktop-approved SUBWAY architecture. Everything on this
  // tab derives from the ONE TOPOLOGY object below - code-owned
  // constants, never ledger text - so the string assembly used here is
  // injection-safe by construction. Payload-carrying tabs stay DOM-
  // built. The tab is fully static apart from the recorded-gates strip,
  // so it builds once and redraws only on its own interactions - a
  // payload poll never stomps the player.
  // ==================================================================
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }
  var archState = { sel: null, layer: "all", zoom: 1, focus: false,
                    panX: 0, panY: 0, fullscreen: false, userZoomed: false,
                    reduceMotion: false, payload: null };
  try {
    if (window.matchMedia
        && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      archState.reduceMotion = true;
    }
  } catch (e0) { /* host without matchMedia */ }

  function archAnnounce(t) {
    if (typeof document === "undefined" || !document.getElementById) return;
    var host = document.getElementById("arch-live");
    if (host) host.textContent = t;
  }
  function redrawArch() {
    // the wired host survives even when a test harness has swapped the
    // global document out between the render and the interaction
    var host = archState.host
      || ((typeof document !== "undefined" && document.querySelector)
          ? document.querySelector(".arch") : null);
    if (!host) return;
    host.innerHTML = pageArchitecture()
      + '<div class="srx" aria-live="polite" id="arch-live"></div>';
    archSpawnPulses();
  }

var TOPOLOGY = {
  stages: [
    { id:"comprehension", label:"Comprehension", source_key:"comprehension", gate:"comprehension", gate_optional:false, actor:"spec", note:"deterministic Jira preflight runs before any model call" },
    { id:"blast_radius", label:"Blast Radius", source_key:"blast_radius", gate:null, gate_optional:false, actor:"lead", note:"a first-class stage that records NO gate row (scripts/governor.py:66); fused with plan on the low-risk fast path" },
    { id:"plan", label:"Plan", source_key:"plan", gate:"plan_approval", gate_optional:true, actor:"planner", note:"gate exists only when gates.plan_approval.enabled (default false); approval = a human deletes the DRAFT marker" },
    { id:"test_spec", label:"Test Spec / Frozen Tests", source_key:"frozen_tests", gate:"frozen_tests", gate_optional:false, actor:"test-spec", note:"member-chain static validation before any test executes" },
    { id:"develop", label:"Develop", source_key:"develop", gate:"unit_tests", gate_optional:false, actor:"developer", note:"the stage name and its gate name differ: develop records unit_tests" },
    { id:"blind_review", label:"Blind Review", source_key:"blind_review", gate:"blind_review", gate_optional:false, actor:"reviewer" },
    { id:"security", label:"Security", source_key:"security_snyk", gate:"security_snyk", gate_optional:false, actor:"security", concurrent_with:"blind_review", note:"runs concurrently with Blind Review when governor.parallel_review_security is on (loop.py ThreadPoolExecutor, both stage.started before either gate row)" },
    { id:"qa", label:"QA", source_key:"qa_e2e", gate:"qa_e2e", gate_optional:false, actor:"qa" },
    { id:"mutation", label:"Mutation", source_key:"mutation", gate:"mutation", gate_optional:false, actor:"mutation" }
  ],
  vocab: {
    run_outcomes: ["merged","completed","escalated","abandoned","running","failed"],
    workflow_states: ["RECEIVED","QUALIFYING","PLANNING","IMPLEMENTING","VALIDATING","REPAIRING","REVIEWING","READY","COMPLETED","BLOCKED","CANCELLED"],
    gate_outcomes: ["pass","fail","unknown","skipped"],
    gate_outcome_note: "never_reached is a renderer projection of an ABSENT row (payload_builder.py:24), never a stored value; unknown and skipped both require an unknown_reason",
    ui_states: ["running","complete","stopped","halted","live (wire events this session: lastSeq != 0)","orphaned (recorded running with lastSeq 0)"],
    vocab_note: "four independent vocabularies: runs.outcome (ledger.RUN_OUTCOMES), workflow delivery state (workflow.STATES), gate outcome (ledger + schema CHECK), and the Run Monitor's live UI projection. They are never merged."
  },
  cardinality: [
    { rel:"ticket -> many workflows", detail:"workflows.ticket_id; a fresh run mints wf-<ticket>-<hex8> with lineage to the superseded journey (workflow.py:412)" },
    { rel:"workflow -> many runs (resumes attach, never restart)", detail:"mission blackboard runs[] (mission_control.py:228); attempt number = len(runs)+1" },
    { rel:"workflow -> many delivery-state transitions", detail:"workflow_transitions, append-only with reason (workflow.py:299)" },
    { rel:"run -> many stage events", detail:"events, the append-only spine; seq on the wire IS events.event_id (loop.py:3643)" },
    { rel:"run -> many gate rows (last row per gate wins)", detail:"gates, superseding rows never updates (schema.sql:139); each gate row cites its event" },
    { rel:"run -> many model calls", detail:"metered by model_authority per (stage, actor, phase, attempt) (model_authority.py:207)" },
    { rel:"run -> many artifacts", detail:"artifacts UNIQUE(run_id, rel_path) with sha256 - the proof resume-carry relies on (schema.sql:266)" },
    { rel:"failure -> zero or many repair attempts", detail:"repair_attempts.failure_id; budget counted per FINGERPRINT: 3 per failure, 6 per workflow (workflow.py:151)" },
    { rel:"finding -> evidence + optional supersession", detail:"findings.evidence_json/evidence_sha; supersedes is a self-reference; dedupe on (ticket_id, evidence_sha)" },
    { rel:"workflow READY -> manual Ship -> COMPLETED", detail:"scripts/ship.py:86 is the only READY->COMPLETED transition; end_run('merged') precedes it" }
  ],
  loops: [
    { name:"Frozen-test regeneration", trigger:"frozen suite fails with class test_harness_defect", model:true, bound:"3 per fingerprint; 4 model calls per run (FAST_STAGE_CALL_BUDGET)", detail:"repair_controller.converge strategy regenerate-frozen-suite; only defective tests regenerate; deterministic on-disk recheck; requirement_ambiguity halts for a human instead" },
    { name:"Develop retry ladder", trigger:"a task fails its tests", model:true, bound:"developer.max_retries (1) + risk extra; plan-dispute replan exactly once; then cohesive-replan under the controller", detail:"retries hand context to the debugger agent; non-conversion overwrites the raw pass with a superseding unit_tests FAIL row" },
    { name:"Blind-review repair", trigger:"reviewer verdict request_changes", model:true, bound:"controller budget (3 per fingerprint, 6 per workflow)", detail:"debugger repair round, then rechecks unit + acceptance + fresh blind review; flip-flop reviews reclassify as requirement_ambiguity (human)" },
    { name:"QA repair", trigger:"acceptance suite fails", model:true, bound:"controller budget; progress ratchet keeps strictly-better trees", detail:"frozen-oracle-defect preflight can forbid code repair entirely (a harness-failure verdict, zero model spend); rechecks unit + acceptance + post-repair review; non-convergence hands off to the deterministic qa-convergence finalizer (superseding qa_e2e FAIL, workflow BLOCKED)" },
    { name:"Mutation strengthen", trigger:"survivors with a green baseline", model:true, bound:"controller budget; catcher tests only - writes refused outside test paths", detail:"unit_tester writes catcher tests; deterministic full mutation re-run is the single recheck; a dry well burns budget and ends BLOCKED" },
    { name:"No-op detection and rollback", trigger:"a repair round changes nothing, or a recheck goes red", model:false, bound:"two consecutive no-op rounds -> BLOCKED", detail:"checkpointer verify_matches detects the unchanged tree; red rechecks roll back to the last checkpoint; QA keeps strictly-improved trees (progress ratchet)" },
    { name:"Budget exhaustion", trigger:"3 attempts per fingerprint or 6 per workflow spent", model:false, bound:"refusals returned as data, never exceptions", detail:"start_repair refuses (not_retryable / failure_budget_exhausted / workflow_budget_exhausted / recheck_unavailable) -> workflow BLOCKED" },
    { name:"Typed stops", trigger:"provider / tool / protocol / budget failure", model:false, bound:"fail-closed, recorded before surfacing", detail:"ToolInfrastructureFailure, ResponseContractViolation, BudgetExceeded (predictive or reached), EnvelopeExceeded, TransportError - each lands a typed docket.call_failure.v1 event and a truthful outcome; unreached stages stay never-reached" },
    { name:"Stop / cancel", trigger:"the human stops the run", model:false, bound:"terminal for the run, never for the journey", detail:"run outcome abandoned (human_override); the workflow parks BLOCKED so Resume stays possible - CANCELLED only when a fresh run supersedes a never-started workflow" },
    { name:"Resume", trigger:"the human authorizes another attempt", model:false, bound:"same workflow, same worktree", detail:"passed stages carry ONLY with proof: gate row + artifact sha256 + prompt-contract stamp + git-provable checkpoint completeness; review/security/qa carry only if unit_tests carried" },
    { name:"Resume refusal", trigger:"ticket drift or worktree divergence", model:false, bound:"refused before any model call", detail:"ticket sha mismatch, tree not matching the last checkpoint, or unverifiable checkpoint state each refuse with the reason" }
  ],
  concurrency: [
    { id:"speculative_baseline", label:"Speculative baseline vs test-spec", knob:"none - always attempted on the eligible path", default_on:true,
      after:"plan", after_label:"Plan agreed (plan_approval when enabled)", stages:["test_spec"],
      participants:["test_spec_agent","baseline_suite"],
      join:"developer", join_label:"Develop initializes from the verified baseline",
      join_note:"develop joins the future BEFORE init_pristine - the same red-tree-never-baptized order as the sequential path",
      conditions:["skipped under governor.parallel_dev - the lead path never consumes the future","skipped when a resume already carries a unit_tests pass"],
      fallback:"an unusable or hung future (abandonable 930s join) means develop runs the ordinary baseline suite itself - in order, exactly as the non-speculative path",
      channels:"test-spec chats on the model transport; the baseline is a deterministic pytest subprocess with zero model calls",
      source:"loop.py:5168-5185 (SPD-4); scripts/developer.py:1158-1174" },
    { id:"parallel_planners", label:"Parallel planner bake-off", knob:"governor.parallel_planners", default_on:false,
      after:"blast_radius", after_label:"Blast radius declared - the bake-off fans out", stages:["plan"],
      participants:["planner","second_plan"],
      join:"judge", join_label:"the blind judge waits for ALL planners - it must",
      conditions:["engages only when the bake-off fans out to more than one role"],
      fallback:"sequential planner turns in role order - deterministic reply routing was the historical reason this ships off",
      channels:"each planner rides its own role and model; the transport routes replies by id; [pN] prefixes keep the interleaved log readable",
      source:"loop.py:2187-2199; scripts/governor.py:306-310" },
    { id:"scope_plan_fused", label:"Fused scope+plan fast path", knob:"deterministic low-risk classifier + governor.fast_path mode", default_on:null,
      engaged_by:"prefetch.low_risk_candidate - a zero-model-call classifier over the deterministic prefetch; governor.fast_path=never forces it off; a pending plan-change-request.md disqualifies it, unoverridable by any mode",
      after:"comprehension", after_label:"Low-risk ticket (deterministic classifier, zero model calls)", stages:["blast_radius","plan"],
      participants:["lead","planner"],
      join:"scope_plan", join_label:"ONE fused model call (scope_plan) produces blast radius AND plan; the plan stage records itself without a second spend",
      join_note:"a fusion, not a fork: the two roles collapse into one turn with a hard call budget",
      conditions:["the classifier scores the ticket low-risk from the prefetch","no pending plan-change-request.md"],
      fallback:"a declined fused turn stops with a typed complexity escalation - never a silent expansion to the slow path; the ordinary lead-then-planner path is chosen up front when the ticket is not low-risk",
      channels:"one model call under a hard budget - the batched look and the one correction draw from the same pot",
      source:"loop.py:1558-1594, 4884-4916; agents/scope_plan.md" },
    { id:"parallel_dev", label:"Sliced development (lead developer)", knob:"governor.parallel_dev", default_on:false,
      after:"test_spec", after_label:"Frozen tests ready - a big splittable plan", stages:["develop"],
      participants:["lead_developer","developer"],
      join:"developer", join_label:"the lead joins the slices; the unit_tests gate measures the joined tree",
      conditions:["a big splittable ticket - the lead partitions the plan into independent slices and runs a worker per slice, coaching failures itself","side effect: the speculative baseline is skipped - the lead path never consumes the future"],
      fallback:"a single-slice plan falls straight back to the plain developer - small tickets never pay the lead overhead",
      channels:"one worker per slice; slice checkpoint discipline owned by the lead",
      source:"loop.py:5513-5519, 5173; scripts/governor.py:298-299; scripts/lead_developer.py" },
    { id:"parallel_qa", label:"Sharded QA (lead QA)", knob:"governor.parallel_qa", default_on:false,
      after:"security", after_label:"Security clean - a big frozen regression suite", stages:["qa"],
      participants:["lead_qa","qa_agent"],
      join:"qa_agent", join_label:"the lead joins the shards into one qa_e2e verdict",
      conditions:["a big regression suite - the frozen tests shard into independent groups, one worker per shard, inadequate mock data coached, real code gaps reported","side effect: governor.parallel_post_develop declines while this is on - lead-qa sharding falls back to the sequential post-develop path"],
      fallback:"a single shard falls back to the plain QA run",
      channels:"one worker per shard",
      source:"loop.py:6284-6291; scripts/governor.py:302-303; scripts/lead_qa.py" },
    { id:"parallel_review_security", label:"Concurrent review + security", knob:"governor.parallel_review_security", default_on:false,
      after:"develop", after_label:"Develop passed (unit_tests) - both read the SAME frozen diff", stages:["blind_review","security"],
      participants:["reviewer","security_agent"],
      join:"qa", join_label:"QA proceeds only after BOTH verdicts join; Mutation stays sequential after QA",
      mutation_position:"sequential after the join - QA gates on both verdicts, then mutation runs exactly as in the sequential path",
      conditions:["blind_review and security_snyk gates both enabled","develop passed and the review is not already carried by resume","governor.parallel_post_develop takes precedence when its own guards hold"],
      fallback:"the sequential path: blind review, then the security scan (SPD-3 - the saving is min of review and security, and a clean scanner costs no model call at all)",
      channels:"each thread runs inside its OWN immutable call attribution ([rev]/[sec] channel prefixes); neither depends on the other verdict",
      source:"loop.py:5970-6012 (SPD-3); scripts/governor.py:313-317" },
    { id:"parallel_post_develop", label:"Concurrent review + security + QA", knob:"governor.parallel_post_develop", default_on:false,
      after:"develop", after_label:"Develop passed (unit_tests) - all three read the develop-complete tree", stages:["blind_review","security","qa"],
      participants:["reviewer","security_agent","qa_agent"],
      join:"mutation", join_label:"the verdict JOINS all three; the repair loops run on the joined results; Mutation stays sequential after the join",
      mutation_position:"ALWAYS sequential after the join, by design: mutation makes zero model calls (no latency to hide), it would contend with QA subprocess runs for CPU, and a speculative run would record gate evidence for a stage a sequential run may never reach",
      conditions:["off by default; engages only on the guarded clean path","blind_review, security_snyk and qa_e2e gates all enabled","nothing resumed for security or QA; no budget halt at the review checkpoint","not under governor.parallel_qa - lead-qa sharding falls back to the sequential path"],
      fallback:"anything special - a resume carry, a disabled gate, lead-qa sharding, a budget halt - falls back to the sequential path",
      supersede:"if the review repair touches the tree, the concurrent QA result is SUPERSEDED (its gate row stands, append-only) and QA re-runs on the repaired tree",
      channels:"review session / stateless scanner / qa session - three distinct channels; none can see another conversation; each thread runs inside its OWN immutable call attribution",
      source:"loop.py:5914-5967, 6256-6264 (R13); scripts/governor.py:320-330" }
  ],
  nodes: [
    { id:"human", label:"Human / operator", kind:"human", files:["(you)"], responsibility:"Answers clarifying questions, ratifies context, approves plans (optional gate), inspects BLOCKED evidence, authorizes Resume, and ships READY work - delivery is deliberately manual." },
    { id:"vscode_ui", label:"VS Code commands + UI", kind:"ui", files:["extension/extension.js","extension/package.json"], responsibility:"29 palette commands; registers surfaces; renders and relays - never decides a gate." },
    { id:"run_monitor_ui", label:"Run Monitor surfaces", kind:"ui", files:["extension/src/run_sidebar.js","extension/src/run_status.js","extension/src/run_flow.js","extension/src/diagnostics.js","extension/src/test_results.js"], responsibility:"Sidebar, status bar, Run Flow tab, Problems panel and Test Explorer - all pure projections of the RunEventStore; exactly four notification moments." },
    { id:"dashboard", label:"Dashboard projection", kind:"ui", files:["report.py","serve.py","extension/src/docket_webview.js","payload_builder.py"], responsibility:"Read-only ledger projection on three hosts; payload_builder is its only SQLite reader." },
    { id:"gateway", label:"gateway.js stdio relay", kind:"deterministic", files:["extension/src/gateway.js","extension/src/models.js"], responsibility:"Spawns python loop.py --stdio; answers ONLY chat / models / capabilities; resolves roles via vscode.lm; forwards events and progress unchanged. Contains no pipeline logic - knows nothing about tickets, agents or gates." },
    { id:"vscode_lm", label:"vscode.lm (Copilot)", kind:"system", files:["(VS Code API)"], responsibility:"THE PRIMARY model transport: selectChatModels + sendRequest; fresh message list every call; token counts from countTokens or null - never 0." },
    { id:"headless", label:"headless_gateway.py", kind:"system", files:["headless_gateway.py"], responsibility:"OPTIONAL terminal-only alternative answering the same stdio protocol via the claude CLI; loop.py cannot tell the two apart. Also the ONE evidence redaction authority (_redact)." },
    { id:"loop", label:"loop.py control plane", kind:"deterministic", files:["loop.py"], responsibility:"Owns the whole nine-stage pipeline, transport-agnostic; deterministic scoring and enforcement; persist-before-emit event protocol; typed stop handling; resume proofs." },
    { id:"mission_control", label:"Mission control / workflow kernel", kind:"deterministic", files:["mission_control.py","workflow.py"], responsibility:"The ONE adapter between loop.py and the delivery-state machine: stage eligibility, forward-fill advancement, block/complete, evidence-gated READY, repair budgets." },
    { id:"repair_controller", label:"Central repair controller", kind:"deterministic", files:["repair_controller.py"], responsibility:"converge(): bounded repair attempts with a growing union of required rechecks; two no-op rounds block; refusals are data, never exceptions." },
    { id:"governor", label:"Governor - pipeline state machine and policy knobs", kind:"deterministic", files:["scripts/governor.py"], responsibility:"The pipeline as data (8 gate/stage/requires triples), policy profiles, required gates, budgets, concurrency switches. NOT an allow/ask/deny arbiter and NOT the blast-radius enforcer - no governor_decisions table exists in production." },
    { id:"blast_enforcer", label:"Blast-radius enforcement", kind:"deterministic", files:["scripts/blast_radius.py"], responsibility:"check_edit(radius, path) -> allow/deny with reason - the PreToolUse hook; must_not_touch denies first; never a warning." },
    { id:"containment", label:"Process containment", kind:"deterministic", files:["containment.py"], responsibility:"run_contained(): exec allowlist (python/python3/pytest), env filtering, path scope, audit trail; untrusted projects refuse before any model call." },
    { id:"checkpointer", label:"Checkpointer (shadow git)", kind:"deterministic", files:["checkpointer.py","rollback.py"], responsibility:"Pristine commit #0 plus per-task radius-scoped checkpoints with permanent tags; rollback + verify_matches power no-op detection, the progress ratchet and resume proofs." },
    { id:"member_chain", label:"Member-chain validator", kind:"deterministic", files:["member_chain.py"], responsibility:"Pure-AST test-oracle validation: receiver/member chains checked against the project's real API before any test executes; convergent correction prompts; semantic fingerprints." },
    { id:"model_authority", label:"Model authority (budget meter)", kind:"deterministic", files:["model_authority.py"], responsibility:"Wraps every transport: immutable per-call attribution (stage, actor, phase, attempt), cache-weighted recorded tokens, predictive BudgetExceeded BEFORE the call." },
    { id:"transport", label:"Transport seam", kind:"deterministic", files:["transport.py"], responsibility:"JSON-lines over stdio (no socket, no port); roles - worker / judge / second_plan / cheap - resolve at the far end, never in agent files." },
    { id:"jira", label:"Jira / ticket-file ingress", kind:"deterministic", files:["scripts/jira_fetch.py","scripts/jira_results.py"], responsibility:"Fetches description, ACs (customfield), comments and clarifications; posts questions back; 4xx never retries; field values never leave Jira." },
    { id:"worktree", label:"Per-workflow worktree", kind:"storage", files:["workflow_workspace.py"], responsibility:"Isolated git worktree per workflow id, cut from the project HEAD; creation failure refuses the run - never a silent fallback to the shared tree." },
    { id:"ledger", label:"ledger.db (append-only)", kind:"storage", files:["ledger.py","schema.sql"], responsibility:"Runs, events, gates, artifacts, workflows, failures, repairs, findings, learnings; corrections are superseding rows, never updates." },
    { id:"artifacts", label:"Evidence on disk", kind:"storage", files:["development/<release>/<ticket>/"], responsibility:"Plans, frozen tests, reports, run logs, questions.json - the ledger records paths + sha256; content stays in the workspace." },
    { id:"run_event_store", label:"RunEventStore", kind:"deterministic", files:["extension/src/run_events.js"], responsibility:"docket.event.v1 consumer: seq IS the ledger event_id, prev_seq is the one ordering authority, duplicates drop, gaps resync once via loop.py --status-json." },
    { id:"system_resume", label:"System / resume actors", kind:"system", files:["loop.py (resume_run)","extension/src/resume.js"], responsibility:"Carried gates land in the new run as pass rows with actor=resume and the carried proof; orphaned runs re-attach only by a NEW spawn of the same workflow." },
    { id:"spec", label:"Spec (comprehension)", kind:"model", files:["agents/spec.md"], responsibility:"Scores ticket comprehension after the free deterministic preflight; classifies blocking / investigation / prerequisite; drafts clarifying questions for the author." },
    { id:"cartographer", label:"Cartographer", kind:"hybrid", files:["agents/cartographer.md","scripts/cartographer.py"], responsibility:"Tool-using repo mapper (grep / list / read) on a step budget - a model driving deterministic tools." },
    { id:"drafter", label:"Context drafter", kind:"model", files:["agents/context_drafter.md","context_drafter.py"], responsibility:"Drafts the project context document; a human must ratify it before it feeds comprehension." },
    { id:"lead", label:"Lead (blast radius)", kind:"model", files:["agents/lead.md"], responsibility:"Declares the blast radius and constraints from the repo map and prefetch; boundaries are then filesystem-verified and enforced deterministically." },
    { id:"scope_plan", label:"Scope+plan (fast path)", kind:"model", files:["agents/scope_plan.md"], responsibility:"The fused low-risk turn: blast radius and plan in ONE model call; the plan stage then records itself without a second spend." },
    { id:"planner", label:"Planner", kind:"model", files:["agents/planner.md"], responsibility:"Writes the implementation plan inside the declared radius; optional fan-out produces competing plans for the blind bake-off." },
    { id:"second_plan", label:"Second planner", kind:"model", files:["agents/planner.md (role second_plan)"], responsibility:"An independent plan from a different model role so the judge has a real choice." },
    { id:"judge", label:"Blind judge", kind:"model", files:["agents/judge.md"], responsibility:"Scores competing plans without knowing which agent produced them." },
    { id:"test_spec_agent", label:"Test-spec (frozen tests)", kind:"model", files:["agents/test-spec.md","scripts/test_spec.py"], responsibility:"Writes the frozen acceptance suite: one compact generation turn, at most one targeted correction (2-call budget, 4 per run); member-chain validation gates every test before freeze." },
    { id:"baseline_suite", label:"Speculative baseline suite", kind:"deterministic", files:["loop.py:5168 (SPD-4)","scripts/developer.py:1158"], responsibility:"The pre-change unit suite as a deterministic pytest subprocess (zero model calls), started speculatively in a single-worker thread the moment the plan is agreed - it boots while test-spec chats. Develop joins the future BEFORE init_pristine so a red tree is never baptized as the baseline; an unusable or hung future (abandonable 930s join) falls back to an ordinary baseline run. Skipped under parallel_dev and when a resume already carries unit_tests pass." },
    { id:"developer", label:"Developer", kind:"model", files:["agents/developer.md","scripts/developer.py"], responsibility:"Implements the plan task by task against the frozen tests inside the radius; checkpoints each green task; ~90% of run tokens." },
    { id:"lead_developer", label:"Lead developer", kind:"model", files:["agents/lead-developer.md","scripts/lead_developer.py"], responsibility:"Slice orchestration when parallel_dev splits the work; owns the slice checkpoint discipline." },
    { id:"debugger", label:"Debugger", kind:"model", files:["agents/debugger.md"], responsibility:"Takes over failed tasks and repair rounds with the failure context - retries never just repeat the developer prompt." },
    { id:"unit_tester", label:"Unit tester", kind:"model", files:["agents/unit_tester.md"], responsibility:"Writes catcher tests for mutation survivors (and coverage-tool tests); forbidden from touching non-test paths." },
    { id:"reviewer", label:"Blind reviewer", kind:"model", files:["agents/reviewer.md","scripts/reviewer.py"], responsibility:"Reviews the diff blind; request_changes routes findings to the central repair controller; re-reviews after repairs." },
    { id:"security_agent", label:"Security", kind:"hybrid", files:["agents/security.md","scripts/security.py"], responsibility:"Drives the deterministic scan AND can invoke tx.chat for triage; findings are owner=human and never auto-repaired (workflow.py:131)." },
    { id:"qa_agent", label:"QA", kind:"model", files:["agents/qa.md","scripts/qa.py"], responsibility:"Runs the acceptance suite end-to-end and writes qa_e2e; failures go to the central repair controller with an oracle-defect preflight." },
    { id:"lead_qa", label:"Lead QA", kind:"model", files:["agents/lead-qa.md","scripts/lead_qa.py"], responsibility:"Slice QA orchestration when parallel_qa is on; verified disputes feed the oracle-defect proof." },
    { id:"qa_convergence", label:"QA convergence finalizer", kind:"deterministic", files:["loop.py:6613 (qa repair non-convergence)"], responsibility:"Deterministic ledger finalizer - not a model agent: no prompt file, no model call, no roster entry. When the QA repair loop fails to converge it writes the superseding qa_e2e FAIL row (actor qa-convergence, superseding true, raw_suite_outcome preserved) so the gate's last word matches the stop; the workflow parks BLOCKED for a human." },
    { id:"mutation_engine", label:"Mutation", kind:"hybrid", files:["mutation.py","agents/mutation.md"], responsibility:"AST-based Python mutation engine (deterministic) whose strengthen loop can invoke tx.chat - survivors are EVIDENCE, never a verdict." },
    { id:"retro", label:"Retro", kind:"model", files:["agents/retro.md","scripts/retro.py"], responsibility:"Reads the run digest after every finished run (skipped on refusal outcomes); a deterministic friction pre-gate means zero model calls on a quiet run; proposes learnings, never edits context." }
  ],
  edges: [
    { from:"human", to:"vscode_ui", label:"Run Ticket / Run with Overrides / palette commands", layers:["product","human"] },
    { from:"vscode_ui", to:"gateway", label:"command invokes runLoop", layers:["product"] },
    { from:"gateway", to:"loop", label:"spawn python -u loop.py --stdio (one JSON object per line)", layers:["product"] },
    { from:"jira", to:"loop", label:"ticket text, ACs, comments, clarifications", layers:["product","evidence"] },
    { from:"jira", to:"spec", label:"ticket + acceptance criteria + prior clarifications", layers:["agents"] },
    { from:"loop", to:"model_authority", label:"every chat request is metered and attributed", layers:["product","enforce"] },
    { from:"model_authority", to:"transport", label:"budget-cleared chat call by ROLE", layers:["product"] },
    { from:"transport", to:"gateway", label:"chat request over stdio - answered, never accumulated", layers:["product"] },
    { from:"transport", to:"headless", label:"optional alternative: same protocol via the claude CLI", layers:["product"] },
    { from:"gateway", to:"vscode_lm", label:"vscode.lm sendRequest - the PRIMARY model supply", layers:["product"] },
    { from:"transport", to:"loop", label:"provider / tool / protocol / budget failure -> truthful typed stop (docket.call_failure.v1)", layers:["repair","evidence"] },
    { from:"loop", to:"worktree", label:"cut per-workflow isolated worktree from project HEAD; fresh run -> NEW workflow id + worktree", layers:["product","enforce"] },
    { from:"loop", to:"ledger", label:"persist-before-emit: runs, events, gates, artifacts", layers:["product","evidence"] },
    { from:"loop", to:"artifacts", label:"plans, frozen tests, reports, run logs, questions.json", layers:["evidence"] },
    { from:"loop", to:"run_event_store", label:"docket.event.v1 - seq IS the ledger event_id, prev_seq chains", layers:["product","evidence"] },
    { from:"run_event_store", to:"run_monitor_ui", label:"pure projection: sidebar, status bar, flow tab, problems, test explorer", layers:["product","evidence"] },
    { from:"ledger", to:"dashboard", label:"payload_builder read-only projection (this page)", layers:["product","evidence"] },
    { from:"loop", to:"mission_control", label:"stage eligibility before, forward-fill advancement after", layers:["enforce"] },
    { from:"mission_control", to:"ledger", label:"workflow states, transitions, failures, repair attempts", layers:["enforce","evidence"] },
    { from:"governor", to:"loop", label:"pipeline-as-data, required gates, budgets, concurrency switches (parallel_planners, parallel_dev, parallel_review_security, parallel_post_develop, parallel_qa; fast_path mode)", layers:["enforce"] },
    { from:"cartographer", to:"lead", label:"repo map, patterns, prefetch", layers:["agents"] },
    { from:"cartographer", to:"spec", label:"repo map for comprehension context", layers:["agents"] },
    { from:"drafter", to:"spec", label:"ratified project context", layers:["agents"] },
    { from:"human", to:"drafter", label:"context ratification - the draft needs a human yes", layers:["human"] },
    { from:"lead", to:"blast_enforcer", label:"declared radius becomes the enforced boundary (filesystem-verified)", layers:["agents","enforce"] },
    { from:"lead", to:"planner", label:"blast radius + constraints + repo context", layers:["agents"] },
    { from:"scope_plan", to:"loop", label:"fused scope+plan turn on the low-risk fast path", layers:["agents"] },
    { from:"planner", to:"judge", label:"candidate plan (authorship hidden)", layers:["agents"] },
    { from:"second_plan", to:"judge", label:"independent second plan (blind bake-off)", layers:["agents"] },
    { from:"judge", to:"loop", label:"winning plan selection with scores", layers:["agents"] },
    { from:"planner", to:"human", label:"optional plan approval: DRAFT marker awaits a human edit", layers:["human"] },
    { from:"human", to:"loop", label:"approve plan (delete the DRAFT line -> plan_approval pass, actor=human)", layers:["human"] },
    { from:"test_spec_agent", to:"member_chain", label:"every generated test statically validated before freeze", layers:["agents","enforce"] },
    { from:"member_chain", to:"test_spec_agent", label:"convergent correction prompt for the ONE defective test", layers:["repair"] },
    { from:"test_spec_agent", to:"developer", label:"frozen acceptance tests + plan + radius", layers:["agents"] },
    { from:"developer", to:"checkpointer", label:"per-task checkpoints after green tests", layers:["agents","enforce"] },
    { from:"developer", to:"debugger", label:"failed task retries hand the failure context to the debugger", layers:["repair"] },
    { from:"developer", to:"reviewer", label:"diff for blind review", layers:["agents"] },
    { from:"developer", to:"security_agent", label:"diff for the deterministic security scan", layers:["agents"] },
    { from:"blast_enforcer", to:"developer", label:"check_edit allows or denies every edit - never a warning", layers:["enforce"] },
    { from:"developer", to:"containment", label:"test and exec runs contained (allowlist, env filter, path scope)", layers:["enforce"] },
    { from:"qa_agent", to:"containment", label:"acceptance suite runs contained", layers:["enforce"] },
    { from:"mutation_engine", to:"containment", label:"mutant runs contained", layers:["enforce"] },
    { from:"reviewer", to:"repair_controller", label:"request_changes findings (one defect = one fingerprint)", layers:["agents","repair"] },
    { from:"qa_agent", to:"repair_controller", label:"acceptance failures (after the oracle-defect preflight)", layers:["agents","repair"] },
    { from:"repair_controller", to:"debugger", label:"bounded repair round - the debugger edits, radius-scoped", layers:["repair"] },
    { from:"repair_controller", to:"developer", label:"cohesive replan of the remaining work (develop convergence)", layers:["repair"] },
    { from:"repair_controller", to:"test_spec_agent", label:"regenerate-frozen-suite: only the defective tests regenerate", layers:["repair"] },
    { from:"repair_controller", to:"checkpointer", label:"no-op round or red recheck -> rollback; two no-op rounds -> BLOCKED; QA progress ratchet keeps strictly-better trees", layers:["repair","enforce"] },
    { from:"repair_controller", to:"mission_control", label:"start_repair budgets (3 per fingerprint, 6 per workflow); exhaustion -> BLOCKED", layers:["repair","enforce"] },
    { from:"mutation_engine", to:"unit_tester", label:"survivors -> strengthen catcher tests", layers:["agents","repair"] },
    { from:"unit_tester", to:"mutation_engine", label:"strengthened tests -> full deterministic mutation recheck", layers:["repair"] },
    { from:"lead_developer", to:"developer", label:"slice orchestration (parallel_dev)", layers:["agents"] },
    { from:"lead_qa", to:"qa_agent", label:"slice QA + verified disputes (parallel_qa)", layers:["agents"] },
    { from:"repair_controller", to:"qa_convergence", label:"QA repair did not converge (rounds exhausted, no-op, or rechecks never went green) - hand the non-convergence to the deterministic finalizer", layers:["repair"] },
    { from:"qa_convergence", to:"ledger", label:"superseding qa_e2e FAIL row (actor qa-convergence, raw_suite_outcome preserved) - the gate's last word matches the stop; the workflow parks BLOCKED", layers:["repair","evidence"] },
    { from:"security_agent", to:"human", label:"security findings are owner=human - never auto-repaired", layers:["human"] },
    { from:"spec", to:"human", label:"clarifying questions (questions.json + Jira comment) - the gate doing its job", layers:["human"] },
    { from:"human", to:"jira", label:"answers arrive as Jira comments or context/clarifications.md", layers:["human"] },
    { from:"run_monitor_ui", to:"human", label:"the four notification moments: completed / stopped-at-gate / plan ready / questions", layers:["human","product"] },
    { from:"human", to:"loop", label:"Resume Run -> same workflow + worktree; passed stages carried only with proof (gate row + artifact sha256 + prompt contract + checkpoint completeness); ticket drift or tree divergence -> refusal", layers:["human","repair"] },
    { from:"human", to:"loop", label:"Fresh run -> new workflow + isolated worktree; a mid-flight predecessor parks BLOCKED (superseded)", layers:["human"] },
    { from:"human", to:"loop", label:"Stop Run -> run abandoned (human_override), workflow parks BLOCKED; CANCELLED only when fresh supersedes a never-started workflow", layers:["human"] },
    { from:"human", to:"ledger", label:"Ship Run (manual): branch + commit + PR body, run -> merged, workflow READY -> COMPLETED (scripts/ship.py)", layers:["human","product"] },
    { from:"ledger", to:"retro", label:"run digest: gates, escalations, questions, danger zones", layers:["agents","evidence"] },
    { from:"retro", to:"ledger", label:"proposed learnings, each citing its ledger event - never a context edit", layers:["evidence"] },
    { from:"system_resume", to:"ledger", label:"carried gates land as pass rows with actor=resume and the carried proof", layers:["evidence"] },
    { from:"checkpointer", to:"worktree", label:"shadow-git commits scoped to the radius; rollback restores exactly", layers:["enforce","evidence"] },
    { from:"loop", to:"baseline_suite", label:"SPD-4: at plan agreement, start the baseline unit suite speculatively (skipped under parallel_dev and when resume carries unit_tests)", layers:["product","enforce"] },
    { from:"baseline_suite", to:"developer", label:"develop joins the speculative future BEFORE init_pristine (abandonable 930s join); an unusable future means develop runs the ordinary baseline itself", layers:["product","enforce"] },
    { from:"repair_controller", to:"reviewer", label:"recheck after a review repair: unit + frozen acceptance re-run, then a FRESH blind re-review of the repaired diff (superseding gate row each round)", layers:["repair"] },
    { from:"repair_controller", to:"qa_agent", label:"a repair that touches the tree supersedes applicable concurrent evidence: the pre-repair concurrent QA row stands (append-only) and QA re-runs on the repaired tree", layers:["repair","evidence"] },
    { from:"repair_controller", to:"human", label:"exhausted or no-op repair - a typed stop: the workflow parks BLOCKED with the refusal reason as data and a human decides", layers:["repair","human"] },
    { from:"mission_control", to:"human", label:"evidence-gated READY: only after every required gate - joined or sequential - reaches an acceptable terminal state; Ship stays manual", layers:["human","enforce"] }
  ]
};

/* ---------------- ARCHITECTURE: the desktop-approved transit map ------- */
/* every view renders from TOPOLOGY - the one explicit data structure; the
   subway is the ONE visual presentation (the old network chart is gone) */
function archLayerLabel(l){
  return { all:"Show all", product:"Product flow", agents:"Agent communication",
    enforce:"Deterministic enforcement", repair:"Repair loops", human:"Human decisions",
    evidence:"Evidence and data flow" }[l] || l;
}
function edgeLanes(e){
  var L = e.layers||[], out=[];
  if (L.indexOf("human")>=0) out.push("human");
  if (L.indexOf("repair")>=0) out.push("repair");
  if (L.indexOf("evidence")>=0) out.push("evidence");
  if (L.indexOf("enforce")>=0) out.push("enforce");
  if (!out.length) out.push("flow");
  return out;
}
function edgeLane(e){ return edgeLanes(e)[0]; }
function wrapLabel(label, maxChars){
  var words = label.split(" "), lines=[], cur="";
  words.forEach(function(w){
    if (cur && (cur+" "+w).length > maxChars){ lines.push(cur); cur=w; }
    else cur = cur ? cur+" "+w : w;
  });
  if (cur) lines.push(cur);
  return lines;
}
function orthPath(pts, r){
  r = r || 16;
  if (pts.length < 2) return "";
  var d = "M "+pts[0][0]+" "+pts[0][1];
  for (var k=1; k<pts.length-1; k++){
    var p=pts[k], a=pts[k-1], b=pts[k+1];
    var v1=[p[0]-a[0], p[1]-a[1]], v2=[b[0]-p[0], b[1]-p[1]];
    var l1=Math.max(1,Math.abs(v1[0])+Math.abs(v1[1])), l2=Math.max(1,Math.abs(v2[0])+Math.abs(v2[1]));
    var r1=Math.min(r,l1/2), r2=Math.min(r,l2/2);
    var pa=[p[0]-v1[0]/l1*r1, p[1]-v1[1]/l1*r1], pb=[p[0]+v2[0]/l2*r2, p[1]+v2[1]/l2*r2];
    d += " L "+pa[0]+" "+pa[1]+" Q "+p[0]+" "+p[1]+" "+pb[0]+" "+pb[1];
  }
  d += " L "+pts[pts.length-1][0]+" "+pts[pts.length-1][1];
  return d;
}
function archMarkerDefs(){
  var lanes = [["flow","var(--ink)"],["enforce","var(--ink-mute)"],["repair","var(--carmine)"],["human","var(--ultra)"],["evidence","var(--ink-faint)"]];
  return "<defs>"+lanes.map(function(l){
    return '<marker id="aarr-'+l[0]+'" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
      + '<path d="M 0 1 L 9 5 L 0 9 z" fill="'+l[1]+'"></path></marker>';
  }).join("")+"</defs>";
}
/* [x, y, label side a/b/l/r] - sides chosen so no label box touches another
   station's label or marker; stationLabelBoxes() computes the SAME boxes
   for the renderer AND the collision check (qa_probe_v4_4 D4) */
var ST_C = {
  human:[80,70,"b"],
  vscode_ui:[160,150,"a"], gateway:[300,150,"a"], vscode_lm:[460,150,"a"],
  transport:[300,250,"l"], headless:[460,250,"r"], model_authority:[300,340,"l"],
  drafter:[560,220,"r"], jira:[560,310,"r"], cartographer:[744,310,"a"],
  governor:[160,370,"l"], mission_control:[180,500,"l"], system_resume:[180,580,"l"],
  loop:[300,430,"b"], spec:[560,430,"b"], lead:[700,430,"b"], planner:[840,430,"b"],
  judge:[980,430,"b"], test_spec_agent:[1120,430,"b"], developer:[1280,430,"b"],
  reviewer:[1440,430,"b"], qa_agent:[1600,430,"b"], mutation_engine:[1760,430,"b"],
  scope_plan:[840,180,"a"], second_plan:[860,310,"a"], baseline_suite:[1150,310,"a"],
  lead_developer:[1280,310,"a"], lead_qa:[1600,310,"a"],
  security_agent:[1440,540,"b"], member_chain:[1120,540,"b"],
  unit_tester:[1835,525,"b"],
  checkpointer:[1060,730,"b"], debugger:[1200,730,"b"], repair_controller:[1360,730,"b"],
  qa_convergence:[1540,730,"b"],
  blast_enforcer:[1130,845,"b"], containment:[1290,845,"b"],
  run_monitor_ui:[240,950,"b"], run_event_store:[420,950,"b"], worktree:[700,950,"b"],
  ledger:[1080,950,"b"], artifacts:[1260,950,"b"], retro:[1440,950,"b"], dashboard:[900,1010,"b"]
};
var STATION_GLYPH = { model:"M", deterministic:"D", hybrid:"Y", human:"H", system:"S", storage:"B", ui:"U" };
var HUBS_C = ["loop","ledger","developer","repair_controller"];
function stationGeom(n){
  var cfg = ST_C[n.id];
  if (!cfg) return null;
  var x=cfg[0], y=cfg[1], side=cfg[2]||"b";
  var r = HUBS_C.indexOf(n.id)>=0 ? 11 : 8;
  var lines = wrapLabel(n.label, 20);
  var wmax = 0; lines.forEach(function(l){ if(l.length>wmax) wmax=l.length; });
  var w = wmax*6.3+4, h = lines.length*11.5+3, box;
  if (side==="b") box = { x0:x-w/2, y0:y+r+6, x1:x+w/2, y1:y+r+6+h };
  else if (side==="a") box = { x0:x-w/2, y0:y-r-6-h, x1:x+w/2, y1:y-r-6 };
  else if (side==="l") box = { x0:x-r-8-w, y0:y-h/2, x1:x-r-8, y1:y+h/2 };
  else box = { x0:x+r+8, y0:y-h/2, x1:x+r+8+w, y1:y+h/2 };
  return { x:x, y:y, r:r, side:side, lines:lines, box:box };
}
function stationLabelBoxes(){
  var out = [];
  TOPOLOGY.nodes.forEach(function(n){
    var g = stationGeom(n);
    if (!g) return;
    out.push({ id:n.id, kind:"marker", x0:g.x-g.r-2, y0:g.y-g.r-2, x1:g.x+g.r+2, y1:g.y+g.r+2 });
    out.push({ id:n.id, kind:"label", x0:g.box.x0, y0:g.box.y0, x1:g.box.x1, y1:g.box.y1 });
  });
  TERMS_C.forEach(function(tm){
    var lbl = tm.lbl || tm.key;
    var w = lbl.length*6.3+4;
    out.push({ id:"term-"+tm.key, kind:"marker", x0:tm.x-11, y0:tm.y-11, x1:tm.x+11, y1:tm.y+11 });
    out.push({ id:"term-"+tm.key, kind:"label", x0:tm.x-w/2, y0:tm.y+15, x1:tm.x+w/2, y1:tm.y+29.5 });
  });
  return out;
}
var ROUTES_C = [
  { id:"main", lane:"flow", cls:"", lbl:"main line - the nine-stage route", lblAt:[330,412],
    stations:["loop","spec","lead","planner","judge","test_spec_agent","developer","reviewer","qa_agent","mutation_engine"],
    pts:[[300,430],[1900,430]] },
  { id:"supply", lane:"flow", cls:"thin", lbl:"model supply line", lblAt:[100,138],
    stations:["human","vscode_ui","gateway","vscode_lm","loop"],
    pts:[[80,70],[80,120],[110,150],[460,150]] },
  { id:"chain", lane:"flow", cls:"thin", lbl:"model supply chain", lblAt:[314,300],
    stations:["gateway","transport","model_authority","loop"],
    pts:[[300,150],[300,430]] },
  { id:"headspur", lane:"flow", cls:"thin", lbl:"", stations:["transport","headless"],
    pts:[[300,250],[460,250]] },
  { id:"ingress", lane:"flow", cls:"thin", lbl:"ticket ingress", lblAt:[572,392],
    stations:["jira","spec","drafter"],
    pts:[[560,150],[560,430]] },
  { id:"carto", lane:"flow", cls:"thin", lbl:"", stations:["cartographer","lead"],
    pts:[[744,320],[744,430]] },
  { id:"carto2", lane:"flow", cls:"thin", lbl:"", stations:["cartographer","spec"],
    pts:[[736,318],[580,420]] },
  { id:"control", lane:"enforce", cls:"thin", lbl:"", stations:["governor","loop"],
    pts:[[160,370],[240,402],[296,426]] },
  { id:"control2", lane:"enforce", cls:"thin", lbl:"policy + delivery state", lblAt:[30,336],
    stations:["mission_control","system_resume","loop"],
    pts:[[180,580],[180,500],[242,463],[296,434]] },
  { id:"bakeoff", lane:"flow", cls:"thin", lbl:"bake-off branch", lblAt:[955,300],
    stations:["lead","second_plan","judge"],
    pts:[[740,430],[790,310],[930,310],[980,430]] },
  { id:"express", lane:"flow", cls:"thin express", lbl:"fused scope+plan express", lblAt:[900,168],
    stations:["spec","scope_plan","test_spec_agent"],
    pts:[[600,430],[660,180],[1020,180],[1080,430]] },
  { id:"baseline", lane:"flow", cls:"thin", lbl:"speculative baseline branch", lblAt:[1246,352],
    stations:["baseline_suite","developer"],
    pts:[[1020,430],[1070,310],[1230,310],[1240,430]] },
  { id:"devspur", lane:"flow", cls:"thin", lbl:"", stations:["developer","lead_developer","debugger"],
    pts:[[1255,430],[1255,310],[1305,310],[1305,430]] },
  { id:"qaspur", lane:"flow", cls:"thin", lbl:"", stations:["qa_agent","lead_qa"],
    pts:[[1575,430],[1575,310],[1625,310],[1625,430]] },
  { id:"sectrack", lane:"flow", cls:"thin", lbl:"concurrent verification track", lblAt:[1490,528],
    stations:["developer","security_agent","qa_agent"],
    pts:[[1330,430],[1380,540],[1500,540],[1550,430]] },
  { id:"pdtrack", lane:"flow", cls:"thin express", lbl:"R13 track - QA rides the parallel section", lblAt:[1400,626],
    stations:["developer","qa_agent","mutation_engine"],
    pts:[[1340,430],[1400,610],[1650,610],[1700,430]] },
  { id:"chainspur", lane:"enforce", cls:"thin", lbl:"", stations:["test_spec_agent","member_chain"],
    pts:[[1120,430],[1120,540]] },
  { id:"repairline", lane:"repair", cls:"", lbl:"", stations:["checkpointer","debugger","repair_controller","qa_convergence","developer","reviewer","qa_agent","human","mission_control"],
    pts:[[1060,730],[1700,730]] },
  { id:"reprise1", lane:"repair", cls:"thin", lbl:"", stations:["reviewer","repair_controller"],
    pts:[[1425,450],[1425,680],[1390,724]] },
  { id:"reprise2", lane:"repair", cls:"thin", lbl:"", stations:["qa_agent","repair_controller"],
    pts:[[1585,450],[1585,660],[1500,724]] },
  { id:"reprise3", lane:"repair", cls:"thin", lbl:"", stations:["developer","debugger","checkpointer"],
    pts:[[1265,450],[1265,690],[1215,726]] },
  { id:"strengthen", lane:"repair", cls:"thin", lbl:"mutation strengthen loop", lblAt:[1750,588],
    stations:["mutation_engine","unit_tester","containment"],
    pts:[[1760,445],[1805,525],[1865,525],[1892,462],[1790,436]] },
  { id:"humanline", lane:"human", cls:"", lbl:"human decision line", lblAt:[100,58],
    stations:["human","spec","planner","loop","mission_control","repair_controller","jira","drafter","run_monitor_ui","security_agent","ledger"],
    pts:[[80,70],[2010,70]] },
  { id:"hdrop1", lane:"human", cls:"thin", lbl:"questions", lblAt:[572,96], stations:["spec","human","jira","drafter"],
    pts:[[560,70],[560,150]] },
  { id:"hdrop2", lane:"human", cls:"thin", lbl:"plan approval", lblAt:[892,96], stations:["planner","human"],
    pts:[[880,70],[880,430]] },
  { id:"hdrop3", lane:"human", cls:"thin", lbl:"stop / resume / fresh", lblAt:[252,96], stations:["loop","human"],
    pts:[[240,70],[240,400],[288,424]] },
  { id:"hdrop4", lane:"human", cls:"thin", lbl:"inspect BLOCKED", lblAt:[1752,96], stations:["repair_controller","human","mission_control"],
    pts:[[1740,70],[1740,690],[1712,722]] },
  { id:"hdrop5", lane:"human", cls:"thin", lbl:"Ship", lblAt:[1912,96], stations:["human","ledger"],
    pts:[[1900,70],[1900,415]] },
  { id:"stopstub", lane:"human", cls:"thin", lbl:"", stations:["loop"],
    pts:[[305,445],[360,525]] },
  { id:"evline", lane:"evidence", cls:"", lbl:"evidence + projection line", lblAt:[250,938],
    stations:["run_monitor_ui","run_event_store","worktree","ledger","artifacts","retro","dashboard","loop","checkpointer","system_resume","qa_convergence","mission_control"],
    pts:[[240,950],[1440,950]] },
  { id:"evrise1", lane:"evidence", cls:"thin", lbl:"persist before emit", lblAt:[312,880],
    stations:["loop","ledger","run_event_store"],
    pts:[[300,450],[300,946]] },
  { id:"evrise2", lane:"evidence", cls:"thin", lbl:"", stations:["checkpointer","worktree"],
    pts:[[1060,742],[1060,880],[740,950]] },
  { id:"evspur", lane:"evidence", cls:"thin", lbl:"", stations:["ledger","dashboard"],
    pts:[[1080,955],[1010,1010],[900,1010]] },
  { id:"enfline", lane:"enforce", cls:"thin", lbl:"containment + radius", lblAt:[1140,822],
    stations:["checkpointer","blast_enforcer","containment","developer","qa_agent","mutation_engine","lead"],
    pts:[[1060,742],[1130,845],[1290,845]] }
];
var FORKS_C = {
  speculative_baseline: { f:[1020,430], j:[1232,430], lbl:[1020,414] },
  parallel_planners:    { f:[740,430], j:[950,430], lbl:[740,414] },
  scope_plan_fused:     { f:[600,430], j:[1080,430], lbl:[600,414] },
  parallel_dev:         { f:[1255,430], j:[1305,430], lbl:[1247,404] },
  parallel_qa:          { f:[1575,430], j:[1625,430], lbl:[1567,404] },
  parallel_review_security: { f:[1330,430], j:[1550,430], lbl:[1330,414] },
  parallel_post_develop:    { f:[1360,520], j:[1700,430], lbl:[1372,516] }
};
var TERMS_C = [
  { key:"READY", from:"workflow_states", x:1900, y:430, note:"evidence-gated; waits for the manual Ship" },
  { key:"COMPLETED", from:"workflow_states", x:2010, y:70, lbl:"SHIPPED (COMPLETED)", note:"reached only through scripts/ship.py after the run ends merged" },
  { key:"BLOCKED", from:"workflow_states", x:1700, y:730, note:"parked with evidence; Resume stays possible" },
  { key:"abandoned", from:"run_outcomes", x:360, y:540, lbl:"ABANDONED (run)", note:"Stop Run ends the RUN abandoned; the journey parks BLOCKED" }
];
function forkJoinSvg(gid, fx, fy, jx, jy, lbl){
  var joinCls = "join" + (archAnim.join===gid ? " join-flash" : "");
  var lblSvg = "";
  if (lbl && lbl.length===2 && typeof lbl[0]==="number"){
    lblSvg = '<text class="fkj-lbl" x="'+lbl[0]+'" y="'+lbl[1]+'" text-anchor="start">'+esc(gid)+"</text>";
  }
  return '<g class="fork" data-fork="'+esc(gid)+'"><path d="M '+(fx-5)+' '+(fy-6)+' L '+(fx+6)+' '+fy+' L '+(fx-5)+' '+(fy+6)+' z"></path>'
    + "<title>fork: "+esc(gid)+"</title></g>"
    + '<g class="'+joinCls+'" data-join="'+esc(gid)+'"><circle cx="'+jx+'" cy="'+jy+'" r="6"></circle>'
    + '<line x1="'+(jx-4)+'" y1="'+jy+'" x2="'+(jx+4)+'" y2="'+jy+'"></line>'
    + "<title>join: "+esc(gid)+"</title></g>"
    + lblSvg;
}
var ARCH_CENTER = {};
var archAnim = { edges:{}, nodes:{}, join:null };
function archEdgeHits(f, tt, sub){
  var out=[];
  TOPOLOGY.edges.forEach(function(e,i){
    if (e.from===f && e.to===tt && (!sub || (e.label||"").toLowerCase().indexOf(sub.toLowerCase())>=0)) out.push(i);
  });
  return out;
}
function archMustEdge(ref){
  var h = archEdgeHits(ref[0], ref[1], ref[2]);
  if (h.length !== 1) throw new Error("scenario ref does not resolve uniquely against TOPOLOGY: "+ref.join(" / "));
  return h[0];
}
function archDegree(id){
  var out=[]; TOPOLOGY.edges.forEach(function(e,i){ if(e.from===id||e.to===id) out.push(i); });
  return out;
}
function archLayerActive(){ return archState.layer!=="all"; }
function archEdgeInLayer(e){ return !archLayerActive() || (e.layers||[]).indexOf(archState.layer)>=0; }
function archNodeDim(id){
  if (!archLayerActive()) return false;
  return !archDegree(id).some(function(i){ return archEdgeInLayer(TOPOLOGY.edges[i]); });
}
function archFocusHidden(){
  return TOPOLOGY.nodes.filter(function(n){
    return n.kind==="ui" || n.kind==="system" || ["gateway","transport","model_authority"].indexOf(n.id)>=0;
  }).map(function(n){ return n.id; });
}
function archIsHidden(id){ return archState.focus && archFocusHidden().indexOf(id)>=0; }
/* ---- the 16 scenarios: every reference resolves against TOPOLOGY at load
   time; a non-unique reference THROWS - the player can never carry a second
   architecture authority (qa_probe_v4_4 D5) ---- */
var SCENARIOS_ARCH = [
 { id:"seq", title:"Normal sequential successful run", steps:[
   { t:"Run Ticket", x:"The palette command spawns the loop over stdio - the extension only relays, it never decides.", mode:"human", e:[["human","vscode_ui"],["vscode_ui","gateway"],["gateway","loop"]] },
   { t:"Ticket + comprehension", x:"Jira text and acceptance criteria arrive; the spec agent scores comprehension after the free deterministic preflight.", e:[["jira","loop"],["jira","spec"]], n:["spec"] },
   { t:"Repo map + ratified context", x:"The cartographer maps the repo on a step budget; the human-ratified context feeds comprehension; the map goes to the lead.", e:[["cartographer","spec"],["drafter","spec"],["cartographer","lead"]] },
   { t:"Blast radius declared", x:"The lead declares the radius; from here every edit is checked deterministically - allow or deny, never a warning.", e:[["lead","blast_enforcer"]], n:["governor"] },
   { t:"Plan", x:"The planner writes the implementation plan inside the declared radius.", e:[["lead","planner"]], n:["planner"] },
   { t:"Frozen tests", x:"Test-spec writes the acceptance suite; member-chain validates every test statically before anything is frozen.", e:[["test_spec_agent","member_chain"]], n:["test_spec_agent"] },
   { t:"Develop", x:"Task by task against the frozen tests; contained execution; a checkpoint after every green task.", e:[["test_spec_agent","developer"],["blast_enforcer","developer"],["developer","containment"],["developer","checkpointer"]] },
   { t:"Blind review, then security", x:"The sequential path: the reviewer sees the diff blind, then the deterministic scan runs.", e:[["developer","reviewer"],["developer","security_agent"]] },
   { t:"QA", x:"The frozen acceptance suite runs for real as the authoritative gate.", e:[["qa_agent","containment"]], n:["qa_agent"] },
   { t:"Mutation", x:"AST mutation with zero model calls; survivors are evidence, never a verdict.", e:[["mutation_engine","containment"]], n:["mutation_engine"] },
   { t:"Evidence, retro, READY", x:"Everything was persisted before it was emitted; retro reads the digest; READY is evidence-gated and waits for a human.", mode:"evidence", e:[["loop","ledger"],["ledger","retro"],["retro","ledger"],["mission_control","human"]] }
 ]},
 { id:"spd4", title:"Speculative baseline beside test-spec, joining before development", steps:[
   { t:"Plan agreed", x:"The moment the plan is agreed, the loop starts the baseline unit suite speculatively (SPD-4).", n:["loop","planner"] },
   { t:"Two tracks at once", x:"Test-spec chats on the model transport WHILE the baseline suite boots as a deterministic pytest subprocess - zero model calls on that track.", join:"speculative_baseline",
     par:[ { e:[["test_spec_agent","member_chain"]], n:["test_spec_agent"] }, { e:[["loop","baseline_suite"]], n:["baseline_suite"] } ] },
   { t:"Join BEFORE init_pristine", x:"Develop consumes the future (abandonable 930s join) before any change - a red tree is never baptized as the baseline.", e:[["baseline_suite","developer"]] },
   { t:"Develop from the verified baseline", x:"An unusable future falls back to an ordinary baseline run - in order, exactly as the non-speculative path.", e:[["test_spec_agent","developer"],["developer","checkpointer"]] }
 ]},
 { id:"planners", title:"Parallel planners", steps:[
   { t:"Bake-off fans out", x:"governor.parallel_planners (off by default) overlaps the planner turns.", e:[["lead","planner"]], n:["second_plan"] },
   { t:"Planners overlap", x:"Each planner rides its own role and model; the transport routes replies by id.", join:"parallel_planners",
     par:[ { e:[["planner","judge"]] }, { e:[["second_plan","judge"]] } ] },
   { t:"The blind judge decides", x:"The judge waits for ALL planners - it must - and scores without knowing authorship.", e:[["judge","loop"]] }
 ]},
 { id:"fused", title:"Fused scope+plan fast path", steps:[
   { t:"Zero-model-call classifier", x:"prefetch.low_risk_candidate decides up front from the deterministic prefetch; a pending plan-change-request.md disqualifies, unoverridable by any mode.", n:["loop","governor"] },
   { t:"ONE fused turn", x:"Blast radius AND plan in one model call; the plan stage records itself without a second spend.", e:[["scope_plan","loop"]], n:["scope_plan"], join:"scope_plan_fused" },
   { t:"Declined = typed escalation", x:"A declined fused turn stops with a typed complexity escalation - never a silent expansion to the slow path.", mode:"repair", n:["loop"] }
 ]},
 { id:"pdev", title:"Parallel development", steps:[
   { t:"Big splittable plan", x:"governor.parallel_dev (off by default): the lead partitions the plan into independent slices. The speculative baseline is skipped - the lead path never consumes the future.", n:["lead_developer"] },
   { t:"One worker per slice", x:"Workers implement slices concurrently; the lead coaches failures itself and owns the slice checkpoint discipline.", join:"parallel_dev",
     par:[ { e:[["lead_developer","developer"]] }, { n:["developer"] } ] },
   { t:"Joined tree, measured once", x:"unit_tests gates the joined tree; a single-slice plan falls straight back to the plain developer.", e:[["developer","checkpointer"]] }
 ]},
 { id:"pqa", title:"Parallel QA", steps:[
   { t:"Sharded frozen suite", x:"governor.parallel_qa (off by default): the lead QA shards the frozen tests into independent groups.", n:["lead_qa"] },
   { t:"One worker per shard", x:"Workers run shards concurrently; inadequate mock data is coached; real code gaps are reported.", join:"parallel_qa",
     par:[ { e:[["lead_qa","qa_agent"]] }, { n:["qa_agent"] } ] },
   { t:"One qa_e2e verdict", x:"A single shard falls back to the plain QA run; parallel_post_develop declines while sharding is on.", e:[["qa_agent","containment"]] }
 ]},
 { id:"prs", title:"Parallel review + security", steps:[
   { t:"Develop passed", x:"Both verifiers read the SAME frozen diff (SPD-3).", n:["developer"] },
   { t:"Two channels at once", x:"Each thread runs inside its OWN immutable call attribution; neither depends on the other's verdict.", join:"parallel_review_security",
     par:[ { e:[["developer","reviewer"]] }, { e:[["developer","security_agent"]] } ] },
   { t:"QA gates on BOTH", x:"QA proceeds only after both verdicts join.", n:["qa_agent"] },
   { t:"Mutation stays sequential", x:"After QA, exactly as in the sequential path.", n:["mutation_engine"] }
 ]},
 { id:"ppd", title:"Parallel review + security + QA, joined before Mutation", steps:[
   { t:"The guarded clean path", x:"All three gates enabled, nothing resumed, no budget halt, not under parallel_qa - anything special falls back to the sequential path.", n:["developer","governor"] },
   { t:"Three channels at once", x:"Review session / stateless scanner / qa session - none can see another's conversation.", join:"parallel_post_develop",
     par:[ { e:[["developer","reviewer"]] }, { e:[["developer","security_agent"]] }, { n:["qa_agent"], e:[["qa_agent","containment"]] } ] },
   { t:"The verdict JOINS all three", x:"Repair loops run on the joined results exactly as in the sequential path.", n:["loop"] },
   { t:"Evidence invalidation", x:"If the review repair touched the tree, the concurrent QA result is SUPERSEDED (its row stands, append-only) and QA re-runs on the repaired tree.", mode:"repair", e:[["repair_controller","qa_agent"]] },
   { t:"Mutation AFTER the join", x:"Always sequential, by design: zero model calls to hide, CPU contention with QA's subprocesses, and no speculative gate evidence.", n:["mutation_engine"] }
 ]},
 { id:"revrep", title:"Blind-review failure and central repair with required rechecks", steps:[
   { t:"request_changes", x:"The blind reviewer routes findings to the central controller - one defect, one fingerprint.", mode:"repair", e:[["developer","reviewer"],["reviewer","repair_controller"]] },
   { t:"Bounded repair round", x:"The debugger edits, radius-scoped; budgets: 3 attempts per fingerprint, 6 per workflow.", mode:"repair", e:[["repair_controller","debugger"]] },
   { t:"Rechecks - the growing union", x:"Unit + frozen acceptance re-run, then a FRESH blind re-review; each round appends a superseding gate row.", mode:"repair", e:[["repair_controller","reviewer"]] },
   { t:"No-op and rollback guard", x:"An unchanged tree is detected; red rechecks roll back to the last checkpoint; two no-op rounds block.", mode:"repair", e:[["repair_controller","checkpointer"]] },
   { t:"Exhaustion is typed", x:"A spent budget refuses as DATA - the workflow parks BLOCKED and a human decides.", mode:"repair", e:[["repair_controller","mission_control"],["repair_controller","human"]] }
 ]},
 { id:"qaconv", title:"QA repair and non-convergence through deterministic qa-convergence", steps:[
   { t:"Acceptance failure", x:"The frozen suite names the exact AC - the highest-quality repair signal; the oracle-defect preflight can forbid code repair entirely.", mode:"repair", e:[["qa_agent","repair_controller"]] },
   { t:"Repair rounds with a ratchet", x:"Strictly-better trees are kept; worse ones roll back.", mode:"repair", e:[["repair_controller","debugger"],["repair_controller","checkpointer"]] },
   { t:"Non-convergence hands off", x:"The rounds are spent and the tree is not converging - the DETERMINISTIC finalizer takes over. No model call, no prompt file.", mode:"repair", e:[["repair_controller","qa_convergence"]] },
   { t:"The superseding FAIL", x:"qa-convergence writes the superseding qa_e2e FAIL (raw_suite_outcome preserved) so the gate's last word matches the stop.", mode:"repair", e:[["qa_convergence","ledger"]] },
   { t:"BLOCKED for a human", x:"The workflow parks BLOCKED; the evidence waits.", mode:"human", e:[["repair_controller","human"]] }
 ]},
 { id:"mutstr", title:"Mutation survivor, catcher-test strengthening and mutation rerun", steps:[
   { t:"Survivors are evidence", x:"The deterministic engine finds mutants the suite never kills - evidence, never a verdict.", e:[["mutation_engine","containment"]], n:["mutation_engine"] },
   { t:"Catcher tests", x:"The unit tester writes one catcher per surviving file; writes outside test paths are refused.", mode:"repair", e:[["mutation_engine","unit_tester"]] },
   { t:"Kept only if honest", x:"A catcher survives only red-against-the-mutant AND green-against-the-real-code - vacuous catchers are discarded.", n:["unit_tester"] },
   { t:"Full deterministic re-run", x:"The single recheck is a complete mutation re-run; a dry well burns budget and ends BLOCKED.", e:[["unit_tester","mutation_engine"],["mutation_engine","containment"]] }
 ]},
 { id:"halt", title:"Human comprehension halt", steps:[
   { t:"The gate reads the ticket", x:"Deterministic pre-gates run first - free; then the spec agent classifies blocking / investigation / prerequisite.", e:[["jira","spec"]] },
   { t:"Questions for the author", x:"Clarifying questions post back to Jira. Outcome: halted - the product WORKING, never a failure.", mode:"human", e:[["spec","human"]] },
   { t:"Answers return", x:"Comments or context/clarifications.md re-enter the next attempt.", mode:"human", e:[["human","jira"]] }
 ]},
 { id:"typed", title:"Budget or typed lifecycle stop", steps:[
   { t:"The meter reads first", x:"model_authority prices the call BEFORE it happens - predictive BudgetExceeded stops the spend, not the invoice.", e:[["loop","model_authority"]] },
   { t:"A typed failure lands", x:"Provider / tool / protocol / budget failures each land docket.call_failure.v1 - recorded before surfacing.", mode:"repair", e:[["transport","loop"]] },
   { t:"Truthful outcome", x:"The run ends with a truthful outcome; unreached stages stay never-reached - no invented state.", mode:"evidence", e:[["loop","ledger"]] }
 ]},
 { id:"resume", title:"Resume", steps:[
   { t:"A human authorizes another attempt", x:"Same workflow, same worktree.", mode:"human", e:[["human","loop","Resume Run"]] },
   { t:"Proof or re-pay", x:"A passed stage carries ONLY with proof: gate row + artifact sha256 + prompt-contract stamp + git-provable checkpoint completeness.", mode:"evidence", e:[["system_resume","ledger"]] },
   { t:"Refusal is honest", x:"Ticket drift or tree divergence refuses BEFORE any model call, with the reason.", n:["system_resume"] }
 ]},
 { id:"stop", title:"Cancel / Stop Run", steps:[
   { t:"The human stops the run", x:"Run outcome: abandoned (human_override).", mode:"human", e:[["human","loop","Stop Run"]] },
   { t:"The journey parks, never dies", x:"The workflow parks BLOCKED so Resume stays possible; CANCELLED happens only when a fresh run supersedes a never-started workflow.", n:["mission_control"], e:[["loop","mission_control"]] }
 ]},
 { id:"ship", title:"READY followed by Ship", steps:[
   { t:"Evidence-gated READY", x:"Only after every required gate - joined or sequential - reaches an acceptable terminal state.", e:[["mission_control","human"]] },
   { t:"Ship is deliberately manual", x:"Branch + commit + PR body; the run ends merged; READY moves to COMPLETED.", mode:"human", e:[["human","ledger"]] }
 ]}
];
var player = { scnIx:-1, steps:[], ix:-1, playing:false, speed:1, timer:null, pulses:[] };
function archResolveSteps(def){
  return def.steps.map(function(st){
    return { t:st.t, x:st.x, mode:st.mode||"flow", join:st.join||null,
      n:(st.n||[]).map(function(id){ if(!TOPOLOGY.nodes.some(function(nn){return nn.id===id;})) throw new Error("scenario names unknown node "+id); return id; }),
      e:(st.e||[]).map(archMustEdge),
      par:(st.par||[]).map(function(br){
        return { n:(br.n||[]).map(function(id){ if(!TOPOLOGY.nodes.some(function(nn){return nn.id===id;})) throw new Error("scenario names unknown node "+id); return id; }),
                 e:(br.e||[]).map(archMustEdge) };
      }) };
  });
}
player.stopTimer = function(){
  if (this.timer !== null && typeof clearTimeout !== "undefined") clearTimeout(this.timer);
  this.timer = null;
};
player.load = function(ix){
  this.stopTimer();
  this.scnIx = ix;
  this.steps = (ix>=0 && ix<SCENARIOS_ARCH.length) ? archResolveSteps(SCENARIOS_ARCH[ix]) : [];
  this.ix = -1; this.playing = false; this.pulses = [];
  archAnim = { edges:{}, nodes:{}, join:null };
  redrawArch();
  if (ix>=0) archAnnounce("Scenario loaded: "+SCENARIOS_ARCH[ix].title+". Use Play or Next step.");
};
player.applyStep = function(){
  var st = this.steps[this.ix];
  if (!st) return;
  var E = {}, N = {};
  st.e.forEach(function(i){ E[i]=1; });
  st.n.forEach(function(id){ N[id]=1; });
  st.par.forEach(function(br){ br.e.forEach(function(i){ E[i]=1; }); br.n.forEach(function(id){ N[id]=1; }); });
  Object.keys(E).forEach(function(i){
    var e = TOPOLOGY.edges[Number(i)];
    N[e.from]=1; N[e.to]=1;
  });
  archAnim = { edges:E, nodes:N, join:st.join };
  this.pulses = [];
  if (!archState.reduceMotion){
    var self=this;
    Object.keys(E).forEach(function(i){
      var e = TOPOLOGY.edges[Number(i)];
      self.pulses.push({ edge:Number(i), lane:(st.mode==="repair"?"repair":st.mode==="human"?"human":st.mode==="evidence"?"evidence":edgeLane(e)), t:0 });
    });
  }
  redrawArch();
  archSpawnPulses();
  archAnnounce("Step "+(this.ix+1)+" of "+this.steps.length+": "+st.t+". "+st.x);
};
player.next = function(){
  if (this.scnIx<0) return;
  if (this.ix < this.steps.length-1){ this.ix++; this.applyStep(); }
  else { this.playing=false; this.stopTimer(); redrawArch(); archAnnounce("Scenario complete."); }
};
player.prev = function(){
  if (this.scnIx<0) return;
  if (this.ix > 0){ this.ix--; this.applyStep(); }
  else { this.ix=-1; archAnim={edges:{},nodes:{},join:null}; this.pulses=[]; redrawArch(); }
};
player.restart = function(){
  if (this.scnIx<0) return;
  this.ix=-1; this.pulses=[]; archAnim={edges:{},nodes:{},join:null};
  this.next();
};
player.tick = function(){
  var self=this;
  if (!this.playing) return;
  this.next();
  if (this.ix >= this.steps.length-1){ this.playing=false; redrawArch(); return; }
  if (typeof setTimeout !== "undefined") this.timer = setTimeout(function(){ self.tick(); }, 2000/this.speed);
};
player.playToggle = function(){
  if (this.scnIx<0) this.load(0);
  if (this.playing){ this.playing=false; this.stopTimer(); redrawArch(); archAnnounce("Paused."); return; }
  this.playing = true;
  if (this.ix >= this.steps.length-1) this.ix = -1;
  var self=this;
  this.next();
  if (typeof setTimeout !== "undefined") this.timer = setTimeout(function(){ self.tick(); }, 2000/this.speed);
  redrawArch();
};
var archRafOn = false;
function archSpawnPulses(){
  if (archState.reduceMotion || !player.pulses.length) return;
  if (typeof requestAnimationFrame === "undefined") return;
  var layer = document.getElementById("pulse-layer");
  if (!layer || !layer.appendChild || !document.createElementNS) return;
  player.pulses.forEach(function(p, k){
    var c = document.createElementNS("http://www.w3.org/2000/svg","circle");
    if (!c || !c.setAttribute) return;
    c.setAttribute("class","pulse lane-"+p.lane);
    c.setAttribute("r","4.5");
    c.setAttribute("data-pulse", String(k));
    layer.appendChild(c);
  });
  if (!archRafOn){ archRafOn=true; requestAnimationFrame(archPulseFrame); }
}
function archPulseFrame(){
  archRafOn = false;
  if (archState.reduceMotion || !player.pulses.length) return;
  var layer = document.getElementById("pulse-layer");
  if (!layer || !layer.querySelectorAll) return;
  var els = layer.querySelectorAll("[data-pulse]");
  var alive = false;
  player.pulses.forEach(function(p, k){
    p.t += 0.011 * player.speed;
    if (p.t > 1) p.t = 0;
    alive = true;
    var el = els[k];
    if (!el) return;
    var e = TOPOLOGY.edges[p.edge], a = ARCH_CENTER[e.from], b = ARCH_CENTER[e.to];
    if (a && b){
      el.setAttribute("cx", a.x+(b.x-a.x)*p.t);
      el.setAttribute("cy", a.y+(b.y-a.y)*p.t);
    }
  });
  if (alive && typeof requestAnimationFrame !== "undefined"){ archRafOn=true; requestAnimationFrame(archPulseFrame); }
}
function subwayMapSvg(){
  ARCH_CENTER = {};
  var W=2080, H=1060;
  var routeSvg="", stSvg="", lblSvg="", fkSvg="", termSvg="";
  lblSvg += '<text class="lanehdr" x="20" y="22">primary direction: trains run LEFT to RIGHT on the main line; carmine routes loop BACK through the repair line below; the dotted line along the bottom is evidence, not execution</text>';
  var LANE_BUNDLES = { repairline:"repair", humanline:"human", evline:"evidence", enfline:"enforce" };
  ROUTES_C.forEach(function(r){ r.edges = []; });
  var flowUsed = {};
  ROUTES_C.forEach(function(r){
    var bundleLane = LANE_BUNDLES[r.id];
    TOPOLOGY.edges.forEach(function(e, i){
      var lanes = edgeLanes(e);
      if (bundleLane){ if (lanes.indexOf(bundleLane)>=0) r.edges.push(i); return; }
      if (lanes[0]!=="flow"){
        if (lanes.indexOf(r.lane)>=0 && r.stations.indexOf(e.from)>=0 && r.stations.indexOf(e.to)>=0) r.edges.push(i);
        return;
      }
      if (flowUsed[i]) return;
      if (r.stations.indexOf(e.from)>=0 && r.stations.indexOf(e.to)>=0){ r.edges.push(i); flowUsed[i]=1; }
    });
  });
  ROUTES_C.forEach(function(r){
    if (archLayerActive() && !r.edges.some(function(i){ return archEdgeInLayer(TOPOLOGY.edges[i]); })) return;
    var anyAnim = r.edges.some(function(i){ return archAnim.edges[i]; });
    var hi = archState.sel && r.edges.some(function(i){ var e=TOPOLOGY.edges[i]; return e.from===archState.sel || e.to===archState.sel; });
    var cls = "aedge route lane-"+r.lane+(r.cls?" "+r.cls:"")+(anyAnim?" scn-on":"")+(hi?" rt-hi":"");
    routeSvg += '<path class="'+cls+'" data-lane="'+r.lane+'" data-edges="'+r.edges.join(",")+'" d="'+orthPath(r.pts, 16)+'" marker-end="url(#aarr-'+r.lane+')">'
      + "<title>"+esc((r.lbl||r.id)+" - carries "+r.edges.length+" recorded communication(s); click a station for the full list")+"</title></path>";
    if (r.lbl){
      var la = r.lblAt || [r.pts[0][0]+6, r.pts[0][1]-8];
      lblSvg += '<text class="routelbl" x="'+la[0]+'" y="'+la[1]+'">'+esc(r.lbl)+"</text>";
    }
  });
  TOPOLOGY.nodes.forEach(function(n){
    if (archIsHidden(n.id)) return;
    var geo = stationGeom(n);
    if (!geo) return;
    ARCH_CENTER[n.id] = { x:geo.x, y:geo.y };
    var deg = archDegree(n.id);
    var r = geo.r, px = geo.x, py = geo.y;
    var partner = archState.sel && archState.sel!==n.id && deg.some(function(i){
      var e=TOPOLOGY.edges[i]; return e.from===archState.sel || e.to===archState.sel;
    });
    var cls = "an k-"+n.kind+(HUBS_C.indexOf(n.id)>=0?" hub":"")+(archState.sel===n.id?" sel":"")
      +(archAnim.nodes[n.id]?" scn-node":"")+(archNodeDim(n.id)?" nd-dim":"")+(partner?" hi-st":"");
    var marker;
    if (n.kind==="deterministic" || n.kind==="storage") marker = '<rect class="st-m" x="'+(px-r)+'" y="'+(py-r)+'" width="'+(2*r)+'" height="'+(2*r)+'" rx="2"></rect>';
    else if (n.kind==="hybrid") marker = '<rect class="st-m" x="'+(px-r)+'" y="'+(py-r)+'" width="'+(2*r)+'" height="'+(2*r)+'" rx="2" transform="rotate(45 '+px+' '+py+')"></rect>';
    else marker = '<circle class="st-m" cx="'+px+'" cy="'+py+'" r="'+r+'"></circle>';
    if (n.kind==="human" || n.kind==="ui") marker += '<circle class="st-m" cx="'+px+'" cy="'+py+'" r="'+(r+3.5)+'" style="fill:none"></circle>';
    var anchor = geo.side==="l" ? "end" : geo.side==="r" ? "start" : "middle";
    var tx = geo.side==="l" ? geo.box.x1 : geo.side==="r" ? geo.box.x0 : px;
    var ty0 = geo.box.y0 + 10;
    var g = '<g class="'+cls+'" data-arch="'+esc(n.id)+'" data-edges="'+deg.join(",")+'" tabindex="0" role="button" aria-pressed="'+(archState.sel===n.id)+'" aria-label="'+esc(n.label+" ("+n.kind+") station - press Enter for its "+deg.length+" communications")+'">'
      + marker + '<text class="st-t" text-anchor="'+anchor+'">';
    geo.lines.forEach(function(ln,k){ g += '<tspan x="'+tx+'" y="'+(ty0+k*11.5)+'">'+esc(ln)+"</tspan>"; });
    g += '</text><text class="st-g" x="'+px+'" y="'+(py+2.8)+'" text-anchor="middle">'+esc(STATION_GLYPH[n.kind]||"?")+"</text>"
      + "<title>"+esc(n.label+" ("+n.kind+") - "+n.responsibility)+"</title></g>";
    stSvg += g;
  });
  (TOPOLOGY.concurrency||[]).forEach(function(g){
    var fj = FORKS_C[g.id];
    if (fj) fkSvg += forkJoinSvg(g.id, fj.f[0], fj.f[1], fj.j[0], fj.j[1], fj.lbl);
  });
  TERMS_C.forEach(function(tm){
    var vocabList = (TOPOLOGY.vocab||{})[tm.from]||[];
    if (vocabList.indexOf(tm.key)<0) return; /* never invent a state */
    var lbl = tm.lbl || tm.key;
    termSvg += '<g class="an terminal k-deterministic" data-terminal="'+esc(tm.key)+'" tabindex="0">'
      + '<rect class="st-m" x="'+(tm.x-9)+'" y="'+(tm.y-9)+'" width="18" height="18"></rect>'
      + '<text class="st-t" x="'+tm.x+'" y="'+(tm.y+25)+'" text-anchor="middle">'+esc(lbl)+"</text>"
      + "<title>"+esc(lbl+" - "+tm.note+" (vocabulary: "+tm.from+")")+"</title></g>";
  });
  var repairBand = '<rect class="lanebox rep" data-lane-region="repair" x="990" y="698" width="800" height="76" rx="6"></rect>'
    + '<text class="lanehdr" x="1000" y="692">repair line - loops back, never through the main line</text>';
  var focusG = "";
  if (archState.focus){
    var hid = archFocusHidden();
    var hiddenEdges = 0;
    TOPOLOGY.edges.forEach(function(e){ if (hid.indexOf(e.from)>=0 || hid.indexOf(e.to)>=0) hiddenEdges++; });
    focusG = '<g data-group-members="'+esc(hid.join(" "))+'">'
      + '<rect x="14" y="'+(H-64)+'" width="620" height="52" rx="6" fill="var(--ground-2)" stroke="var(--rule)" stroke-dasharray="5 4"></rect>'
      + '<text class="st-t" x="26" y="'+(H-44)+'">Focus on execution: '+hid.length+' infrastructure components hidden with '+hiddenEdges+' communications.</text>'
      + '<text class="st-t" x="26" y="'+(H-28)+'" style="fill:var(--ink-mute);font-size:10px">Nothing is lost - every one stays in the text equivalent below. Use Show complete topology to restore.</text>'
      + "</g>";
  }
  return '<svg id="archmap-svg" viewBox="0 0 '+W+" "+H+'" width="100%" role="img" '
    + 'aria-label="The Docket transit map: the nine-stage main line with branch lines, fork and join stations, a carmine repair line, an ultramarine human line and a dotted evidence line; the complete text equivalent follows below">'
    + archMarkerDefs() + repairBand + routeSvg + fkSvg + termSvg + stSvg + lblSvg + focusG + '<g id="pulse-layer"></g></svg>';
}
function archToolbarHtml(){
  var h = '<div class="arch-toolbar" role="toolbar" aria-label="Transit map controls">';
  h += '<label for="arch-scnsel">Scenario</label><select id="arch-scnsel" data-scnsel>'
    + '<option value="-1"'+(player.scnIx<0?" selected":"")+'>Explore freely (no scenario)</option>'
    + SCENARIOS_ARCH.map(function(s,i){ return '<option value="'+i+'"'+(player.scnIx===i?" selected":"")+">"+(i+1)+" - "+esc(s.title)+"</option>"; }).join("")
    + "</select>"
    + '<button class="tbtn" data-playtoggle="1" aria-pressed="'+player.playing+'">'+(player.playing?"Pause":"Play")+"</button>"
    + '<button class="tbtn" data-restart="1">Restart</button>'
    + '<button class="tbtn" data-prev="1">Prev step</button>'
    + '<button class="tbtn" data-next="1">Next step</button>'
    + '<label for="arch-speedsel">Speed</label><select id="arch-speedsel" data-speedsel>'
    + [0.5,1,2].map(function(s){ return '<option value="'+s+'"'+(player.speed===s?" selected":"")+">"+s+"x</option>"; }).join("")
    + "</select>"
    + '<label><input type="checkbox" data-reduce="1"'+(archState.reduceMotion?" checked":"")+"> Reduce motion</label>"
    + '<button class="tbtn" data-focusexec="1" aria-pressed="'+archState.focus+'">Focus on execution</button>'
    + '<button class="tbtn" data-archshowall="1">Show complete topology</button>'
    + '<button class="tbtn" data-zoomout="1" aria-label="Zoom out">-</button>'
    + '<button class="tbtn" data-zoomin="1" aria-label="Zoom in">+</button>'
    + '<button class="tbtn" data-zoomreset="1">Reset view</button>'
    + '<button class="tbtn" data-archfit="1" title="fit the complete topology in the visible width">Fit</button>'
    + '<button class="tbtn" data-archfs="1" aria-pressed="'+archState.fullscreen+'" aria-expanded="'+archState.fullscreen+'">'
    + (archState.fullscreen ? "Exit full screen" : "Full screen") + "</button>"
    + "</div>";
  return h;
}
function archStepbarHtml(){
  var h = '<div class="arch-stepbar" id="arch-stepbar" aria-live="polite">';
  if (player.scnIx>=0){
    var sc2 = SCENARIOS_ARCH[player.scnIx];
    if (player.ix>=0 && player.steps[player.ix]){
      var st = player.steps[player.ix];
      h += "<b>"+(player.ix+1)+" / "+player.steps.length+"</b> &nbsp; <b>"+esc(sc2.title)+"</b> - "+esc(st.t)+": "+esc(st.x);
      if (st.join) h += ' <span style="color:var(--ultra)">[join: '+esc(st.join)+"]</span>";
    } else {
      h += "<b>"+esc(sc2.title)+"</b> - press Play or Next step. "+esc(player.steps.length)+" steps.";
    }
  } else {
    h += '<span class="free">No scenario loaded - explore by clicking stations, or pick a scenario to watch the execution order travel the lines. The default view stays calm: nothing moves until you ask.</span>';
  }
  h += "</div>";
  return h;
}
function archDetailHtml(){
  var sel = archState.sel;
  if (!sel) return '<div class="empty" style="padding:14px">Select a node - click it or Tab to it and press Enter. Its inbound and outbound edges highlight and this bottom panel fills with what it is responsible for, what it receives and what it produces - the map above stays fully visible.</div>';
  var n = TOPOLOGY.nodes.filter(function(x){ return x.id===sel; })[0];
  if (!n) return "";
  var inbound = TOPOLOGY.edges.filter(function(e){ return e.to===sel; });
  var outbound = TOPOLOGY.edges.filter(function(e){ return e.from===sel; });
  var h = '<div class="sub-head" style="margin-top:0">'+esc(n.label)+' <span class="snapnote">'+esc(n.kind)+'</span> <button class="act" data-archclose="1" title="dismiss this station detail (the selection clears; the map stays)">Close</button></div>'
    + '<div class="tab-intro" style="margin:0 0 8px">'+esc(n.responsibility)+"</div>"
    + '<div class="sub-head">Files / modules</div>'
    + '<div class="tl-more" style="padding:2px 0">'+n.files.map(function(f2){ return "<code>"+esc(f2)+"</code>"; }).join(" ")+"</div>"
    + '<div class="sub-head">Inputs ('+inbound.length+" inbound edges)</div>";
  if (!inbound.length) h += '<div class="tl-more">no inbound edge in the topology</div>';
  inbound.forEach(function(e){
    h += '<div class="tl-more edge-hi edge-in" style="padding:2px 0"><b>'+esc(e.from)+"</b> - "+esc(e.label)+"</div>";
  });
  h += '<div class="sub-head">Outputs ('+outbound.length+" outbound edges)</div>";
  if (!outbound.length) h += '<div class="tl-more">no outbound edge in the topology</div>';
  outbound.forEach(function(e){
    h += '<div class="tl-more edge-hi edge-out" style="padding:2px 0">to <b>'+esc(e.to)+"</b> - "+esc(e.label)+"</div>";
  });
  return h;
}
function archConcurrencyHtml(){
  var groups = TOPOLOGY.concurrency||[];
  var h = '<div class="arch-h">Concurrency, speculation and fused paths - '+groups.length+' concurrency and fused-path groups, from the same topology</div>'
    + '<div class="arch-p">Every mode below is DATA on the one TOPOLOGY object - the stage-map badges above, these cards and the text-equivalent table below all derive from it. The defaults are honest: every governor knob ships OFF; the speculative baseline is the only always-on overlap; the fused fast path is chosen up front by a deterministic classifier, never fallen back to. Mutation participates in NO concurrent group - it always runs sequentially after the post-development join.</div>';
  groups.forEach(function(g){
    h += '<div class="panel conc-grp" data-conc="'+esc(g.id)+'">'
      + '<div class="conc-head"><b>'+esc(g.label)+'</b> <code>'+esc(g.knob)+'</code> <span class="snapnote">'+(g.default_on===true? "always on (no knob)" : g.default_on===false? "off by default" : "classifier-chosen")+"</span></div>"
      + '<div class="conc-flow">'
      + '<div class="cx-from">'+esc(g.after_label || ("after "+g.after))+"</div>"
      + '<div class="cx-branches">'
      + (g.participants||[]).map(function(pid){
          var n = TOPOLOGY.nodes.filter(function(x){ return x.id===pid; })[0] || {label:pid, kind:"?"};
          return '<div class="cx-br k-'+esc(n.kind)+'">'+esc(n.label)+' <span class="snapnote">'+esc(n.kind)+"</span></div>";
        }).join("")
      + "</div>"
      + '<div class="cx-join">join: '+esc(g.join_label||g.join)+(g.join_note? " ("+esc(g.join_note)+")":"")+"</div>"
      + "</div>";
    if (g.mutation_position) h += '<div class="tl-more" style="padding:2px 0"><b>Mutation:</b> '+esc(g.mutation_position)+"</div>";
    if (g.supersede) h += '<div class="tl-more" style="padding:2px 0"><b>Evidence invalidation:</b> '+esc(g.supersede)+"</div>";
    if (g.engaged_by) h += '<div class="tl-more" style="padding:2px 0"><b>Engaged by:</b> '+esc(g.engaged_by)+"</div>";
    h += '<div class="tl-more" style="padding:2px 0"><b>Engages when:</b> '+(g.conditions||[]).map(esc).join("; ")+"</div>"
      + '<div class="tl-more" style="padding:2px 0"><b>Fallback:</b> '+esc(g.fallback||"")+"</div>"
      + (g.channels? '<div class="tl-more" style="padding:2px 0"><b>Channels:</b> '+esc(g.channels)+"</div>" : "")
      + '<div class="tl-more" style="padding:2px 0;color:var(--ink-faint)">source: '+esc(g.source||"")+"</div>"
      + "</div>";
  });
  return h;
}
function pageArchitecture(){
  var p = archState.payload || {};
  var h = '<div class="section arch" style="max-width:none"><div class="section-head"><h2>Architecture</h2>'
    + '<span class="sub">the complete system map, read from current source - one explicit topology drives every diagram, the detail panel and the text equivalent below (no hand-maintained copies)</span></div>';
  /* 1. system overview */
  h += '<div class="arch-h">System overview</div>'
    + '<div class="arch-p">Agents decide; deterministic Python enforces and scores; the append-only ledger records; every UI surface only renders. The chain below is the whole product:</div>'
    + '<div class="df-box"><div class="df-name">You</div><div class="df-note">run tickets, answer questions, approve plans, ship READY work</div></div><div class="df-arrow">v</div>'
    + '<div class="df-box"><div class="df-name">VS Code commands + UI</div><div class="df-note">29 palette commands; renders and relays - the extension never decides a gate</div></div><div class="df-arrow">v</div>'
    + '<div class="df-box"><div class="df-name">gateway.js stdio relay</div><div class="df-note">policy-free: answers ONLY chat / models / capabilities; vscode.lm is the PRIMARY model transport (headless claude-CLI bridge is the optional alternative)</div></div><div class="df-arrow">v</div>'
    + '<div class="df-box"><div class="df-name">Python loop / control plane</div><div class="df-note">loop.py owns orchestration and every verdict; mission_control owns delivery state; model_authority meters every call</div></div><div class="df-arrow">v</div>'
    + '<div class="df-box"><div class="df-name">Project worktree + deterministic tools</div><div class="df-note">per-workflow isolated worktree; blast-radius enforcement, containment, checkpointer, member-chain validation</div></div><div class="df-arrow">v</div>'
    + '<div class="df-box"><div class="df-name">Append-only ledger + evidence</div><div class="df-note">ledger.db is the authoritative history; artifacts stay on disk with recorded sha256</div></div><div class="df-arrow">v</div>'
    + '<div class="df-box"><div class="df-name">Projections</div><div class="df-note">RunEventStore is the LIVE projection (seq = ledger event_id); the dashboard payload is the read-only historical projection</div></div>';
  h += '<div class="arch-p">The extension runs inside the editor\'s own bundled Node runtime. Nothing here asks you to install Node or npm: the extension is plain CommonJS with no dependencies and no build step, so there is nothing to fetch and nothing to compile. A system node is used only by this repository\'s developer test commands.</div>';

  /* 2. the nine-stage pipeline with its separate gate vocabulary */
  h += '<div class="arch-h">The nine-stage pipeline - stage names and gate names are different vocabularies</div>'
    + '<div class="arch-p">Nine STAGES record eight GATE names. Blast Radius is a first-class stage that records no gate row of its own; the plan gate (plan_approval) exists only when <code>gates.plan_approval.enabled</code> is on; Develop records the gate named unit_tests. Security runs concurrently with Blind Review when <code>governor.parallel_review_security</code> is on - both stages start before either gate row is written, and a repair that later touches the tree supersedes and re-runs the concurrent result. Every parallel, speculative and fused execution mode is STRUCTURED DATA, not prose - the badges on the cells below and the group cards that follow both derive from TOPOLOGY.concurrency.</div>'
    + '<div class="stagemap">';
  TOPOLOGY.stages.forEach(function(s2, i){
    var concG = (TOPOLOGY.concurrency||[]).filter(function(g){ return (g.stages||[]).indexOf(s2.id)>=0; });
    h += '<div class="sm-cell'+(s2.concurrent_with? " sm-conc":"")+'" data-smstage="'+esc(s2.id)+'"'+(s2.note? ' title="'+esc(s2.note)+'"':"")+'>'
      + '<div class="sm-n">'+(i+1)+" / 9"+(s2.concurrent_with? " - concurrent-capable":"")+"</div>"
      + '<div class="sm-stage">'+esc(s2.label)+"</div>"
      + '<div class="sm-gate">'+(s2.gate
          ? "gate: <b>"+esc(s2.gate)+"</b>"+(s2.gate_optional? ' <span class="snapnote">opt-in</span>':"")
          : '<span class="none">no gate row of its own</span>')
      + "</div>"
      + (concG.length? '<div class="sm-knobs">'+concG.map(function(g){ return '<span class="sm-knob" data-smconc="'+esc(g.id)+'" title="'+esc(g.label+" - "+(g.knob||""))+'">'+esc(g.id)+"</span>"; }).join("")+"</div>" : "")
      + "</div>";
  });
  h += "</div>";
  h += '<div class="arch-p arch-recorded-gates">Recorded gates in THIS payload: ' + ((p.gate_order || []).map(function (g) {
    var gi = (p.gate_info || {})[g] || {};
    return esc(gi.label || g);
  }).join(", ") || "none - no ledger loaded yet") + '. The nine stages above are the pipeline; these are the gate rows this ledger actually writes.</div>';
  h += archConcurrencyHtml();
  /* 3. the transit map - the desktop-approved primary visualization */
  var layers = ["all","product","agents","enforce","repair","human","evidence"];
  h += '<div class="arch-h">The Docket transit map - the one visual authority</div>'
    + '<div class="arch-p">The desktop-approved subway presentation. Stations are the 44 components (kind shown by shape and by the letter inside each marker); lines are ROUTES that bundle the recorded communications; forks and joins mark every one of the seven concurrency modes; the carmine repair line loops back BELOW the main line, the ultramarine human line runs along the top and the dotted evidence line along the bottom. Click a station (or Tab to it and press Enter) for every inbound and outbound communication; pick a scenario and press Play to watch the execution order travel the lines - the default view stays calm, and Reduce motion swaps travel for step-by-step emphasis. Layer filtering hides routes from view only; the topology beneath never changes and the text equivalent below always carries all of it.</div>'
    + archToolbarHtml()
    + archStepbarHtml()
    + '<div class="fchips" style="margin-bottom:10px" role="group" aria-label="Architecture layers">'
    + layers.map(function(l){
        return '<button class="chip" data-alayer="'+l+'" aria-pressed="'+(archState.layer===l)+'">'+esc(archLayerLabel(l))+"</button>";
      }).join("")
    + "</div>"
    + '<div class="panel arch-map"><div class="arch-viewport" data-archvp="1" tabindex="0" role="group" aria-label="Transit map viewport - drag or use the arrow keys to pan when zoomed in">'
    + '<div class="arch-canvas" style="width:'+Math.round(archState.zoom*100)+'%;transform:translate('+Math.round(archState.panX||0)+'px, '+Math.round(archState.panY||0)+'px)">'+subwayMapSvg()+"</div></div></div>"
    + '<div class="arch-legend">'
    + '<span class="arch-lg-item"><span class="arch-lg-swatch" style="border-top-color:var(--ink)"></span>main + branch routes (ink - success is silent)</span>'
    + '<span class="arch-lg-item"><span class="arch-lg-swatch lg-loop"></span>repair line (restrained carmine, loops back)</span>'
    + '<span class="arch-lg-item"><span class="arch-lg-swatch lg-human"></span>human decision line (dashed ultramarine)</span>'
    + '<span class="arch-lg-item"><span class="arch-lg-swatch" style="border-top-style:dotted;border-top-color:var(--ink-faint)"></span>evidence line (dotted)</span>'
    + '<span class="arch-lg-item">station letters: M model, D deterministic, Y hybrid, H human, S system, B storage, U UI - the shape repeats it (circle model, square deterministic, diamond hybrid, double ring human/UI)</span>'
    + '<span class="arch-lg-item">motion: pulses travel on selection, scenario playback or Play - simultaneous pulses are genuinely concurrent branches; the join marker flashes; nothing moves unasked</span>'
    + "</div>"
    + '<div class="panel arch-detail-panel" id="arch-detail">'+archDetailHtml()+"</div>";
  /* 4. repair and recovery loops */
  h += '<div class="arch-h">Repair and recovery loops - every bounded loop, labeled model or deterministic</div>'
    + '<div class="panel"><div style="overflow-x:auto"><table class="grid"><caption class="srx">Bounded repair and recovery loops</caption><thead><tr>'
    + '<th scope="col">Loop</th><th scope="col">Trigger</th><th scope="col" title="whether the loop spends model calls or is purely deterministic">Calls models?</th><th scope="col">Bound</th><th scope="col">What happens</th></tr></thead><tbody>';
  TOPOLOGY.loops.forEach(function(L){
    h += '<tr><td class="txt"><b>'+esc(L.name)+'</b></td><td class="txt">'+esc(L.trigger)+"</td>"
      + "<td>"+(L.model? "model" : "deterministic")+"</td>"
      + '<td class="txt">'+esc(L.bound)+"</td>"
      + '<td class="txt">'+esc(L.detail)+"</td></tr>";
  });
  h += "</tbody></table></div></div>";
  /* 5. human decision flow - rendered from the same topology (human layer) */
  var humanEdges = TOPOLOGY.edges.filter(function(e){ return (e.layers||[]).indexOf("human")>=0; });
  h += '<div class="arch-h">Human decision flow</div>'
    + '<div class="arch-p">READY is not delivery: the workflow waits for the manual Ship Run, which records the merge and moves READY to COMPLETED. Refresh and Start Clean affect ONLY the UI projection - historical truth in the ledger never changes. The '+humanEdges.length+' human edges, from the same topology:</div>'
    + '<div class="panel" style="padding:10px 14px">'
    + humanEdges.map(function(e){
        return '<div class="tl-more" style="padding:3px 0"><b>'+esc(e.from)+"</b> -&gt; <b>"+esc(e.to)+"</b>: "+esc(e.label)+"</div>";
      }).join("")
    + "</div>";
  /* 6. state and evidence model */
  var V = TOPOLOGY.vocab;
  h += '<div class="arch-h">State vocabularies - four independent sets, never merged</div>'
    + '<div class="arch-p">'+esc(V.vocab_note)+" "+esc(V.gate_outcome_note)+"</div>"
    + '<div class="vocab-cols">'
    + '<div class="panel vocab-col"><div class="vc-h">run execution outcome (runs.outcome)</div>'+V.run_outcomes.map(function(v2){ return '<div class="vc-v">'+esc(v2)+"</div>"; }).join("")+"</div>"
    + '<div class="panel vocab-col"><div class="vc-h">workflow delivery state (workflow.STATES)</div>'+V.workflow_states.map(function(v2){ return '<div class="vc-v">'+esc(v2)+"</div>"; }).join("")+"</div>"
    + '<div class="panel vocab-col"><div class="vc-h">gate outcome (stored)</div>'+V.gate_outcomes.map(function(v2){ return '<div class="vc-v">'+esc(v2)+"</div>"; }).join("")+'<div class="vc-v" style="color:var(--ink-faint);font-style:italic">never_reached (projection only)</div></div>'
    + '<div class="panel vocab-col"><div class="vc-h">UI live-process state (Run Monitor)</div>'+V.ui_states.map(function(v2){ return '<div class="vc-v">'+esc(v2)+"</div>"; }).join("")+"</div>"
    + "</div>";
  h += '<div class="arch-h">Entities, cardinality and ownership</div>'
    + '<div class="panel"><div style="overflow-x:auto"><table class="grid"><caption class="srx">Entity cardinality</caption><thead><tr>'
    + '<th scope="col">Relationship</th><th scope="col">Where it is recorded</th></tr></thead><tbody>'
    + TOPOLOGY.cardinality.map(function(c){
        return '<tr><td class="txt"><b>'+esc(c.rel)+'</b></td><td class="txt">'+esc(c.detail)+"</td></tr>";
      }).join("")
    + "</tbody></table></div></div>";
  /* 7. the text equivalent - every node and every edge, always complete */
  h += '<div class="arch-h">Text equivalent - the complete topology as tables</div>'
    + '<div class="arch-p">Everything the diagrams show, as text: all '+TOPOLOGY.nodes.length+" nodes and all "+TOPOLOGY.edges.length+" edges, unaffected by the layer filter above. No information exists only as color or hover text.</div>"
    + '<div id="arch-text-equiv">'
    + '<div class="panel" style="margin-bottom:12px"><div style="overflow-x:auto"><table class="grid"><caption class="srx">All topology nodes</caption><thead><tr>'
    + '<th scope="col">Node</th><th scope="col">Kind</th><th scope="col">Files</th><th scope="col">Responsibility</th></tr></thead><tbody>'
    + TOPOLOGY.nodes.map(function(n){
        return '<tr><td class="txt"><b>'+esc(n.label)+'</b></td><td class="txt">'+esc(n.kind)+"</td>"
          + '<td class="txt">'+n.files.map(function(f2){ return "<code>"+esc(f2)+"</code>"; }).join(" ")+"</td>"
          + '<td class="txt">'+esc(n.responsibility)+"</td></tr>";
      }).join("")
    + "</tbody></table></div></div>"
    + '<div class="panel"><div style="overflow-x:auto"><table class="grid"><caption class="srx">All topology edges</caption><thead><tr>'
    + '<th scope="col">From</th><th scope="col">To</th><th scope="col">Layers</th><th scope="col">What flows</th></tr></thead><tbody>'
    + TOPOLOGY.edges.map(function(e){
        return '<tr class="aeq-edge"><td class="txt">'+esc(e.from)+'</td><td class="txt">'+esc(e.to)+"</td>"
          + '<td class="txt">'+esc((e.layers||[]).join(", "))+"</td>"
          + '<td class="txt">'+esc(e.label)+"</td></tr>";
      }).join("")
    + "</tbody></table></div></div>"
    + '<div class="panel" style="margin-top:12px"><div style="overflow-x:auto"><table class="grid"><caption class="srx">All concurrency and fused-path groups</caption><thead><tr>'
    + '<th scope="col">Group</th><th scope="col">Mode</th><th scope="col">Branches</th><th scope="col">Join</th><th scope="col">Conditions</th><th scope="col">Fallback</th></tr></thead><tbody>'
    + (TOPOLOGY.concurrency||[]).map(function(g){
        var br = (g.participants||[]).map(function(pid){
          var n = TOPOLOGY.nodes.filter(function(x){ return x.id===pid; })[0] || {label:pid};
          return n.label;
        }).join(" | ");
        return '<tr class="aeq-conc" data-conc-row="'+esc(g.id)+'"><td class="txt"><b>'+esc(g.label)+'</b></td>'
          + '<td class="txt">'+esc(g.knob)+" - "+(g.default_on===true? "always on" : g.default_on===false? "off by default" : "classifier-chosen")+"</td>"
          + '<td class="txt">'+esc(br)+"</td>"
          + '<td class="txt">'+esc(g.join_label||g.join)+"</td>"
          + '<td class="txt">'+(g.conditions||[]).map(esc).join("; ")+"</td>"
          + '<td class="txt">'+esc(g.fallback||"")+"</td></tr>";
      }).join("")
    + "</tbody></table></div></div>"
    + '<h2 style="font-size:16px;margin:18px 0 6px">Scenarios the transit map can animate</h2>'
    + '<div class="tab-intro">Each step lists the exact recorded communications it lights; parallel branches are marked. The player resolves every reference against TOPOLOGY at load time - an unknown identity throws.</div>'
    + '<div class="panel" style="margin-top:8px"><div style="overflow-x:auto"><table class="grid"><caption class="srx">All scenarios</caption><thead><tr>'
    + '<th scope="col">#</th><th scope="col">Scenario</th><th scope="col">Steps</th></tr></thead><tbody>'
    + SCENARIOS_ARCH.map(function(sc2, i){
        var steps = sc2.steps.map(function(st){
          var bits = [];
          (st.e||[]).forEach(function(r2){ bits.push(r2[0]+" to "+r2[1]); });
          (st.par||[]).forEach(function(br, bi){
            var bb=[]; (br.e||[]).forEach(function(r2){ bb.push(r2[0]+" to "+r2[1]); }); (br.n||[]).forEach(function(id){ bb.push(id); });
            bits.push("[branch "+(bi+1)+": "+bb.join(", ")+"]");
          });
          return st.t + (bits.length? " ("+bits.join("; ")+")" : "");
        }).join(" -> ");
        return '<tr data-eq-scn="'+i+'"><td>'+(i+1)+'</td><td class="txt"><b>'+esc(sc2.title)+'</b></td><td class="txt">'+esc(steps)+"</td></tr>";
      }).join("")
    + "</tbody></table></div></div>"
    + "</div>";
  h += "</div>";
  return h;
}


  // ---- V4.4 fit-to-width geometry: one fit authority ------------------
  // The canvas is width-percent of a hidden-overflow viewport, so the
  // complete topology fits ANY editor width by construction at zoom 1.
  // Full screen fits by CONTAIN (both axes). TOPOLOGY is never touched.
  function archViewportEl() {
    var host = archState.host;
    return host && host.querySelector
      ? host.querySelector("[data-archvp]") : null;
  }
  function archCanvasEl() {
    var host = archState.host;
    return host && host.querySelector
      ? host.querySelector(".arch-canvas") : null;
  }
  function archFitScale() {
    if (!archState.fullscreen) return 1;
    var vp = archViewportEl();
    if (!vp || !vp.clientWidth) return 1;
    var mapH = vp.clientWidth * (1060 / 2080);
    var availH = vp.clientHeight || mapH;
    return Math.min(1, +(availH / mapH).toFixed(3));
  }
  function archFitView() {
    archState.zoom = archFitScale();
    archState.panX = 0;
    archState.panY = 0;
    archState.userZoomed = false;
  }
  function clampArchPan() {
    var vp = archViewportEl();
    if (!vp || !vp.clientWidth) return;
    var cw = vp.clientWidth * archState.zoom;
    var ch = cw * (1060 / 2080);
    var vh = vp.clientHeight || ch;
    var maxX = Math.max(0, cw - vp.clientWidth);
    var maxY = Math.max(0, ch - vh);
    archState.panX = Math.min(0, Math.max(-maxX, archState.panX));
    archState.panY = Math.min(0, Math.max(-maxY, archState.panY));
  }
  function archApplyTransform() {
    var c = archCanvasEl();
    if (!c) { redrawArch(); return; }
    c.style.width = Math.round(archState.zoom * 100) + "%";
    c.style.transform = "translate(" + Math.round(archState.panX)
      + "px, " + Math.round(archState.panY) + "px)";
  }
  function archRestoreFsFocus() {
    var host = archState.host;
    if (!host || !host.querySelector) return;
    var b = host.querySelector("[data-archfs]");
    if (b && b.focus) b.focus();
  }
  function archToggleFs() {
    archState.fullscreen = !archState.fullscreen;
    var host = archState.host;
    if (host && host.classList) {
      host.classList.toggle("arch-fs", archState.fullscreen);
    }
    if (typeof document !== "undefined" && document.body
        && document.body.classList) {
      document.body.classList.toggle("arch-fs-body", archState.fullscreen);
    }
    // Native Fullscreen API only when supported - VS Code webviews may
    // refuse it; the CSS focus mode above is the real mechanism.
    try {
      if (archState.fullscreen) {
        if (host && host.requestFullscreen) {
          var pr = host.requestFullscreen();
          if (pr && pr.catch) pr.catch(function () {});
        }
      } else if (document.exitFullscreen && document.fullscreenElement) {
        var px = document.exitFullscreen();
        if (px && px.catch) px.catch(function () {});
      }
    } catch (e2) { /* fail-open to the CSS focus mode */ }
    archFitView();
    redrawArch();
    archAnnounce(archState.fullscreen
      ? "Full screen architecture - press Escape to exit; the map fits "
        + "the viewport and the prose sections moved below it"
      : "Exited full screen - focus returned to the Full screen button");
    archRestoreFsFocus();
  }

  function archAttr(t, name) {
    while (t && t.getAttribute) {
      var v = t.getAttribute(name);
      if (v !== null && v !== undefined) return v;
      t = t.parentNode;
    }
    return null;
  }
  function archSelect(id) {
    archState.sel = archState.sel === id ? null : id;
    player.pulses = [];
    if (archState.sel && !archState.reduceMotion) {
      archDegree(archState.sel).forEach(function (i) {
        player.pulses.push({ edge: i,
          lane: edgeLane(TOPOLOGY.edges[i]), t: 0 });
      });
    }
    redrawArch();
    archAnnounce(archState.sel
      ? "Selected " + id + " - inbound and outbound communications "
        + "highlighted; detail panel updated"
      : "Station selection cleared");
  }
  function wireArch(host) {
    host.addEventListener("click", function (e) {
      var v;
      v = archAttr(e.target, "data-arch");
      if (v) { archSelect(v); return; }
      v = archAttr(e.target, "data-alayer");
      if (v) {
        archState.layer = v;
        redrawArch();
        archAnnounce("Architecture layer: " + archLayerLabel(v)
          + " - the topology itself never changes");
        return;
      }
      if (archAttr(e.target, "data-playtoggle")) { player.playToggle(); return; }
      if (archAttr(e.target, "data-restart")) { player.restart(); return; }
      if (archAttr(e.target, "data-prev")) { player.prev(); return; }
      if (archAttr(e.target, "data-next")) { player.next(); return; }
      if (archAttr(e.target, "data-zoomin")) {
        archState.zoom = Math.min(3, +(archState.zoom * 1.25).toFixed(3));
        archState.userZoomed = true;
        clampArchPan(); archApplyTransform(); return;
      }
      if (archAttr(e.target, "data-zoomout")) {
        archState.zoom = Math.max(0.4, +(archState.zoom / 1.25).toFixed(3));
        archState.userZoomed = true;
        clampArchPan(); archApplyTransform(); return;
      }
      if (archAttr(e.target, "data-zoomreset")) {
        archFitView(); archApplyTransform(); return;
      }
      if (archAttr(e.target, "data-archfit")) {
        archFitView(); archApplyTransform(); return;
      }
      if (archAttr(e.target, "data-archfs")) { archToggleFs(); return; }
      if (archAttr(e.target, "data-archclose")) {
        archSelect(archState.sel); return;
      }
      if (archAttr(e.target, "data-focusexec")) {
        archState.focus = !archState.focus;
        redrawArch();
        archAnnounce(archState.focus
          ? "Focus on execution - infrastructure collapsed into an honest "
            + "labeled group; the text equivalent keeps everything"
          : "Complete topology restored");
        return;
      }
      if (archAttr(e.target, "data-archshowall")) {
        archState.focus = false; archState.layer = "all";
        redrawArch(); archAnnounce("Complete topology restored");
      }
    });
    host.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && archState.fullscreen) {
        e.preventDefault();
        archToggleFs();
        return;
      }
      if (archAttr(e.target, "data-archvp") !== null
          && ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"]
             .indexOf(e.key) !== -1) {
        e.preventDefault();
        var step = 48;
        if (e.key === "ArrowLeft") archState.panX += step;
        if (e.key === "ArrowRight") archState.panX -= step;
        if (e.key === "ArrowUp") archState.panY += step;
        if (e.key === "ArrowDown") archState.panY -= step;
        clampArchPan(); archApplyTransform();
        return;
      }
      var id = archAttr(e.target, "data-arch");
      if (id && (e.key === "Enter" || e.key === " ")) {
        e.preventDefault();
        archSelect(id);
      }
    });
    // drag-to-pan inside the hidden-overflow viewport; the page never
    // grows a horizontal scrollbar however far the user zooms.
    var archDrag = null;
    host.addEventListener("pointerdown", function (e) {
      if (archAttr(e.target, "data-archvp") === null) return;
      archDrag = { x: e.clientX, y: e.clientY,
                   px: archState.panX, py: archState.panY };
    });
    host.addEventListener("pointermove", function (e) {
      if (!archDrag) return;
      archState.panX = archDrag.px + (e.clientX - archDrag.x);
      archState.panY = archDrag.py + (e.clientY - archDrag.y);
      clampArchPan(); archApplyTransform();
    });
    host.addEventListener("pointerup", function () { archDrag = null; });
    host.addEventListener("pointercancel", function () {
      archDrag = null;
    });
    // the resize authority: editor/sidebar/panel/full-screen changes
    // re-fit (when the user has not zoomed) and always re-clamp the pan.
    if (typeof ResizeObserver !== "undefined") {
      try {
        var archRo = new ResizeObserver(function () {
          if (!archState.userZoomed) archState.zoom = archFitScale();
          clampArchPan();
          archApplyTransform();
        });
        archRo.observe(host);
      } catch (eRo) { /* hosts without layout keep the percent fit */ }
    }
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("fullscreenchange", function () {
        // native fullscreen dismissed by the browser (its own Escape
        // path): fold the CSS focus mode away with it.
        if (!document.fullscreenElement && archState.fullscreen) {
          archToggleFs();
        }
      });
    }
    host.addEventListener("change", function (e) {
      if (archAttr(e.target, "data-scnsel") !== null) {
        player.load(parseInt(e.target.value, 10)); return;
      }
      if (archAttr(e.target, "data-speedsel") !== null) {
        player.speed = parseFloat(e.target.value) || 1; return;
      }
      if (archAttr(e.target, "data-reduce") !== null) {
        archState.reduceMotion = !!e.target.checked;
        if (archState.reduceMotion) player.pulses = [];
        redrawArch();
      }
    });
  }

  function renderArchitecture(p) {
    var host = document.querySelector(".arch");
    if (!host) return;
    archState.payload = p || archState.payload;
    archState.host = host;
    if (host.dataset.builtV44) return;
    host.dataset.builtV44 = "1";
    wireArch(host);
    redrawArch();
  }

  function renderGateLegend(p) {
    var host = document.querySelector(".gate-legend");
    if (!host) return;
    host.textContent = "";
    (p.gate_order || []).forEach(function (g, i) {
      var info = (p.gate_info || {})[g] || {};
      var item = el("div", "gl-item");
      item.appendChild(el("span", "gl-abbr", (GATE_ABBR[g] || g.slice(0,4).toUpperCase())));
      item.appendChild(el("span", "gl-name", info.label || g));
      if (info.desc) item.appendChild(el("span", "gl-desc", info.desc));
      host.appendChild(item);
    });
  }

  function renderGates(p) {
    var body = $("#gate-body");
    if (!body) return;
    body.textContent = "";

    (p.gate_stats || []).forEach(function (g) {
      var tr = document.createElement("tr");
      tr.className = "gate-tr clickable";
      tr.setAttribute("role", "button");
      tr.setAttribute("tabindex", "0");
      tr.dataset.gate = g.name;
      var isOpen = state.openGate === g.name;
      tr.setAttribute("aria-expanded", isOpen ? "true" : "false");
      function toggle() {
        state.openGate = state.openGate === g.name ? null : g.name;
        renderGates(state.payload);
        var reopened = body.querySelector('tr[data-gate="' + g.name + '"]');
        if (reopened) reopened.focus();
      }
      tr.addEventListener("click", toggle);
      tr.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
      });

      // name only. The description goes on its own full-width row below, so a
      // long line can never bleed across the ran / passed / caught / score
      // columns the way it does when it lives in this narrow first cell.
      var nameCell = el("td", "gate-name-cell");
      nameCell.appendChild(el("div", "gate-full", g.label || g.name));
      var ginfo = (p.gate_info || {})[g.name];
      if (ginfo && ginfo.required === false) {
        var opt = el("span", "snapnote gate-optin", "opt-in");
        opt.title = "runs only when enabled in config - a skip here is "
          + "policy, not a pass";
        nameCell.appendChild(opt);
      }
      tr.appendChild(nameCell);

      tr.appendChild(put(el("td"), num(g.ran), "decisions not measured"));
      tr.appendChild(put(el("td"), num(g.pass)));
      tr.appendChild(put(el("td"), num(g.fail), "fail rows not measured"));

      var caught = el("td", "caught" + (g.caught ? " has" : ""));
      caught.textContent = g.caught;
      caught.title = g.caught
        ? g.label + " stopped " + g.caught + " run(s) that every upstream gate let through"
        : "this gate has never stopped anything in scope";
      tr.appendChild(caught);

      // Every recorded state in its own cell. num(0) is "0": a measured
      // zero renders 0, and only a value nobody measured earns the dash.
      tr.appendChild(put(el("td"), num(g.halts), "halts not measured"));
      tr.appendChild(put(el("td"), num(g.unknown),
        "unknown rows not measured"));
      tr.appendChild(put(el("td"), num(g.skipped), "skips not measured"));
      tr.appendChild(put(el("td"), num(g.never_reached),
        "absences not measured"));

      // score spread: min - median - max as a mini range bar
      var scoreCell = el("td", "score-cell");
      if (g.score_med != null) {
        var wrap = el("div", "score-range");
        wrap.title = "min " + g.score_min + " / median " + g.score_med + " / max " + g.score_max;
        var lo = el("span", "sr-lo", g.score_min.toFixed(2));
        var track = el("span", "sr-track");
        var span = el("span", "sr-span");
        // position span from min to max across a 0..threshold(1.0)-ish scale
        var scale = Math.max(1, g.score_max);
        span.style.left = (g.score_min / scale * 100) + "%";
        span.style.width = ((g.score_max - g.score_min) / scale * 100) + "%";
        track.appendChild(span);
        var medDot = el("span", "sr-med");
        medDot.style.left = (g.score_med / scale * 100) + "%";
        track.appendChild(medDot);
        wrap.appendChild(lo);
        wrap.appendChild(track);
        wrap.appendChild(el("span", "sr-hi", g.score_max.toFixed(2)));
        scoreCell.appendChild(wrap);
      } else {
        scoreCell.appendChild(unk("no scores recorded"));
      }
      tr.appendChild(scoreCell);

      tr.appendChild(put(el("td"), pct(g.pass_rate),
        "never decided in scope - unknown and skipped rows are answers "
        + "about the gate, not decisions"));
      body.appendChild(tr);

      // description on its own row, spanning the full table width so it wraps
      // instead of overflowing the first column
      if (g.desc) {
        var dtr = document.createElement("tr");
        dtr.className = "gate-desc-tr";
        var dtd = el("td", "gate-desc");
        dtd.colSpan = 11;
        dtd.textContent = g.desc;
        dtr.appendChild(dtd);
        body.appendChild(dtr);
      }

      if (isOpen) {
        var ctr = document.createElement("tr");
        ctr.className = "gate-caught-tr";
        var ctd = el("td", "gate-caught");
        ctd.colSpan = 11;
        ctd.appendChild(el("div", "eyebrow", "Stops and unknowns this gate recorded"));
        var rows = g.stopped || [];
        if (!rows.length) {
          ctd.appendChild(el("div", "empty",
            "This gate has recorded no stops or unknowns yet."));
        } else {
          rows.forEach(function (s) {
            var line = el("div", "gc-row");
            var head = el("div", "gc-head");
            head.appendChild(el("span", "mark " + s.outcome));
            head.appendChild(el("span", "gc-run",
              s.run ? String(s.run).slice(-8) : (s.issue || "")));
            if (s.count > 1) {
              var b = el("span", "snapnote gc-count", "x" + s.count + " runs");
              b.title = s.count + " runs recorded this identical outcome";
              head.appendChild(b);
            }
            if (s.at) head.appendChild(el("span", "gc-at",
              String(s.at).slice(0, 16)));
            line.appendChild(head);
            if (s.reason) {
              line.appendChild(el("div", "gc-reason", s.reason));
            } else {
              line.appendChild(unk("no reason recorded for this stop"));
            }
            (s.items || []).forEach(function (it) {
              line.appendChild(el("div", "gc-item", it));
            });
            if (s.more > 0) {
              line.appendChild(el("div", "gc-more", "+ " + s.more + " more"));
            }
            ctd.appendChild(line);
          });
          if (g.stopped_more > 0) {
            ctd.appendChild(el("div", "gc-more",
              "+ " + g.stopped_more + " earlier stops not shown"));
          } else {
            // Cap honesty runs both ways: when nothing was dropped, say
            // so, instead of leaving "is this everything?" to the reader.
            ctd.appendChild(el("div", "gc-more",
              "every recorded stop for this gate is shown above"));
          }
        }
        ctr.appendChild(ctd);
        body.appendChild(ctr);
      }
    });
  }

  // ---- one accounting authority -----------------------------------------
  //
  // Every money and token figure on this tab comes from payload.accounting,
  // which payload_builder computed through model_authority - the same seam
  // the budget brake meters. This renderer does no arithmetic of its own: it
  // formats numbers and states their coverage. That is the whole point. The
  // one time a renderer divided cached tokens itself, it divided by
  // (input + cached) and reported a 49.7% cache read where the truth was
  // 98.95% (run DATACMP-0-b53bd016), and the two figures sat on one page.

  function coverage(n, of, word) {
    if (n == null || of == null) return "";
    if (of === 0) return "";
    return n + " of " + of + " " + word;
  }

  function renderAccounting(p) {
    var host = document.getElementById("acct-body");
    if (!host) return;
    host.textContent = "";
    var a = p.accounting;
    if (!a) return;

    function row(label, value, why, note) {
      var r = el("div", "acct-row");
      r.appendChild(el("span", "acct-l", label));
      r.appendChild(put(el("span", "acct-v"), value, why));
      if (note) r.appendChild(el("span", "acct-n", note));
      host.appendChild(r);
      return r;
    }

    var counted = coverage(a.calls_token_counted, a.calls, "calls counted");
    row("model calls", num(a.calls), "no model turn on record",
        a.calls_failed ? a.calls_failed + " failed" : "");
    row("input tokens", num(a.tokens_in),
        "not every call reported a count, so the total is unknown",
        a.tokens_in === null && a.tokens_in_subtotal != null
          ? num(a.tokens_in_subtotal) + " across " + counted
          : counted);
    row("output tokens", num(a.tokens_out),
        "not every call reported a count, so the total is unknown",
        a.tokens_out === null && a.tokens_out_subtotal != null
          ? num(a.tokens_out_subtotal) + " across " + counted : "");
    // cache read is cached / input. Never cached / (input + cached): the
    // gateway already counts cache reads inside tokens_in.
    row("cache read", a.cache_read_pct == null ? null : a.cache_read_pct + "%",
        "no call reported a cache split, so the share was never measured",
        coverage(a.cache_calls_counted, a.calls, "calls reported a split"));
    row("recorded tokens", num(a.recorded_tokens),
        "the input or output side is unknown, so the weighted figure is too",
        "input + output, cache reads weighted at " + a.cache_read_weight);
    row("cost", money(a.cost_usd),
        "not every call was priced, so there is no total",
        a.cost_usd === null && a.cost_priced_subtotal != null
          ? money(a.cost_priced_subtotal) + " across " +
            coverage(a.calls_priced, a.calls, "calls priced")
          : coverage(a.calls_priced, a.calls, "calls priced"));

    var note = document.querySelector(".acct-note");
    if (note) {
      var bits = ["Computed by model_authority v" + a.authority_version +
                  ", the same seam the budget brake meters."];
      bits.push("Cache read is cached / input - cache reads are already " +
                "counted inside the input figure, so dividing by " +
                "(input + cached) would count them twice.");
      if ((a.cumulative_sessions || []).length) {
        bits.push("A provider reported a running total rather than a " +
                  "per-turn price on " + a.cumulative_sessions.length +
                  " session(s); those were converted to increments before " +
                  "anything was summed.");
      }
      if (a.per_call_truncated) {
        bits.push(a.per_call_truncated + " further calls are in the ledger " +
                  "and not listed here.");
      }
      note.textContent = bits.join(" ");
    }
  }

  // The scope's own money figures, which are a DIFFERENT population from the
  // accounting block above: those are per model CALL, these are per RUN and
  // per TICKET (payload_builder's `_run_cost` reads the run's accumulator).
  // Both are shipped, so both are shown, each with the coverage it is based
  // on - `tickets_priced` and `runs_priced` were computed and shipped and
  // rendered nowhere, which left the headline total with no stated basis.
  function renderScopeCost(p) {
    var host = document.getElementById("scope-cost-body");
    if (!host) return;
    host.textContent = "";
    var t = p.totals || {};
    if (t.tickets == null) return;

    function row(label, value, why, note) {
      var r = el("div", "acct-row");
      r.appendChild(el("span", "acct-l", label));
      r.appendChild(put(el("span", "acct-v"), value, why));
      if (note) r.appendChild(el("span", "acct-n", note));
      host.appendChild(r);
    }

    row("total cost", money(t.cost_usd),
        "not every ticket in scope recorded a price, so there is no total",
        t.cost_usd === null && t.cost_priced_subtotal != null
          ? money(t.cost_priced_subtotal) + " recorded across " +
            coverage(t.runs_priced, t.run_total, "runs")
          : coverage(t.runs_priced, t.run_total, "runs"));
    row("per ticket", money(t.cost_per_ticket),
        "no ticket in scope has a fully known cost",
        coverage(t.tickets_priced, t.tickets, "tickets fully priced"));
    row("input tokens", num(t.tokens_in),
        "not every run reported a count, so the total is unknown",
        t.tokens_in === null && t.tokens_in_subtotal != null
          ? num(t.tokens_in_subtotal) + " counted across " +
            coverage(t.runs_token_counted, t.run_total, "runs")
          : coverage(t.runs_token_counted, t.run_total, "runs"));
  }

  // ---- V4.4 Usage & Cost workbench ---------------------------------------
  //
  // Coverage bars, the token-flow figure, the linked breakdowns and the
  // per-call explorer. callExplorerModel is the pure seam: given the
  // payload and a filter set it answers which retained calls are in view,
  // the honest population numbers, and the DERIVED option lists.

  // Payload stage vocabulary -> pipeline stage id, derived from TOPOLOGY
  // (the one authority) rather than a second hand-kept map: a stage's own
  // id, its source_key and its gate name all resolve to it. The single
  // documented exception: actors whose payload stage is "context" (the
  // context-mapping crew) fold onto Blast Radius, where loop.py attributes
  // the lead's work.
  function stagePipelineMap() {
    var map = { context: "blast_radius" };
    (TOPOLOGY.stages || []).forEach(function (s) {
      map[s.id] = s.id;
      if (s.source_key) map[s.source_key] = s.id;
      if (s.gate) map[s.gate] = s.id;
    });
    return map;
  }

  // One actor -> its pipeline stage attribution, from the payload's own
  // roster rows (agent_info is the production owner of `stage`).
  function actorStageOf(p, actor) {
    var ag = ((p && p.agents) || []).filter(function (a) {
      return a.role === actor;
    })[0];
    if (!ag || !ag.stage) return null;
    return stagePipelineMap()[ag.stage] || null;
  }

  function callExplorerModel(p, f) {
    f = f || {};
    var a = (p && p.accounting) || {};
    var rows = a.per_call || [];
    var q = String(f.q || "").toLowerCase();
    var sel = f.sel || null;
    var optActor = {};
    var optStage = {};
    var optModel = {};
    var out = [];
    rows.forEach(function (c) {
      var st = actorStageOf(p, c.actor) || "unattributed";
      optActor[c.actor] = 1;
      optStage[st] = 1;
      if (c.model) optModel[c.model] = 1;
      var hay = (String(c.actor || "") + " " + String(c.model || "") + " "
                 + String(c.run || "") + " " + String(c.issue || ""))
        .toLowerCase();
      // Recorded-vs-absent is the split the filters cut on: a reported
      // zero cache split IS reported, and an unpriced call is null cost,
      // never $0.
      var selOk = !sel
        || (sel.dim === "agent" && c.actor === sel.val)
        || (sel.dim === "model" && c.model === sel.val)
        || (sel.dim === "ticket" && c.issue === sel.val)
        || (sel.dim === "stage" && st === sel.val);
      var ok = (!q || hay.indexOf(q) >= 0)
        && (!f.actor || c.actor === f.actor)
        && (!f.stage || st === f.stage)
        && (!f.model || c.model === f.model)
        && (!f.ok || (f.ok === "failed"
              ? c.failed === true : c.failed !== true))
        && (!f.priced || (f.priced === "priced"
              ? c.cost_usd != null : c.cost_usd == null))
        && (!f.cache || (f.cache === "reported"
              ? c.tokens_cached != null : c.tokens_cached == null))
        && selOk;
      if (ok) out.push(c);
    });
    return { rows: out, retained: rows.length,
             total: a.calls == null ? null : a.calls,
             truncated: a.per_call_truncated || 0,
             options: { actor: Object.keys(optActor).sort(),
                        stage: Object.keys(optStage).sort(),
                        model: Object.keys(optModel).sort() } };
  }

  // A coverage bar: the measured share of the call population, as a bar
  // plus the N-of-M sentence. A missing side is a reasoned dash.
  function covRow(host, label, numer, denom, note, why) {
    var r = el("div", "cov-row");
    r.appendChild(el("span", "cov-l", label));
    var v = el("span", "cov-v");
    if (numer == null || denom == null) {
      v.appendChild(unk(why));
    } else {
      var track = el("span", "cov-track");
      var bar = el("span", "cov-bar");
      bar.style.width = denom
        ? Math.max(numer ? 1 : 0, Math.round((numer / denom) * 100)) + "%"
        : "0%";
      track.appendChild(bar);
      v.appendChild(track);
      v.appendChild(el("span", "cov-n",
        num(numer) + " of " + num(denom) + " calls"));
    }
    r.appendChild(v);
    r.appendChild(el("span", "acct-n", note));
    host.appendChild(r);
  }

  function renderCoverage(p) {
    var host = document.querySelector("#acct-coverage");
    if (!host) return;
    host.textContent = "";
    var a = p.accounting;
    if (!a) return;
    covRow(host, "token coverage", a.calls_token_counted, a.calls,
      "recorded-token accounting: the share of calls that reported "
      + "token counts", "token-counted calls not measured");
    covRow(host, "price coverage", a.calls_priced, a.calls,
      "provider price coverage: a null cost is unpriced, never $0",
      "priced calls not measured");
    covRow(host, "cache coverage", a.cache_calls_counted, a.calls,
      "cache-metric coverage: only these calls reported a cached-read "
      + "split", "cache-reporting calls not measured");
  }

  function renderTokenFlow(p) {
    var host = document.querySelector("#tokflow");
    if (!host) return;
    host.textContent = "";
    var a = p.accounting;
    if (!a) return;
    var tin = a.tokens_in_subtotal;
    var tout = a.tokens_out_subtotal;
    var tc = a.tokens_cached;
    var reported = tc != null && (a.cache_calls_counted || 0) > 0;
    function tf(label, barPct, sliverPct, sliverTitle, n) {
      var variant = label === "input tokens" ? " tokflow-in"
        : label === "output tokens" ? " tokflow-out" : "";
      var r = el("div", "tf-row" + variant);
      r.appendChild(el("span", "tf-l", label));
      var track = el("span", "tf-track");
      if (barPct != null) {
        var bar = el("span", "tf-bar");
        bar.style.width = barPct + "%";
        track.appendChild(bar);
      }
      if (sliverPct != null) {
        var sl = el("span", "tf-bar tokflow-cache tf-cache");
        sl.style.width = sliverPct + "%";
        sl.title = sliverTitle || "";
        track.appendChild(sl);
      }
      r.appendChild(track);
      r.appendChild(put(el("span", "tf-n"), n,
        "not every call reported a count"));
      host.appendChild(r);
      return r;
    }
    tf("input tokens", tin != null ? 100 : null,
       reported && tin ? Math.max(0.5, Math.round((tc / tin) * 1000) / 10)
                       : null,
       reported ? "cached reads: " + num(tc) + " tokens = "
         + a.cache_read_pct + "% of counted input, reported by "
         + num(a.cache_calls_counted) + " call(s)" : "",
       num(tin));
    var cacheLine = el("div", "tf-row");
    cacheLine.appendChild(el("span", "tf-l", "of which cached"));
    if (reported) {
      // The share is the payload's own aggregate (cache_read_pct), never
      // a second division done here - see the 49.7%-vs-98.95% incident.
      cacheLine.appendChild(el("span", "tf-note",
        num(tc) + " cached-read tokens (" + a.cache_read_pct
        + "% of counted input) - reported by "
        + num(a.cache_calls_counted) + " of " + num(a.calls)
        + " calls"));
    } else {
      cacheLine.appendChild(el("span", "tokflow-unavail tf-note tf-unavail",
        "cache split unavailable - no call reported a cached-read "
        + "split; absent is not 0"));
    }
    host.appendChild(cacheLine);
    tf("output tokens",
       tin && tout != null ? Math.max(1, Math.round((tout / tin) * 100))
         : (tout != null ? 100 : null),
       null, "", num(tout));
    var rec = el("div", "tf-row");
    rec.appendChild(el("span", "tf-l", "recorded total"));
    if (a.recorded_tokens != null) {
      rec.appendChild(el("span", "tf-note",
        num(a.recorded_tokens) + " (cache-read weight "
        + a.cache_read_weight + ")"));
    } else {
      rec.appendChild(put(el("span", "tf-note"), null,
        "an input or output side is unknown, so the cache-weighted "
        + "total is not computable"));
    }
    host.appendChild(rec);
  }

  function usageSelParts() {
    if (!state.usageSel) return null;
    var i = state.usageSel.indexOf(":");
    return { dim: state.usageSel.slice(0, i),
             val: state.usageSel.slice(i + 1) };
  }

  function currentCallFilters() {
    var f = { q: state.callQ, sel: usageSelParts() };
    Object.keys(state.callF).forEach(function (k) {
      f[k] = state.callF[k];
    });
    return f;
  }

  function renderUsageBreaks(p) {
    var host = document.querySelector(".u-breaks");
    if (!host) return;
    host.textContent = "";
    var sel = usageSelParts();
    function panel(title, rows, dim) {
      var box = el("div", "panel ub-panel");
      box.appendChild(el("div", "ub-head", title));
      if (!rows.length) {
        box.appendChild(el("div", "empty",
          "no recorded input tokens under this breakdown"));
      }
      var mx = rows.length ? rows[0].n : 1;
      rows.forEach(function (r) {
        var bar = el("div", "ubar");
        bar.dataset.usel = dim + ":" + r.key;
        bar.setAttribute("role", "button");
        bar.setAttribute("tabindex", "0");
        if (sel && sel.dim === dim && sel.val === r.key) {
          bar.className += " ub-sel";
        }
        bar.appendChild(el("span", "ub-name", r.label));
        var track = el("span", "ub-track");
        var fill = el("span", "ub-bar");
        fill.style.width =
          Math.max(1, Math.round((r.n / (mx || 1)) * 100)) + "%";
        track.appendChild(fill);
        bar.appendChild(track);
        bar.appendChild(el("span", "ub-n", num(r.n)));
        function toggleSel() {
          var key = dim + ":" + r.key;
          state.usageSel = state.usageSel === key ? null : key;
          renderUsageBreaks(state.payload);
          renderCallExplorer(state.payload);
        }
        bar.addEventListener("click", toggleSel);
        bar.addEventListener("keydown", function (e) {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            toggleSel();
          }
        });
        box.appendChild(bar);
      });
      host.appendChild(box);
    }
    var agents = p.agents || [];
    var stAgg = {};
    agents.forEach(function (a) {
      if (a.tokens_in == null) return;
      var k = (a.stage && stagePipelineMap()[a.stage]) || "unattributed";
      stAgg[k] = (stAgg[k] || 0) + a.tokens_in;
    });
    var stLabel = { unattributed: "(no stage attribution)" };
    (TOPOLOGY.stages || []).forEach(function (s) {
      stLabel[s.id] = s.label;
    });
    panel("by stage (each actor's roster attribution; context-mapping "
          + "actors fold onto Blast Radius)",
      Object.keys(stAgg).filter(function (k) { return stAgg[k] > 0; })
        .sort(function (x, y) { return stAgg[y] - stAgg[x]; })
        .map(function (k) {
          return { key: k, label: stLabel[k] || k, n: stAgg[k] };
        }), "stage");
    panel("by agent (top 10 of " + agents.length + " actors)",
      agents.filter(function (a) {
        return a.tokens_in != null && a.tokens_in > 0;
      }).sort(function (x, y) { return y.tokens_in - x.tokens_in; })
        .slice(0, 10).map(function (a) {
          return { key: a.role, label: a.role, n: a.tokens_in };
        }), "agent");
    panel("by recorded model string",
      (p.models || []).filter(function (m) {
        return m.tokens_in != null && m.tokens_in > 0;
      }).sort(function (x, y) {
        return (y.tokens_in || 0) - (x.tokens_in || 0);
      }).slice(0, 10).map(function (m) {
        return { key: m.model, label: m.model, n: m.tokens_in || 0 };
      }), "model");
    panel("by ticket",
      (p.tickets || []).map(function (t) {
        return { key: t.issue, label: t.issue,
                 n: t.tokens_in != null ? t.tokens_in
                   : (t.tokens_in_subtotal || 0) };
      }).filter(function (r) { return r.n > 0; })
        .sort(function (x, y) { return y.n - x.n; }), "ticket");
  }

  // The count line's element is held by reference (not re-found through
  // querySelector) so updating it never rebuilds the toolbar - the search
  // input keeps focus across keystrokes, same rule as the Runs toolbar.
  var callCountEl = null;

  function updateCallCount(p) {
    if (!callCountEl || !p || !p.accounting) return;
    var m = callExplorerModel(p, currentCallFilters());
    var sel = usageSelParts();
    callCountEl.textContent =
      m.rows.length + " of " + num(m.retained)
      + " retained rows match - "
      + (sel ? "filtered to " + sel.dim + " = " + sel.val
             + " (select its bar again to clear)"
             : "no breakdown selection")
      + "; the ledger records "
      + (m.total == null ? "an unknown number of" : num(m.total))
      + " calls"
      + (m.truncated ? " (" + num(m.truncated)
        + " more were truncated at source)" : "");
  }

  function renderCallExplorer(p) {
    updateCallCount(p);
    var host = document.querySelector(".percall-host");
    if (!host) return;
    host.textContent = "";
    var a = p.accounting;
    if (!a) return;
    var m = callExplorerModel(p, currentCallFilters());
    if (!m.retained) {
      host.appendChild(el("div", "empty",
        "No per-call rows were retained in this payload."
        + (m.total ? " The ledger records " + num(m.total)
          + " calls; the per-call sample was truncated at source."
          : "")));
      return;
    }
    var table = el("table", "grid percall-table");
    table.appendChild(el("caption", "srx", "Recorded model calls"));
    var thead = el("thead");
    var hr = el("tr");
    ["At", "Actor", "Model", "Run", "Tok in", "Tok out", "Cache",
     "Recorded", "Cost", "Cost basis", "Ok"].forEach(function (h) {
      hr.appendChild(el("th", null, h));
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el("tbody");
    var CAP = 30;
    m.rows.slice(0, CAP).forEach(function (c) {
      var tr = el("tr");
      tr.appendChild(el("td", "txt", String(c.at || "").slice(5, 16)));
      tr.appendChild(el("td", null, c.actor));
      tr.appendChild(put(el("td"), c.model, "no model recorded"));
      tr.appendChild(el("td", "txt", String(c.run || "").slice(-8)));
      tr.appendChild(put(el("td"), num(c.tokens_in),
        "no count reported"));
      tr.appendChild(put(el("td"), num(c.tokens_out),
        "no count reported"));
      tr.appendChild(put(el("td"), num(c.tokens_cached),
        "no cache split reported on this call"));
      tr.appendChild(put(el("td"), num(c.recorded_tokens),
        "an input or output side is unknown"));
      tr.appendChild(put(el("td"), money(c.cost_usd),
        "call not priced"));
      tr.appendChild(put(el("td"), c.cost_basis,
        "no cost basis recorded - the call was never priced"));
      tr.appendChild(el("td", c.failed ? "pc-failed" : null,
        c.failed ? "failed" : "ok"));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.appendChild(table);
    if (m.rows.length > CAP) {
      host.appendChild(el("div", "gc-more",
        "+ " + (m.rows.length - CAP)
        + " more matched rows not shown (display cap)"));
    }
  }

  // The toolbar: search + six selects, options DERIVED from the data. The
  // search input never rebuilds itself on keystroke, so focus survives -
  // same rule as the Runs toolbar.
  function renderCallBar(p) {
    var host = document.querySelector(".percall-bar");
    if (!host) return;
    host.classList.add("tux-bar");
    var a = p.accounting;
    if (!a) { host.textContent = ""; return; }
    var all = callExplorerModel(p, {});
    host.textContent = "";
    host.appendChild(el("span", "tux-level",
      "filters - counts are recorded model calls"));
    callCountEl = el("span", "percall-count");
    host.appendChild(callCountEl);
    updateCallCount(p);
    var q = el("input", "tux-q percall-q");
    q.type = "search";
    q.value = state.callQ || "";
    q.placeholder = "actor / model / run / ticket";
    q.setAttribute("aria-label",
      "Search retained calls by actor, model, run or ticket");
    q.addEventListener("input", function () {
      state.callQ = q.value;
      renderCallExplorer(state.payload);
    });
    host.appendChild(q);
    function addSel(label, key, opts) {
      var s = el("select", "percall-sel percall-sel-" + key);
      s.setAttribute("aria-label", label);
      var o0 = el("option", null, label + ": all");
      o0.value = "";
      s.appendChild(o0);
      opts.forEach(function (v) {
        var o = el("option", null, v);
        o.value = v;
        if (state.callF[key] === v) o.selected = true;
        s.appendChild(o);
      });
      s.addEventListener("change", function () {
        state.callF[key] = s.value;
        renderCallExplorer(state.payload);
      });
      host.appendChild(s);
    }
    addSel("agent", "actor", all.options.actor);
    addSel("stage", "stage", all.options.stage);
    addSel("model", "model", all.options.model);
    addSel("outcome", "ok", ["ok", "failed"]);
    addSel("priced", "priced", ["priced", "unpriced"]);
    addSel("cache", "cache", ["reported", "unavailable"]);
  }

  // ---- V4.4 pipeline economics -------------------------------------------
  //
  // The actor aggregates folded onto the nine pipeline stages. A bucket's
  // counter is NULL until some actor in it recorded a value - a stage
  // whose actors never counted events reports unknown, not zero. Nothing
  // goes uncounted: context actors fold onto Blast Radius (the same
  // documented exception as the breakdowns) and roster-less actors land
  // on an explicit unattributed row.
  function stageEconModel(p) {
    function bucket(id, label) {
      return { id: id, label: label, actors: [], events: null,
               failed: null, tokens_in: null, tokens_out: null,
               duration_ms: null, cost_usd: null, actors_priced: 0 };
    }
    function add(cur, v) {
      if (v == null) return cur;
      return (cur == null ? 0 : cur) + v;
    }
    var map = stagePipelineMap();
    var stages = (TOPOLOGY.stages || []).map(function (s) {
      return bucket(s.id, s.label);
    });
    var byId = {};
    stages.forEach(function (b) { byId[b.id] = b; });
    var unatt = bucket("unattributed", "(no stage attribution)");
    ((p && p.agents) || []).forEach(function (a) {
      var b = (a.stage && byId[map[a.stage]]) || unatt;
      b.actors.push(a.role);
      b.events = add(b.events, a.calls);
      b.failed = add(b.failed, a.failed_calls);
      b.tokens_in = add(b.tokens_in, a.tokens_in);
      b.tokens_out = add(b.tokens_out, a.tokens_out);
      b.duration_ms = add(b.duration_ms, a.duration_ms);
      b.cost_usd = add(b.cost_usd, a.cost_usd);
      if (a.cost_usd != null) b.actors_priced += 1;
    });
    return { stages: stages, unattributed: unatt };
  }

  function renderStageEcon(p) {
    var host = document.querySelector("#stage-econ");
    if (!host) return;
    host.textContent = "";
    if (!p.agents || !p.agents.length) {
      host.appendChild(el("div", "empty",
        "No actor aggregates in this payload."));
      return;
    }
    var m = stageEconModel(p);
    var rows = m.stages.concat([m.unattributed]);
    var maxTin = 0;
    rows.forEach(function (b) {
      if (b.tokens_in != null && b.tokens_in > maxTin) {
        maxTin = b.tokens_in;
      }
    });
    var table = el("table", "grid");
    table.appendChild(el("caption", "srx", "Pipeline stage economics"));
    var thead = el("thead");
    var hr = el("tr");
    ["Stage", "Actors", "Recorded events", "Failed", "Tok in", "",
     "Tok out", "Duration", "Priced subtotal"].forEach(function (h) {
      var th = el("th", null, h);
      if (h === "Recorded events") {
        th.title = "ledger event rows by these actors - not necessarily "
          + "model calls";
      }
      hr.appendChild(th);
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el("tbody");
    rows.forEach(function (b) {
      if (b.id === "unattributed" && !b.actors.length) return;
      var tr = el("tr");
      tr.appendChild(el("td", null, b.label));
      var ac = el("td", "txt");
      if (b.actors.length) ac.textContent = b.actors.join(", ");
      else ac.appendChild(unk("no actor carries this stage attribution "
        + "in the roster"));
      tr.appendChild(ac);
      tr.appendChild(put(el("td"), num(b.events),
        "no events counted for these actors"));
      tr.appendChild(put(el("td"), num(b.failed),
        "failed calls not counted"));
      tr.appendChild(put(el("td"), num(b.tokens_in),
        "no tokens recorded"));
      var barTd = el("td");
      if (maxTin && b.tokens_in) {
        var bar = el("span", "bar ultra");
        bar.style.width =
          Math.max(1, Math.round((b.tokens_in / maxTin) * 120)) + "px";
        barTd.appendChild(bar);
      }
      tr.appendChild(barTd);
      tr.appendChild(put(el("td"), num(b.tokens_out),
        "no tokens recorded"));
      tr.appendChild(put(el("td"),
        b.duration_ms == null ? null
          : Math.round(b.duration_ms / 1000) + "s",
        "duration not recorded"));
      var costTd = el("td");
      if (b.cost_usd == null) {
        costTd.appendChild(unk("no actor in this stage recorded a "
          + "priced call"));
      } else {
        costTd.textContent = money(b.cost_usd);
        costTd.title = b.actors_priced + " of " + b.actors.length
          + " actors priced";
      }
      tr.appendChild(costTd);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    host.appendChild(table);
  }

  // ---- cost by agent ----------------------------------------------------

  function renderAgents(p) {
    var body = $("#agent-body");
    body.textContent = "";
    var rows = p.agents || [];
    var max = Math.max.apply(null, rows.map(function (a) {
      return a.cost_usd || 0;
    }).concat([0.0001]));

    rows.forEach(function (a) {
      var tr = document.createElement("tr");
      tr.appendChild(el("td", null, a.role));
      var c = el("td");
      if (a.cost_usd === null || a.cost_usd === undefined) {
        c.appendChild(unk("no cost recorded for this agent"));
      } else {
        var bar = el("span", "bar ultra");
        bar.style.width = Math.max(1, Math.round((a.cost_usd / max) * 50)) + "px";
        c.appendChild(bar);
        c.appendChild(document.createTextNode(" " + money(a.cost_usd)));
      }
      tr.appendChild(c);
      tr.appendChild(put(el("td"), num(a.calls)));
      tr.appendChild(put(el("td"), num(a.tokens_in)));
      tr.appendChild(put(el("td"), num(a.tokens_out)));
      body.appendChild(tr);
    });
  }

  // ---- the ledger's own confession --------------------------------------

  function renderShape(p) {
    var host = $(".shape");
    host.textContent = "";
    var shape = p.ledger_shape;
    if (!shape) return;
    var gaps = [];
    Object.keys(shape.tables || {}).forEach(function (k) {
      var t = shape.tables[k];
      if (!t.present) gaps.push("table '" + t.table + "' is missing entirely");
      (t.missing || []).forEach(function (m) { gaps.push(k + "." + m); });
    });
    if (!gaps.length) return;
    var w = el("div", "warn");
    w.appendChild(el("strong", null, "This ledger does not answer everything the dashboard asks. "));
    w.appendChild(document.createTextNode(
      "Fields below render as em-dashes rather than zeros. Fix the CONTRACT dict in payload_builder.py, then re-run - nothing else changes."));
    var code = el("div");
    code.style.marginTop = "6px";
    code.appendChild(el("code", null, gaps.join("   /   ")));
    w.appendChild(code);
    host.appendChild(w);
  }

  // ---- sections that know when they have nothing to say ----------------

  function renderOptional(p) {
    // payload key null  -> this ledger has no such table. hide entirely.
    // payload key []    -> it has one, and it is empty. show, and say so.
    // Those are different facts. A hidden section says "we do not track this";
    // an empty one says "we track it and nothing happened". Conflating them is
    // the same lie as printing 0 for a cost we never recorded.
    //
    // A whole PAGE can go this way too - and when it does its nav tab goes with
    // it. A tab leading to an empty page is worse than no tab.
    Array.prototype.forEach.call(document.querySelectorAll("[data-needs]"),
      function (sec) {
        var missing = p[sec.dataset.needs] === null ||
                      p[sec.dataset.needs] === undefined;
        if (sec.classList.contains("page")) sec.dataset.hidden = missing ? "true" : "false";
        else sec.hidden = missing;
      });
  }

  function fillTable(bodyId, rows, emptyMsg, cols) {
    var body = document.getElementById(bodyId);
    if (!body) return;
    body.textContent = "";
    if (!rows || !rows.length) {
      var tr = document.createElement("tr");
      var td = el("td", "empty-cell", emptyMsg);
      td.colSpan = cols;
      tr.appendChild(td);
      body.appendChild(tr);
      return body;
    }
    return body;
  }

  // Insert a one-time explanatory line above a tab's table. render() runs again
  // on every poll, so guard against inserting it twice.
  function introOnce(bodyId, cls, text) {
    var body = document.getElementById(bodyId);
    if (!body || !body.closest) return;
    var table = body.closest("table");
    if (!table || !table.parentNode) return;
    var prev = table.previousElementSibling;
    if (prev && prev.classList && prev.classList.contains(cls)) return;
    table.parentNode.insertBefore(el("p", "tab-intro " + cls, text), table);
  }

  // V4.4: the pure prompt-table seam - which versions are in view under
  // the agent/stage/model filters, plus the DERIVED option lists.
  function promptsModel(p, f) {
    f = f || {};
    var rows = (p && p.prompt_versions) || [];
    var optAgent = {};
    var optStage = {};
    var optModel = {};
    var out = [];
    rows.forEach(function (v) {
      var stages = (v.stages && v.stages.length)
        ? v.stages : (v.stage ? [v.stage] : []);
      if (v.agent) optAgent[v.agent] = 1;
      stages.forEach(function (s) { optStage[s] = 1; });
      (v.models || []).forEach(function (m) { optModel[m] = 1; });
      var ok = (!f.agent || v.agent === f.agent)
        && (!f.stage || stages.indexOf(f.stage) >= 0)
        && (!f.model || (v.models || []).indexOf(f.model) >= 0);
      if (ok) out.push(v);
    });
    return { rows: out, total: rows.length,
             options: { agent: Object.keys(optAgent).sort(),
                        stage: Object.keys(optStage).sort(),
                        model: Object.keys(optModel).sort() } };
  }

  function renderPromptsBar(p) {
    var host = document.querySelector(".prompts-bar");
    if (!host) return;
    host.classList.add("tux-bar");
    host.textContent = "";
    host.appendChild(el("span", "tux-level", "filters - counts are ticket-keyed prompt rows"));
    var rows = p.prompt_versions;
    if (!rows || !rows.length) return;
    var all = promptsModel(p, {});
    function addSel(label, key, opts) {
      var s = el("select", "percall-sel prompts-sel-" + key);
      s.setAttribute("aria-label", "prompt " + label);
      var o0 = el("option", null, label + ": all");
      o0.value = "";
      s.appendChild(o0);
      opts.forEach(function (v) {
        var o = el("option", null, v);
        o.value = v;
        if (state.promptF[key] === v) o.selected = true;
        s.appendChild(o);
      });
      s.addEventListener("change", function () {
        state.promptF[key] = s.value;
        renderPrompts(state.payload);
      });
      host.appendChild(s);
    }
    addSel("agent", "agent", all.options.agent);
    addSel("stage", "stage", all.options.stage);
    addSel("model", "model", all.options.model);
  }

  function renderPrompts(p) {
    introOnce("prompt-body", "prompt-intro",
      "Every prompt change bumps a version - that is the rule this table pays " +
      "off. For each version it shows what it carries (base and delta), who " +
      "runs it, how many calls it drove, and how the tickets it touched " +
      "decided. If a version's merge rate rises after a prompt change, the " +
      "change probably helped; if it falls, look there first. It is " +
      "correlation, not proof - too much moves at once to say a version " +
      "caused a merge - but it tells you where to look.");
    var rows = p.prompt_versions;
    if (rows === null || rows === undefined) return;
    var m = promptsModel(p, state.promptF);
    var body = fillTable("prompt-body", rows,
      "No event carries a prompt_version.", 12);
    if (!rows || !rows.length) return;
    body.textContent = "";
    if (!m.rows.length) {
      var etr = document.createElement("tr");
      var etd = el("td", "empty",
        "No version matches the filters (" + m.total
        + " versions in the payload).");
      etd.colSpan = 12;
      etr.appendChild(etd);
      body.appendChild(etr);
      return;
    }
    m.rows.forEach(function (v) {
      var tr = document.createElement("tr");
      var vc = el("td", "txt pv-version", v.version);
      if ((v.calls || 0) < 5) {
        var tag = el("span", "gate-optin pv-small", "small sample");
        tag.title = "fewer than 5 calls - the rate is an anecdote, "
          + "not a signal";
        vc.appendChild(tag);
      }
      tr.appendChild(vc);
      tr.appendChild(put(el("td", "txt"), v.base, "no base recorded"));
      tr.appendChild(put(el("td", "txt"), v.delta, "no delta recorded"));
      tr.appendChild(put(el("td"), v.agent, "no agent stamped"));
      var stages = (v.stages && v.stages.length)
        ? v.stages : (v.stage ? [v.stage] : []);
      tr.appendChild(put(el("td", "txt"),
        stages.length ? stages.join(", ") : null,
        "no stage recorded"));
      tr.appendChild(put(el("td", "txt"),
        (v.models || []).length ? v.models.join(", ") : null,
        "no model recorded"));
      tr.appendChild(put(el("td"), num(v.calls)));
      var tk = put(el("td"), num(v.runs));
      tk.title = "ticket-keyed: distinct issue keys under this version, "
        + "never run ids";
      tr.appendChild(tk);
      var mg = put(el("td"), num(v.merged), "merge fact not recorded");
      mg.title = "ticket-keyed, same basis as Tickets touched";
      tr.appendChild(mg);
      tr.appendChild(put(el("td"), pct(v.merge_rate),
        "no touched ticket has decided under this version - there is "
        + "no rate, not a 0% rate"));
      tr.appendChild(put(el("td"), num(v.tokens_in), "no count"));
      tr.appendChild(put(el("td"), money(v.cost_usd),
        "no call priced under this version"));
      body.appendChild(tr);
    });
  }

  function renderModels(p) {
    var rows = p.models;
    if (rows === null || rows === undefined) return;
    var body = fillTable("model-body", rows, "No event records a model.", 4);
    if (!rows || !rows.length) return;
    var max = Math.max.apply(null, rows.map(function (m) {
      return m.cost_usd || 0;
    }).concat([0.0001]));
    rows.forEach(function (m) {
      var tr = document.createElement("tr");
      tr.appendChild(el("td", null, m.model));
      var c = el("td");
      if (m.cost_usd === null || m.cost_usd === undefined) {
        c.appendChild(unk("no cost recorded for this model"));
      } else {
        var bar = el("span", "bar ultra");
        bar.style.width = Math.max(1, Math.round((m.cost_usd / max) * 44)) + "px";
        c.appendChild(bar);
        c.appendChild(document.createTextNode(" " + money(m.cost_usd)));
      }
      tr.appendChild(c);
      tr.appendChild(put(el("td"), num(m.calls)));
      tr.appendChild(put(el("td"), money(m.cost_per_call)));
      body.appendChild(tr);
    });
  }

  function renderArtifacts(p) {
    var rows = p.artifact_kinds;
    if (rows === null || rows === undefined) return;
    var body = fillTable("artifact-body", rows,
      "The artifacts table exists but is empty. Nothing has been written yet.", 4);
    if (!rows || !rows.length) return;
    rows.forEach(function (a) {
      var tr = document.createElement("tr");
      tr.appendChild(el("td", null, a.kind));
      tr.appendChild(put(el("td"), num(a.count)));
      tr.appendChild(put(el("td"), num(a.tickets)));
      tr.appendChild(put(el("td"), bytes(a.bytes)));
      body.appendChild(tr);
    });
  }

  function bytes(v) {
    if (v === null || v === undefined) return null;
    if (v < 1024) return v + " B";
    if (v < 1048576) return (v / 1024).toFixed(1) + " KB";
    return (v / 1048576).toFixed(1) + " MB";
  }

  // ---- V4.4 evidence browser ---------------------------------------------
  //
  // Every attempt's artifact rows, flattened, filterable and searchable.
  // The population is honest both ways: `retained` counts the rows the
  // payload actually ships, `total` is DERIVED from artifact_kinds (the
  // aggregate the builder computed over every scoped record) - never a
  // snapshot literal, and null when the aggregate itself is absent.
  function artifactsBrowserModel(p, f) {
    f = f || {};
    var q = String(f.q || "").toLowerCase();
    var optKind = {};
    var optTicket = {};
    var rows = [];
    var all = 0;
    ((p && p.tickets) || []).forEach(function (t) {
      (t.runs || []).forEach(function (r) {
        (r.artifacts || []).forEach(function (a) {
          all += 1;
          optKind[a.kind] = 1;
          optTicket[a.issue] = 1;
          var hay = (String(a.rel_path || "") + " " + String(a.kind || "")
                     + " " + String(a.run || "") + " "
                     + String(a.actor || "")).toLowerCase();
          var ok = (!f.kind || a.kind === f.kind)
            && (!f.ticket || a.issue === f.ticket)
            && (!q || hay.indexOf(q) >= 0);
          if (ok) rows.push(a);
        });
      });
    });
    var kinds = (p && p.artifact_kinds) || null;
    var total = null;
    if (kinds) {
      total = 0;
      kinds.forEach(function (k) { total += k.count || 0; });
    }
    return { rows: rows, retained: all, total: total,
             options: { kind: Object.keys(optKind).sort(),
                        ticket: Object.keys(optTicket).sort() } };
  }

  var artCountEl = null;

  function currentArtFilters() {
    return { kind: state.artKind, ticket: state.artTicket,
             q: state.artQ };
  }

  function updateArtCount(p) {
    if (!artCountEl || !p) return;
    var m = artifactsBrowserModel(p, currentArtFilters());
    artCountEl.textContent = m.rows.length + " of " + m.retained
      + " retained rows match"
      + (m.total != null
         ? " - the aggregate above covers " + num(m.total)
           + " scoped records (derived from artifact_kinds)"
         : "");
  }

  function renderArtBar(p) {
    var host = document.querySelector(".artbrowse-bar");
    if (!host) return;
    host.classList.add("tux-bar");
    host.textContent = "";
    host.appendChild(el("span", "tux-level", "filters - counts are retained artifact rows"));
    var all = artifactsBrowserModel(p, {});
    if (!all.retained) return;
    artCountEl = el("span", "percall-count");
    host.appendChild(artCountEl);
    updateArtCount(p);
    var q = el("input", "tux-q artbrowse-q");
    q.type = "search";
    q.value = state.artQ || "";
    q.placeholder = "path / kind / run / actor";
    q.setAttribute("aria-label",
      "Search retained artifact rows by path, kind, run or actor");
    q.addEventListener("input", function () {
      state.artQ = q.value;
      renderArtBrowser(state.payload);
    });
    host.appendChild(q);
    function addSel(label, key, opts) {
      var s = el("select", "percall-sel artbrowse-sel-" + key);
      s.setAttribute("aria-label", "artifact " + label);
      var o0 = el("option", null, label + ": all");
      o0.value = "";
      s.appendChild(o0);
      opts.forEach(function (v) {
        var o = el("option", null, v);
        o.value = v;
        if ((key === "kind" ? state.artKind : state.artTicket) === v) {
          o.selected = true;
        }
        s.appendChild(o);
      });
      s.addEventListener("change", function () {
        if (key === "kind") state.artKind = s.value;
        else state.artTicket = s.value;
        renderArtBrowser(state.payload);
      });
      host.appendChild(s);
    }
    addSel("kind", "kind", all.options.kind);
    addSel("ticket", "ticket", all.options.ticket);
  }

  function renderArtBrowser(p) {
    updateArtCount(p);
    var host = document.querySelector(".artbrowse-host");
    if (!host) return;
    host.textContent = "";
    var m = artifactsBrowserModel(p, currentArtFilters());
    if (!m.retained) {
      host.appendChild(el("div", "empty",
        "No artifact rows in this payload."));
      return;
    }
    var wrap = el("div", "gate-scroll");
    var table = el("table", "grid");
    table.appendChild(el("caption", "srx", "Artifact evidence rows"));
    var thead = el("thead");
    var hr = el("tr");
    ["Kind", "Relative path", "Run", "Actor", "Bytes", "At",
     "Sha256 (Copy for full)", "Host-open"].forEach(function (h) {
      hr.appendChild(el("th", null, h));
    });
    thead.appendChild(hr);
    table.appendChild(thead);
    var tbody = el("tbody");
    var CAP = 50;
    m.rows.slice(0, CAP).forEach(function (a) {
      var tr = el("tr");
      tr.appendChild(el("td", null, a.kind));
      // The path is TEXT, never an anchor: an href built from a ledger
      // row is exactly the traversal the containment work closed.
      var pathTd = el("td",
        "txt" + (a.escapes_workspace ? " art-unsafe" : ""), a.rel_path);
      if (a.escapes_workspace) {
        pathTd.title = "recorded path resolves outside the workspace";
      }
      tr.appendChild(pathTd);
      tr.appendChild(el("td", "txt", String(a.run || "").slice(-8)));
      tr.appendChild(put(el("td"), a.actor, "no actor recorded"));
      tr.appendChild(put(el("td"), bytes(a.bytes), "size not recorded"));
      tr.appendChild(el("td", "txt", String(a.at || "").slice(0, 16)));
      var shaTd = el("td", "txt art-sha");
      if (a.sha256) {
        shaTd.appendChild(el("span", "art-sha-prefix",
          String(a.sha256).slice(0, 10)));
        var btn = el("button", "act art-copy", "Copy sha256");
        btn.title = "copies the full 64-character sha256";
        btn.addEventListener("click", function () {
          copySha(btn, shaTd, a.sha256);
        });
        shaTd.appendChild(btn);
      } else {
        shaTd.appendChild(unk("no sha256 recorded for this row"));
      }
      tr.appendChild(shaTd);
      var open = el("td", "txt");
      if (a.escapes_workspace) {
        open.appendChild(el("span", "art-ineligible",
          "ineligible - escapes the workspace"));
      } else {
        open.textContent =
          "eligible in the VS Code host (containment holds)";
      }
      tr.appendChild(open);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    host.appendChild(wrap);
    if (m.rows.length > CAP) {
      host.appendChild(el("div", "gc-more",
        "+ " + (m.rows.length - CAP)
        + " more matched rows not shown (display cap)"));
    }
  }

  // The ONE clipboard helper. A clipboard the host refuses (or never
  // granted) fails HONESTLY - the button says so; a silent no-op would
  // claim a copy that never happened. onDone/onFail let callers add
  // their own honesty (the sha copier reveals the full hash).
  function copyText(btn, text, onDone, onFail) {
    function failed() {
      btn.textContent = "copy failed - select the text";
      if (onFail) onFail();
    }
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard
          && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          btn.textContent = "copied";
          if (onDone) onDone();
        }, failed);
      } else {
        failed();
      }
    } catch (e) {
      failed();
    }
  }

  // Copy the FULL sha; on failure the full 64 characters are revealed
  // beside the button so the reader can select them.
  function copySha(btn, cell, sha) {
    copyText(btn, sha, function () {
      btn.textContent = "copied (64 chars)";
    }, function () {
      btn.textContent = "copy failed - select it:";
      cell.appendChild(el("span", "art-sha-full", " " + sha));
    });
  }

  // ---- V4.4 Reference: TOPOLOGY-derived blocks ---------------------------
  //
  // The Reference page is server-rendered by extra_tabs.py, but its state
  // vocabularies and compact pipeline figure are rebuilt HERE from
  // TOPOLOGY - the same one authority the Architecture tab draws from -
  // so there is never a second hand-maintained copy to drift.

  function renderRefVocab() {
    var host = document.querySelector(".ref-vocab");
    if (!host) return;
    host.textContent = "";
    var v = TOPOLOGY.vocab || {};
    function fam(title, vals, note) {
      var box = el("div", "rv-fam");
      box.appendChild(el("div", "rv-title", title));
      var line = el("div", "rv-vals");
      (vals || []).forEach(function (w) {
        line.appendChild(el("span", "rv-val", w));
      });
      box.appendChild(line);
      if (note) box.appendChild(el("div", "rv-note", note));
      host.appendChild(box);
    }
    fam("run outcomes (ledger.RUN_OUTCOMES)", v.run_outcomes);
    fam("workflow delivery states (workflow.STATES)", v.workflow_states);
    fam("gate outcomes (ledger + schema CHECK)", v.gate_outcomes,
        v.gate_outcome_note);
    fam("run monitor UI projection", v.ui_states);
    if (v.vocab_note) host.appendChild(el("div", "rv-note", v.vocab_note));
  }

  var STAGE_ABBR_REF = { comprehension: "COMP", blast_radius: "RAD",
    plan: "PLAN", test_spec: "SPEC", develop: "DEV", blind_review: "REV",
    security: "SEC", qa: "QA", mutation: "MUT" };

  function renderRefTopology() {
    var host = document.querySelector(".ref-topology");
    if (!host) return;
    host.textContent = "";
    var NS = "http://www.w3.org/2000/svg";
    var stages = TOPOLOGY.stages || [];
    if (!stages.length) return;
    var nodeW = 96;
    var gap = 18;
    var y = 78;
    var w = 16 + stages.length * (nodeW + gap) - gap;
    var wrap = el("div", "ref-topo-scroll");
    var svg = document.createElementNS(NS, "svg");
    svg.setAttribute("class", "archsvg");
    svg.setAttribute("viewBox", "0 0 " + w + " 210");
    svg.setAttribute("width", "100%");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label",
      "Pipeline: forward flow with the repair loops and the human lane; "
      + "the Architecture tab holds the complete interactive map");
    function mk(tag, attrs, text) {
      var n = document.createElementNS(NS, tag);
      Object.keys(attrs).forEach(function (k) {
        n.setAttribute(k, attrs[k]);
      });
      if (text != null) n.textContent = text;
      svg.appendChild(n);
      return n;
    }
    mk("rect", { x: 8, y: 8, width: w - 16, height: 30, rx: 6,
                 "class": "rtp-human" });
    mk("text", { x: w / 2, y: 27, "text-anchor": "middle",
                 "class": "rtp-text" },
       "human lane: clarifying questions - plan approval - context "
       + "ratification - Ship");
    stages.forEach(function (st, i) {
      var x = 8 + i * (nodeW + gap);
      var box = mk("rect", { x: x, y: y, width: nodeW, height: 40,
                             rx: 6, "class": "rtp-stage" });
      var title = document.createElementNS(NS, "title");
      title.textContent = st.label
        + (st.gate ? " - gate " + st.gate : " - no gate row");
      box.appendChild(title);
      mk("text", { x: x + nodeW / 2, y: y + 24, "text-anchor": "middle",
                   "class": "rtp-abbr" },
         STAGE_ABBR_REF[st.id] || st.id.slice(0, 4).toUpperCase());
      if (i < stages.length - 1) {
        mk("line", { x1: x + nodeW, y1: y + 20, x2: x + nodeW + gap,
                     y2: y + 20, "class": "rtp-flow" });
      }
      // the human lane hooks: questions (comprehension), plan approval
      // (plan), Ship (mutation exit) - the stages whose gate is a human
      // decision.
      if (i === 0 || i === 2 || i === stages.length - 1) {
        mk("line", { x1: x + nodeW / 2, y1: 38, x2: x + nodeW / 2,
                     y2: y, "class": "rtp-hook" });
      }
    });
    // loop labels come from TOPOLOGY.loops - the same names the
    // Architecture tab tables.
    function loopName(prefix) {
      var hit = (TOPOLOGY.loops || []).filter(function (x) {
        return x.name.indexOf(prefix) >= 0;
      })[0];
      return hit ? hit.name : prefix;
    }
    function loopArc(fromI, toI, label, yy) {
      var x1 = 8 + fromI * (nodeW + gap) + nodeW / 2;
      var x2 = 8 + toI * (nodeW + gap) + nodeW / 2;
      mk("path", { d: "M " + x1 + " " + (y + 40) + " C " + x1 + " " + yy
                     + ", " + x2 + " " + yy + ", " + x2 + " " + (y + 40),
                   fill: "none", "class": "rtp-loop" });
      mk("text", { x: (x1 + x2) / 2, y: yy - 4, "text-anchor": "middle",
                   "class": "rtp-loop-l" }, label);
    }
    loopArc(5, 4, loopName("Blind-review repair") + ": request_changes",
            158);
    loopArc(7, 4, loopName("QA repair"), 176);
    loopArc(8, 3, loopName("Mutation strengthen") + ": Survivor", 194);
    wrap.appendChild(svg);
    host.appendChild(wrap);
  }

  // ---- V4.4 Ledger: measured database facts ------------------------------

  function renderDbFacts(p) {
    var host = document.querySelector("#db-facts");
    if (!host) return;
    host.textContent = "";
    var df = p.db_facts;
    if (!df) return;
    function row(label, valueEl, note) {
      var r = el("div", "acct-row");
      r.appendChild(el("span", "acct-l", label));
      var v = el("span", "acct-v dbf-v");
      if (typeof valueEl === "object" && valueEl !== null) {
        v.appendChild(valueEl);
      } else {
        v.textContent = valueEl;
      }
      r.appendChild(v);
      r.appendChild(el("span", "acct-n", note));
      host.appendChild(r);
    }
    row("journal mode",
        df.journal_mode == null
          ? unk("the journal-mode PRAGMA could not be read")
          : df.journal_mode,
        "measured with PRAGMA journal_mode by the payload builder - the "
        + "schema declares WAL, but a declaration is not a measurement");
    row("lock / reader state",
        unk("not measured - lock state is transient and a payload "
          + "snapshot cannot report it; a live host could"),
        "unavailable, never invented");
    row("last write seen",
        df.last_write_seen == null
          ? unk("the events table records no timestamp yet")
          : df.last_write_seen,
        "the events table's own max timestamp - a lower bound (other "
        + "tables can be written without an event)");
    row("database size",
        df.db_bytes == null
          ? unk("the file size could not be measured")
          : bytes(df.db_bytes),
        df.page_size != null
          ? "measured on disk; page size " + num(df.page_size) + " bytes"
          : "measured on disk");
    row("generated",
        p.generated_at || "-",
        (p.generated_by || "") + ". The dashboard is not the only "
        + "reader of this database.");
    // The same gap derivation renderShape() uses on Overview - one rule,
    // two surfaces.
    var gaps = 0;
    var shp = p.ledger_shape || {};
    Object.keys(shp.tables || {}).forEach(function (k) {
      var t = shp.tables[k];
      if (!t.present) gaps += 1;
      gaps += (t.missing || []).length;
    });
    row("contract",
        "schema v" + p.schema,
        gaps
          ? "ledger-shape probe found " + gaps
            + " gap(s) - see the Overview warning"
          : "ledger-shape probe: every curated mapping matched");
  }

  // ---- the rest of the ledger -------------------------------------------

  function renderInventory(p) {
    var host = $(".inventory");
    if (!host) return;
    host.textContent = "";
    var rows = (p.inventory || []).filter(function (t) { return !t.curated; });
    if (!rows.length) {
      host.appendChild(el("div", "empty",
        "Every table in this ledger already has a purpose-built panel above."));
      return;
    }
    host.appendChild(el("div", "inv-lede",
      "Everything else this ledger records. The panels above cover runs, gates, " +
      "events and artifacts; these are the other tables the pipeline writes. " +
      "Each says what it holds, how many rows it has, and - where a column has " +
      "only a few distinct values - a breakdown of them."));
    rows.forEach(function (t) {
      var card = el("div", "inv-card panel");

      var head = el("div", "inv-head");
      head.appendChild(el("span", "inv-name", t.table));
      // curated-vs-found: a described table went through the curated
      // mapping; anything else was DISCOVERED in the database - the
      // approved design tags the difference on every card.
      var desc = TABLE_INFO[t.table];
      head.appendChild(el("span", "inv-tag", desc ? "curated" : "found"));
      head.appendChild(el("span", "inv-rows",
        (t.rows === null ? "unknown" : Number(t.rows).toLocaleString()) + " rows"));
      card.appendChild(head);

      card.appendChild(el("div", "inv-desc" + (desc ? "" : " inv-undesc"),
        desc || "A table this ledger records; no description on file yet."));

      // Say what could not be worked out. A table quietly missing from the
      // drill-down is worse than a table that explains why it is not there.
      if (!t.joinable) {
        card.appendChild(el("div", "inv-note inv-join", t.note ||
          "cannot be tied to a run"));
      } else {
        card.appendChild(el("div", "inv-note inv-join",
          "joined to runs on " + t.key_column + " - see any ticket's drill-down"));
      }

      if (!t.enums || !t.enums.length) {
        card.appendChild(el("div", "inv-note",
          "no low-cardinality columns to break down"));
      }
      (t.enums || []).forEach(function (e) {
        card.appendChild(el("div", "inv-col", e.column));
        var max = Math.max.apply(null, e.values.map(function (v) { return v.count; }));
        e.values.forEach(function (v) {
          var r = el("div", "inv-bar-row");
          r.appendChild(el("span", "inv-val", String(v.value)));
          var track = el("span", "inv-track");
          var bar = el("span", "inv-bar");
          bar.style.width = Math.max(2, Math.round((v.count / max) * 100)) + "%";
          track.appendChild(bar);
          r.appendChild(track);
          r.appendChild(el("span", "inv-n", String(v.count)));
          card.appendChild(r);
        });
      });

      host.appendChild(card);
    });
  }

  function relatedBlock(name, rows) {
    var wrap = el("div");
    wrap.appendChild(el("div", "sub-head", name));
    var cols = Object.keys(rows[0]);
    var tbl = document.createElement("table");
    tbl.className = "grid rel";
    tbl.appendChild(el("caption", "srx", "Related ledger rows for this attempt"));
    var thead = document.createElement("thead");
    var htr = document.createElement("tr");
    cols.forEach(function (c) { htr.appendChild(el("th", null, c)); });
    thead.appendChild(htr);
    tbl.appendChild(thead);
    var tb = document.createElement("tbody");
    rows.forEach(function (r) {
      var tr = document.createElement("tr");
      cols.forEach(function (c) {
        var td = el("td");
        var v = r[c];
        if (v === null || v === undefined) td.appendChild(unk());
        else td.textContent = String(v);
        tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    wrap.appendChild(tbl);
    return wrap;
  }



  // ==================================================================
  // V4.4: the FINDINGS TAB - the approved investigation workspace over
  // payload.kernel. findingsTabModel is the PURE seam (executed by
  // report.py --self-test): it computes every header metric and repair
  // split from kernel rows and never names a status - a ledger that
  // grows a status renders it with no edit here. All DOM below is
  // createElement/textContent: kernel strings are ledger evidence.
  // ==================================================================
  var FX_UNAVAILABLE =
    "Unavailable - this ledger records no workflow kernel (or no " +
    "findings table), so nothing was measured.";
  var FX_EMPTY =
    "No findings recorded - the kernel is here and its findings list " +
    "is empty.";

  function findingsTabModel(kernel) {
    if (!kernel || kernel.findings === null
        || kernel.findings === undefined) {
      return { state: "unavailable", message: FX_UNAVAILABLE };
    }
    var rows = kernel.findings || [];
    var byStatus = {}, byVerdict = {}, byKind = {};
    var withVerdict = 0, superseded = 0;
    rows.forEach(function (f) {
      var s = f.status == null ? "(no status)" : String(f.status);
      byStatus[s] = (byStatus[s] || 0) + 1;
      if (f.verdict != null) {
        withVerdict += 1;
        var v = String(f.verdict);
        byVerdict[v] = (byVerdict[v] || 0) + 1;
      }
      if (f.supersedes != null) superseded += 1;
      var k = f.kind == null ? "(no kind)" : String(f.kind);
      byKind[k] = (byKind[k] || 0) + 1;
    });
    var reps = kernel.repairs || [];
    var conv = 0, didNot = 0;
    reps.forEach(function (r) {
      if (r.converted === 1 || r.converted === true) conv += 1;
      else if (r.converted === 0 || r.converted === false) didNot += 1;
    });
    return {
      state: rows.length ? "counts" : "empty",
      message: rows.length ? "" : FX_EMPTY,
      total: rows.length,
      with_verdict: withVerdict,
      lacking_verdict: rows.length - withVerdict,
      superseded: superseded,
      by_status: tally(byStatus),
      by_verdict: tally(byVerdict),
      by_kind: tally(byKind),
      repairs: { attempts: reps.length, converted: conv, did_not: didNot,
                 open: reps.length - conv - didNot },
      // the chain's honesty labels: the findings schema records no
      // workflow_id or failure_id, so finding -> workflow is a ticket
      // join (DERIVED) and workflow -> failure is a candidate join
      // (DERIVED); only repair_attempts.failure_id is a RECORDED link.
      chain_provenance: { workflow: "derived", failure: "derived",
                          repair: "recorded" }
    };
  }

  var fxState = { sel: null, q: "", status: "all", verdict: "all",
                  kind: "all", ticket: "all", sort: "newest",
                  wired: false };

  function fxSlug(s) {
    return String(s).toLowerCase().replace(/[^a-z0-9]+/g, "_");
  }

  function fxRows(kernel) {
    var rows = (kernel.findings || []).slice();
    var q = fxState.q.toLowerCase();
    rows = rows.filter(function (f) {
      if (fxState.status !== "all"
          && String(f.status) !== fxState.status) return false;
      if (fxState.verdict !== "all") {
        if (fxState.verdict === "(none)") {
          if (f.verdict != null) return false;
        } else if (String(f.verdict) !== fxState.verdict) return false;
      }
      if (fxState.kind !== "all"
          && String(f.kind) !== fxState.kind) return false;
      if (fxState.ticket !== "all"
          && String(f.ticket_id) !== fxState.ticket) return false;
      if (q) {
        var hay = [f.ticket_id, f.kind, f.status, f.verdict, f.summary,
                   f.run_id].join(" ").toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    });
    var dir = fxState.sort === "oldest" ? 1 : -1;
    rows.sort(function (a, b) {
      if (fxState.sort === "ticket") {
        return String(a.ticket_id).localeCompare(String(b.ticket_id));
      }
      if (fxState.sort === "status") {
        return String(a.status).localeCompare(String(b.status));
      }
      if (fxState.sort === "kind") {
        return String(a.kind).localeCompare(String(b.kind));
      }
      return dir * String(a.created_at || "")
        .localeCompare(String(b.created_at || ""));
    });
    return rows;
  }

  function fxProv(kind) {
    var s = el("span", "prov" + (kind === "derived" ? " derived" : ""),
               kind);
    s.title = kind === "derived"
      ? "computed by joining recorded rows - the ledger does not record " +
        "this link directly"
      : "a recorded ledger fact";
    return s;
  }

  function fxField(host, label, value, provKind, why) {
    var row = el("div", "fxd-field");
    row.appendChild(el("span", "fxd-l", label));
    var v = el("span", "fxd-v");
    if (value === null || value === undefined || value === "") {
      v.appendChild(unk(why || "not recorded"));
    } else {
      v.appendChild(document.createTextNode(String(value)));
    }
    if (provKind) v.appendChild(fxProv(provKind));
    row.appendChild(v);
    host.appendChild(row);
  }

  function fxChainNode(cls, kindLabel, text, title) {
    var n = el("div", "fxc-node " + cls);
    n.appendChild(el("span", "fxc-k", kindLabel));
    n.appendChild(document.createTextNode(text));
    if (title) n.title = title;
    return n;
  }

  function renderFindingsTab(p) {
    var host = document.querySelector(".findings-tab");
    if (!host) return;
    var kernel = p ? p.kernel : null;
    var m = findingsTabModel(kernel);
    host.textContent = "";
    if (m.state === "unavailable" || m.state === "empty") {
      host.appendChild(el("div", "empty findings-" +
        (m.state === "unavailable" ? "unavailable" : "empty"), m.message));
      return;
    }
    var K = kernel;
    var meta = K.meta || {};
    var pops = meta.populations || {};

    function popLine(name) {
      var pp = pops[name] || {};
      if (pp.retained == null) return name + ": unavailable";
      var bits = name + ": " + pp.retained + " retained";
      if (pp.total_in_scope != null && pp.total_in_scope !== pp.retained) {
        bits += " of " + pp.total_in_scope + " in scope";
      }
      if (pp.total_in_ledger != null
          && pp.total_in_ledger !== pp.total_in_scope) {
        bits += " (" + pp.total_in_ledger + " in the whole ledger)";
      }
      return bits;
    }

    // ---- 1. the command header ------------------------------------
    var cmd = el("div", "panel f-cmd");
    function metric(label, val, cls, why) {
      var s = el("div", "astat");
      var v = el("span", "astat-v" + (cls ? " " + cls : ""));
      put(v, val === null ? null : num(val), why);
      s.appendChild(v);
      s.appendChild(el("span", "astat-l", label));
      cmd.appendChild(s);
    }
    metric("recorded findings", m.total);
    metric("with a taxonomy verdict", m.with_verdict);
    metric("lacking a verdict", m.lacking_verdict);
    metric("superseded", m.superseded);
    metric("repair attempts", m.repairs.attempts);
    metric("converted", m.repairs.converted);
    metric("did not convert", m.repairs.did_not);
    metric("open", m.repairs.open, m.repairs.open ? "is-open" : "");
    host.appendChild(cmd);
    var popNote = el("div", "tab-intro");
    popNote.textContent = "Computed over the retained kernel rows - "
      + ["findings", "repairs", "failures", "workflows"].map(popLine)
        .join("; ") + ". " + (meta.scope_note || "");
    host.appendChild(popNote);

    // ---- 2. lifecycle and taxonomy, apart -------------------------
    var dist = el("div", "panel f-life-dist");
    function distRows(rows, label) {
      if (!rows.length) return;
      dist.appendChild(el("div", "eyebrow", label));
      var mx = Math.max.apply(null, rows.map(function (r) {
        return r.count; }).concat([1]));
      rows.forEach(function (r) {
        var row = el("div", "fld-row s-" + fxSlug(r.status));
        row.appendChild(el("span", "fld-name", r.status));
        var track = el("span", "fld-track");
        var bar = el("span", "fld-bar");
        bar.style.width = Math.round((r.count / mx) * 100) + "%";
        track.appendChild(bar);
        row.appendChild(track);
        row.appendChild(el("span", "fld-n", String(r.count)));
        dist.appendChild(row);
      });
    }
    distRows(m.by_status,
      "lifecycle - aggregate state distribution (the ledger records " +
      "each finding's current status, never a claim about how findings " +
      "moved between states)");
    distRows(m.by_verdict, "taxonomy verdict - a second vocabulary over " +
      "the same rows, never summed with the lifecycle");
    host.appendChild(dist);

    // ---- 3. the explorer -------------------------------------------
    host.appendChild(el("div", "sub-head",
      "The explorer - queue, evidence, resolution chain"));
    var bar = el("div", "tux-bar fchips fx-filterbar");
    bar.appendChild(el("span", "tux-level",
      "filters - combined; counts are retained findings"));
    function sel_(name, label, values, current) {
      var w = el("label", "fx-filter");
      w.appendChild(el("span", "astat-l", label + " "));
      var s = document.createElement("select");
      s.dataset.fxsel = name;
      ["all"].concat(values).forEach(function (v) {
        var o = document.createElement("option");
        o.value = v; o.textContent = v;
        if (v === current) o.selected = true;
        s.appendChild(o);
      });
      w.appendChild(s);
      bar.appendChild(w);
    }
    function uniq(key) {
      var seen = {};
      (K.findings || []).forEach(function (f) {
        var v = f[key];
        seen[v == null ? "(none)" : String(v)] = 1;
      });
      return Object.keys(seen).sort();
    }
    sel_("status", "status", uniq("status"), fxState.status);
    sel_("verdict", "verdict", uniq("verdict"), fxState.verdict);
    sel_("kind", "kind", uniq("kind"), fxState.kind);
    sel_("ticket", "ticket", uniq("ticket_id"), fxState.ticket);
    var sortW = el("label", "fx-filter");
    sortW.appendChild(el("span", "astat-l", "sort "));
    var sortS = document.createElement("select");
    sortS.dataset.fxsort = "1";
    [["newest", "newest first"], ["oldest", "oldest first"],
     ["ticket", "by ticket"], ["status", "by status"],
     ["kind", "by kind"]].forEach(function (o2) {
      var o = document.createElement("option");
      o.value = o2[0]; o.textContent = o2[1];
      if (o2[0] === fxState.sort) o.selected = true;
      sortS.appendChild(o);
    });
    sortW.appendChild(sortS);
    bar.appendChild(sortW);
    var qw = el("label", "fx-filter");
    qw.appendChild(el("span", "astat-l", "search "));
    var qi = el("input", "tux-q");
    qi.type = "search"; qi.value = fxState.q;
    qi.dataset.fxq = "1";
    qi.setAttribute("aria-label", "Search findings");
    qw.appendChild(qi);
    bar.appendChild(qw);
    host.appendChild(bar);

    var rows = fxRows(K);
    var grid = el("div", "fx-grid");

    // queue
    var queue = el("div", "panel fx-queue");
    queue.setAttribute("role", "listbox");
    queue.setAttribute("aria-label", "Finding queue");
    var count = el("div", "tab-intro");
    count.textContent = rows.length + " of " + (K.findings || []).length
      + " retained findings match the combined filters";
    queue.appendChild(count);
    rows.forEach(function (f) {
      var b = el("button", "fx-row");
      b.type = "button";
      b.dataset.fid = String(f.finding_id);
      b.setAttribute("aria-pressed",
        fxState.sel === String(f.finding_id) ? "true" : "false");
      var top = el("div", "fxr-top");
      top.appendChild(el("span", "fxr-id",
        "#" + f.finding_id + " " + (f.ticket_id || "")));
      top.appendChild(el("span", "fxr-meta",
        [f.status, f.verdict, f.kind].filter(function (x) {
          return x != null; }).join(" / ")));
      b.appendChild(top);
      b.appendChild(el("div", "fxr-sum", f.summary || ""));
      queue.appendChild(b);
    });
    if (!rows.length) {
      queue.appendChild(el("div", "empty",
        "No finding matches the combined filters."));
    }
    grid.appendChild(queue);

    // detail
    var detail = el("div", "panel fx-detail");
    var selRow = null;
    (K.findings || []).forEach(function (f) {
      if (String(f.finding_id) === fxState.sel) selRow = f;
    });
    if (!selRow) {
      detail.appendChild(el("div", "empty",
        "Select a finding from the queue - its full evidence body, " +
        "provenance-tagged relationships and resolution chain fill in " +
        "here."));
    } else {
      detail.appendChild(el("div", "sub-head",
        "Finding #" + selRow.finding_id));
      fxField(detail, "ticket", selRow.ticket_id, "recorded");
      fxField(detail, "run", selRow.run_id, "recorded");
      fxField(detail, "recorded at", selRow.created_at, "recorded");
      fxField(detail, "kind", selRow.kind, "recorded");
      fxField(detail, "lifecycle", selRow.status, "recorded");
      fxField(detail, "verdict", selRow.verdict, "recorded",
        "no taxonomy verdict recorded for this finding");
      fxField(detail, "supersedes", selRow.supersedes, "recorded",
        "supersedes no earlier finding");
      var act = el("div", "fxd-field");
      act.appendChild(el("span", "fxd-l", "actor"));
      var av = el("span", "fxd-v");
      av.appendChild(el("em", null,
        "not recorded on the finding row - the findings schema has no " +
        "actor column"));
      act.appendChild(av);
      detail.appendChild(act);
      detail.appendChild(el("div", "sub-head", "Evidence"));
      var ev = el("pre", "fxd-evidence");
      ev.textContent = selRow.evidence || "";
      if (!selRow.evidence) {
        detail.appendChild(el("div", "empty",
          "No evidence body recorded."));
      } else {
        detail.appendChild(ev);
        var cap = (((K.meta || {}).caps || {}).finding_evidence_chars);
        if (cap && selRow.evidence.length >= cap) {
          detail.appendChild(el("div", "tab-intro",
            "carried at the declared " + cap + "-character cap; the " +
            "full body stays in the ledger"));
        }
      }
    }
    grid.appendChild(detail);

    // chain
    var chain = el("div", "panel fx-chain");
    chain.appendChild(el("div", "sub-head", "Resolution chain"));
    if (!selRow) {
      chain.appendChild(el("div", "empty",
        "The chain follows the selected finding: workflows (derived by " +
        "ticket), typed failures, repair attempts (recorded join) and " +
        "the disposition."));
    } else {
      var wfs = (K.workflows || []).filter(function (w) {
        return w.ticket_id === selRow.ticket_id;
      }).sort(function (a, b) {
        var c = String(a.created_at).localeCompare(String(b.created_at));
        return c !== 0 ? c
          : String(a.workflow_id).localeCompare(String(b.workflow_id));
      });
      if (!wfs.length) {
        chain.appendChild(fxChainNode("c-missing", "workflows",
          "No recorded link - no kernel workflow exists for this " +
          "finding's ticket"));
      } else {
        wfs.forEach(function (w, i) {
          var latest = i === wfs.length - 1;
          chain.appendChild(fxChainNode(latest ? "c-conv" : "",
            "workflow (derived by ticket)",
            w.workflow_id + " - " + w.state
            + (latest ? " (latest)" : " (superseded)")));
        });
      }
      var wfIds = {};
      wfs.forEach(function (w) { wfIds[w.workflow_id] = 1; });
      var fails = (K.failures || []).filter(function (fl) {
        return wfIds[fl.workflow_id];
      });
      chain.appendChild(el("div", "fxc-arrow", "v"));
      if (!fails.length) {
        chain.appendChild(fxChainNode("c-missing",
          "failures (derived)", "No recorded link - no typed failure on " +
          "this ticket's workflows"));
      } else {
        fails.forEach(function (fl) {
          chain.appendChild(fxChainNode("", "failure (derived candidate)",
            "#" + fl.failure_id + " " + (fl.failure_class || "")
            + " at " + (fl.source_stage || "?")
            + (fl.owner ? " - owner " + fl.owner : "")));
        });
      }
      chain.appendChild(el("div", "fxc-arrow", "v"));
      var failIds = {};
      fails.forEach(function (fl) { failIds[fl.failure_id] = 1; });
      var reps2 = (K.repairs || []).filter(function (r) {
        return failIds[r.failure_id];
      });
      if (!reps2.length) {
        chain.appendChild(fxChainNode("c-missing",
          "repairs (recorded join)", "No recorded link - no repair " +
          "attempt cites these failures"));
      } else {
        reps2.forEach(function (r) {
          var cls = (r.converted === 1 || r.converted === true)
            ? "c-conv" : (r.converted === 0 || r.converted === false)
              ? "c-notconv" : "c-open";
          chain.appendChild(fxChainNode(cls, "repair (recorded)",
            (r.strategy || "?") + " - "
            + ((r.converted === 1 || r.converted === true) ? "converted"
              : (r.converted === 0 || r.converted === false)
                ? "did not convert"
                : "open - not a no-op, the attempt is still unresolved")));
        });
      }
      chain.appendChild(el("div", "fxc-arrow", "v"));
      var dispo = wfs.length ? wfs[wfs.length - 1].state : null;
      chain.appendChild(fxChainNode("", "disposition",
        dispo ? "latest workflow state: " + dispo
              : "No recorded link - no workflow, no disposition"));
    }
    grid.appendChild(chain);
    host.appendChild(grid);

    // ---- 4. repair economics --------------------------------------
    host.appendChild(el("div", "sub-head",
      "Repair economics - by strategy (correlation, not causation)"));
    var byStrat = {};
    (K.repairs || []).forEach(function (r) {
      var s = byStrat[r.strategy || "(no strategy)"] = byStrat[
        r.strategy || "(no strategy)"] || { n: 0, conv: 0, notconv: 0,
          open: 0, rech: 0, classes: {}, stages: {} };
      s.n += 1;
      if (r.converted === 1 || r.converted === true) s.conv += 1;
      else if (r.converted === 0 || r.converted === false) s.notconv += 1;
      else s.open += 1;
      if (r.rechecks != null && r.rechecks !== "") s.rech += 1;
      (K.failures || []).forEach(function (fl) {
        if (fl.failure_id === r.failure_id) {
          if (fl.failure_class) s.classes[fl.failure_class] = 1;
          if (fl.source_stage) s.stages[fl.source_stage] = 1;
        }
      });
    });
    var stratNames = Object.keys(byStrat).sort();
    var bars = el("div", "panel rx-bars");
    var mx2 = Math.max.apply(null, stratNames.map(function (s2) {
      return byStrat[s2].n; }).concat([1]));
    stratNames.forEach(function (s2) {
      var d = byStrat[s2];
      var row = el("div", "rx-row");
      row.appendChild(el("span", "rx-name", s2));
      var track = el("span", "rx-track");
      [["conv", d.conv], ["notconv", d.notconv],
       ["open", d.open]].forEach(function (seg) {
        if (!seg[1]) return;
        var sp = el("span", "rx-seg " + seg[0]);
        sp.style.width = Math.max(2,
          Math.round((seg[1] / mx2) * 100)) + "%";
        sp.title = seg[1] + " " + (seg[0] === "conv" ? "converted"
          : seg[0] === "notconv" ? "did not convert"
          : "open - not a no-op");
        track.appendChild(sp);
      });
      row.appendChild(track);
      row.appendChild(el("span", "rx-fig",
        d.conv + " conv / " + d.notconv + " not / " + d.open
        + " open of " + d.n
        + " - rechecks on " + d.rech
        + (Object.keys(d.classes).length
           ? " - " + Object.keys(d.classes).sort().join(",") : "")
        + (Object.keys(d.stages).length
           ? " - " + Object.keys(d.stages).sort().join(",") : "")));
      bars.appendChild(row);
    });
    if (!stratNames.length) {
      bars.appendChild(el("div", "empty", "No repair attempts recorded."));
    }
    host.appendChild(bars);

    // ---- 5. the complete browsers ---------------------------------
    function browser(title, list, cols, rowFn, popName) {
      host.appendChild(el("div", "sub-head", title));
      host.appendChild(el("div", "tab-intro", popLine(popName)));
      var panel = el("div", "panel");
      var tbl = document.createElement("table");
      tbl.className = "grid";
      var thead = document.createElement("thead");
      var tr = document.createElement("tr");
      cols.forEach(function (c) { tr.appendChild(el("th", null, c)); });
      thead.appendChild(tr);
      tbl.appendChild(thead);
      var tb = document.createElement("tbody");
      var shown = (list || []).slice(0, 100);
      shown.forEach(function (r) { tb.appendChild(rowFn(r)); });
      if (!(list || []).length) {
        var er = document.createElement("tr");
        var td = el("td", "empty-cell", "nothing recorded");
        td.colSpan = cols.length;
        er.appendChild(td);
        tb.appendChild(er);
      }
      tbl.appendChild(tb);
      panel.appendChild(tbl);
      if ((list || []).length > shown.length) {
        panel.appendChild(el("div", "tab-intro",
          "showing the first " + shown.length + " of "
          + list.length + " retained rows"));
      }
      host.appendChild(panel);
    }
    function td_(v, why) {
      var t = el("td");
      if (v === null || v === undefined || v === "") {
        t.appendChild(unk(why || "not recorded"));
      } else t.textContent = String(v);
      return t;
    }
    browser("All retained findings", K.findings,
      ["id", "at", "ticket", "kind", "lifecycle", "verdict", "summary"],
      function (f) {
        var tr = document.createElement("tr");
        tr.appendChild(td_(f.finding_id));
        tr.appendChild(td_(f.created_at));
        tr.appendChild(td_(f.ticket_id));
        tr.appendChild(td_(f.kind));
        tr.appendChild(td_(f.status));
        tr.appendChild(td_(f.verdict, "no verdict recorded"));
        tr.appendChild(td_(f.summary));
        return tr;
      }, "findings");
    browser("All retained failures", K.failures,
      ["id", "at", "workflow", "stage", "class", "owner", "retryable"],
      function (f) {
        var tr = document.createElement("tr");
        tr.appendChild(td_(f.failure_id));
        tr.appendChild(td_(f.at));
        tr.appendChild(td_(f.workflow_id));
        tr.appendChild(td_(f.source_stage));
        tr.appendChild(td_(f.failure_class));
        tr.appendChild(td_(f.owner));
        tr.appendChild(td_(f.retryable == null ? null
          : (f.retryable ? "yes" : "no")));
        return tr;
      }, "failures");
    browser("All retained repair attempts", K.repairs,
      ["id", "workflow", "failure", "strategy", "converted", "rechecks"],
      function (r) {
        var tr = document.createElement("tr");
        tr.appendChild(td_(r.attempt_id));
        tr.appendChild(td_(r.workflow_id));
        tr.appendChild(td_(r.failure_id));
        tr.appendChild(td_(r.strategy));
        tr.appendChild(td_(
          (r.converted === 1 || r.converted === true) ? "converted"
            : (r.converted === 0 || r.converted === false)
              ? "did not convert" : null,
          "open - no outcome recorded yet"));
        tr.appendChild(td_(r.rechecks, "no rechecks recorded"));
        return tr;
      }, "repairs");
    browser("All retained workflow transitions", K.transitions,
      ["workflow", "from", "to", "reason", "at"],
      function (t2) {
        var tr = document.createElement("tr");
        tr.appendChild(td_(t2.workflow_id));
        tr.appendChild(td_(t2.from_state));
        tr.appendChild(td_(t2.to_state));
        tr.appendChild(td_(t2.reason));
        tr.appendChild(td_(t2.at));
        return tr;
      }, "transitions");
  }

  function wireFindingsTab() {
    var host = document.querySelector(".findings-tab");
    if (!host || fxState.wired) return;
    fxState.wired = true;
    host.addEventListener("click", function (e) {
      var t = e.target;
      while (t && t !== host) {
        if (t.dataset && t.dataset.fid) {
          fxState.sel = fxState.sel === t.dataset.fid
            ? null : t.dataset.fid;
          renderFindingsTab(state.payload);
          return;
        }
        t = t.parentNode;
      }
    });
    host.addEventListener("change", function (e) {
      var t = e.target;
      if (t.dataset && t.dataset.fxsel) {
        fxState[t.dataset.fxsel] = t.value;
        renderFindingsTab(state.payload);
      } else if (t.dataset && t.dataset.fxsort) {
        fxState.sort = t.value;
        renderFindingsTab(state.payload);
      }
    });
    host.addEventListener("input", function (e) {
      var t = e.target;
      if (t.dataset && t.dataset.fxq) {
        fxState.q = t.value;
        renderFindingsTab(state.payload);
      }
    });
  }

  // ---- router -----------------------------------------------------------
  //
  // Hash routing, not a framework. #/runs is a real URL: back button works,
  // deep links work, and the whole thing survives being emailed as one file
  // and opened from a Downloads folder with no server behind it. A router
  // that needs a server is a router this dashboard cannot use.

  function pages() {
    return Array.prototype.slice.call(document.querySelectorAll(".page"));
  }

  function pageId(p) { return p.id.replace(/^page-/, ""); }

  function buildNav() {
    var host = $(".nav-in");
    if (!host) return;
    host.textContent = "";
    pages().forEach(function (p) {
      if (p.dataset.hidden === "true") return;
      var b = el("button", "tab", p.dataset.title || pageId(p));
      b.type = "button";
      b.setAttribute("role", "tab");
      b.dataset.page = pageId(p);
      b.addEventListener("click", function () {
        location.hash = "#/" + pageId(p);
      });
      host.appendChild(b);
    });
  }

  function route(navigated) {
    var want = (location.hash || "").replace(/^#\/?/, "") || "overview";
    var all = pages();
    var target = all.filter(function (p) {
      return pageId(p) === want && p.dataset.hidden !== "true";
    })[0] || all.filter(function (p) { return p.dataset.hidden !== "true"; })[0];
    if (!target) return;
    all.forEach(function (p) { p.classList.toggle("on", p === target); });
    // V4.4: ONLY the Architecture tab escapes the content column - a
    // dedicated body class, never a weakened global width constraint.
    if (document.body && document.body.classList) {
      document.body.classList.toggle("arch-active",
        pageId(target) === "architecture");
    }
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (t) {
      var on = t.dataset.page === pageId(target);
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.classList.toggle("on", on);
    });
    // Landing at the top is right for a NAVIGATION and wrong for anything
    // else. The live server re-renders every few seconds; scrolling to top on
    // each of those would yank the page out from under someone mid-read, once
    // per poll, forever. Only an actual hash change scrolls.
    if (navigated) window.scrollTo(0, 0);
  }

  // ---- KPI tiles --------------------------------------------------------

  var FMT = {
    money: function (v) { return money(v); },
    pct: function (v) { return pct(v); },
    int: function (v) { return num(v === null ? null : Math.round(v)); },
    hours: function (v) { return hours(v); }
  };

  function fmtDelta(t) {
    var d = t.delta;
    if (d === null || d === undefined) return null;
    var s = d > 0 ? "+" : d < 0 ? "-" : "+/-";
    var a = Math.abs(d);
    if (t.format === "pct") return s + Math.round(a * 100) + " pts";
    if (t.format === "money") return s + "$" + a.toFixed(2);
    if (t.format === "hours") return s + hours(a);
    return s + num(Math.round(a));
  }

  function verdict(t) {
    // "better" or "worse" is only sayable when there IS a better direction.
    // Two of these KPIs do not have one, and colouring them would teach the
    // reader that a comprehension gate stopping a bad ticket is a bad day.
    if (t.direction === "ambiguous" || !t.delta) return "";
    var better = t.direction === "lower_better" ? t.delta < 0 : t.delta > 0;
    return better ? "good" : "bad";
  }

  function renderKpis(p) {
    var host = $(".kpis");
    if (!host) return;
    var k = p.kpis || {};
    var scope = $(".kpi-scope");
    if (scope) {
      scope.textContent = k.previous
        ? k.current + " compared with " + k.previous
        : (k.current || "") + " - no earlier release in this ledger to compare against";
    }
    host.textContent = "";
    (k.tiles || []).forEach(function (t) {
      var card = el("div", "kpi panel" + (t.direction === "ambiguous" ? " amb" : ""));
      card.appendChild(el("div", "kpi-label", t.label));
      var v = el("div", "kpi-value");
      put(v, FMT[t.format](t.value), "not recorded in the ledger");
      card.appendChild(v);

      var foot = el("div", "kpi-foot");
      var d = fmtDelta(t);
      if (d === null) {
        foot.appendChild(el("span", "kpi-delta none", "no prior release"));
      } else {
        foot.appendChild(el("span", "kpi-delta " + verdict(t), d));
        foot.appendChild(el("span", "kpi-vs", "vs " + k.previous));
      }
      card.appendChild(foot);

      if (t.note) {
        var q = el("button", "kpi-why", "why this has no verdict");
        q.type = "button";
        q.title = t.note;
        q.addEventListener("click", function () {
          var open = card.querySelector(".kpi-note");
          if (open) { open.remove(); return; }
          card.appendChild(el("div", "kpi-note", t.note));
        });
        card.appendChild(q);
      }
      host.appendChild(card);
    });
  }

  // ---- trend ------------------------------------------------------------

  var TRENDS = [
    ["comprehension_halt_rate", "Stopped at comprehension", "pct"],
    ["first_pass_rate", "First pass", "pct"],
    ["cost_per_ticket", "Cost per ticket", "money"],
    ["median_cycle_hours", "Median cycle", "hours"]
  ];

  function renderTrends(p) {
    var host = $(".trends");
    if (!host) return;
    host.textContent = "";
    var t = p.trend || [];
    if (t.length < 2) {
      host.appendChild(el("div", "empty",
        "One release in this ledger. A trend needs at least two."));
      return;
    }
    TRENDS.forEach(function (spec) {
      var pts = t.map(function (r) { return r[spec[0]]; });
      var card = el("div", "trend panel");
      card.appendChild(el("div", "kpi-label", spec[1]));
      if (pts.every(function (v) { return v === null || v === undefined; })) {
        // show the metric anyway, so all four read as a set - just say why it is
        // blank rather than dropping it and leaving a hole the reader wonders at
        card.appendChild(el("div", "trend-empty",
          "not recorded in this ledger"));
      } else {
        card.appendChild(spark(t, pts, spec[2]));
      }
      host.appendChild(card);
    });
  }

  function spark(rows, pts, fmt) {
    // Hand-rolled SVG. A charting library would be a CDN dependency, and this
    // file has to open on a plane.
    var W = 260, H = 74, PAD = 6;
    var real = pts.filter(function (v) { return v !== null && v !== undefined; });
    var lo = Math.min.apply(null, real), hi = Math.max.apply(null, real);
    if (hi === lo) { hi = lo + 1; lo = lo - 1; }
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("class", "spark");
    svg.setAttribute("role", "img");

    function x(i) { return PAD + (i * (W - PAD * 2)) / Math.max(1, pts.length - 1); }
    function y(v) { return H - 22 - ((v - lo) / (hi - lo)) * (H - 34); }

    var d = "", started = false;
    pts.forEach(function (v, i) {
      if (v === null || v === undefined) return;
      d += (started ? "L" : "M") + x(i) + " " + y(v) + " ";
      started = true;
    });
    var path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d.trim());
    path.setAttribute("class", "spark-line");
    svg.appendChild(path);

    pts.forEach(function (v, i) {
      if (v === null || v === undefined) return;
      var c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("cx", x(i));
      c.setAttribute("cy", y(v));
      c.setAttribute("r", i === pts.length - 1 ? 3 : 2);
      c.setAttribute("class", "spark-dot" + (i === pts.length - 1 ? " last" : ""));
      var title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = rows[i].release + ": " + FMT[fmt](v);
      c.appendChild(title);
      svg.appendChild(c);
    });

    [[0, "start"], [pts.length - 1, "end"]].forEach(function (e) {
      var t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t.setAttribute("x", e[1] === "start" ? PAD : W - PAD);
      t.setAttribute("y", H - 4);
      t.setAttribute("text-anchor", e[1]);
      t.setAttribute("class", "spark-ax");
      t.textContent = rows[e[0]].release;
      svg.appendChild(t);
    });

    var lastv = null;
    for (var i = pts.length - 1; i >= 0; i--) {
      if (pts[i] !== null && pts[i] !== undefined) { lastv = pts[i]; break; }
    }
    var lab = document.createElementNS("http://www.w3.org/2000/svg", "text");
    lab.setAttribute("x", W - PAD);
    lab.setAttribute("y", 12);
    lab.setAttribute("text-anchor", "end");
    lab.setAttribute("class", "spark-now");
    lab.textContent = FMT[fmt](lastv);
    svg.appendChild(lab);
    return svg;
  }

  // ---- chips ------------------------------------------------------------

  function buildChips(p) {
    // One chip per status actually present, plus All. Built from the data so a
    // status the code has never heard of still gets a filter - the fix for the
    // review that found 'escalated'/'ambiguous' had no chip and no tile.
    //
    // When runs outnumber tickets (the same ticket run many times), filter by
    // RUN-level status: a ticket matches if ANY of its runs had that status.
    // Otherwise a ledger with one merged ticket over 20 escalated attempts
    // would offer only a 'Merged' chip and hide the escalations entirely.
    var host = document.querySelector(".chips");
    if (!host) return;
    host.textContent = "";
    var tot = p.totals || {};
    var runLevel = (tot.run_total || 0) > (tot.tickets || 0);
    var counts = (runLevel ? tot.run_outcome_counts : tot.outcome_counts) || {};
    var order = (runLevel ? tot.run_outcomes : tot.outcomes) || [];
    state.filterLevel = runLevel ? "run" : "ticket";
    var LABELS = { halted: "Awaiting a human" };

    function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

    var defs = [["all", "All", p.tickets ? p.tickets.length : 0]];
    order.forEach(function (o) {
      defs.push([o, LABELS[o] || cap(o), counts[o] || 0]);
    });

    defs.forEach(function (d) {
      var c = el("button", "chip", null);
      c.type = "button";
      c.dataset.filter = d[0];
      c.setAttribute("aria-pressed", d[0] === state.filter ? "true" : "false");
      c.appendChild(document.createTextNode(d[1] + " "));
      c.appendChild(el("span", "chip-n", String(d[2])));
      host.appendChild(c);
    });
    wireChips();
  }

  function wireChips() {
    var chips = document.querySelectorAll(".chip");
    Array.prototype.forEach.call(chips, function (c) {
      c.addEventListener("click", function () {
        state.filter = c.dataset.filter;
        state.open = null;
        Array.prototype.forEach.call(chips, function (o) {
          o.setAttribute("aria-pressed", o === c ? "true" : "false");
        });
        renderWalk(state.payload);
      });
    });
  }

  function $(sel) { return document.querySelector(sel); }

  // ---- entry ------------------------------------------------------------

  function render(payload) {
    state.payload = payload;
    buildChips(payload);
    renderOptional(payload);
    buildNav();
    route(false);   // a re-render is not a navigation
    renderKpis(payload);
    renderTrends(payload);
    renderLead(payload);
    renderNowLine(payload);
    renderNeedsYou(payload);
    renderVerdictLine(payload);
    renderRunsToolbar(payload);
    renderWalk(payload);
    renderRunsAttempts(payload);
    renderTaxonomy(payload);
    renderFindings(payload);
    wireFindingsTab();
    renderFindingsTab(payload);
    renderGates(payload);
    renderGateLegend(payload);
    renderAgentsStats(payload);
    renderAgentsBar(payload);
    renderAgentRoster(payload);
    renderArchitecture(payload);
    renderAccounting(payload);
    renderCoverage(payload);
    renderTokenFlow(payload);
    renderUsageBreaks(payload);
    renderCallBar(payload);
    renderCallExplorer(payload);
    renderStageEcon(payload);
    renderScopeCost(payload);
    renderAgents(payload);
    renderPromptsBar(payload);
    renderPrompts(payload);
    renderModels(payload);
    renderArtifacts(payload);
    renderArtBar(payload);
    renderArtBrowser(payload);
    renderDbFacts(payload);
    renderRefVocab();
    renderRefTopology();
    renderInventory(payload);
    renderShape(payload);
  }

  function boot() {
    window.addEventListener("hashchange", function () { route(true); });
    // Delegated Copy for server-rendered lines (the Reference tab's CLI
    // and config rows carry data-copy). Guarded on the attribute so it
    // collides with nothing else - the data-showall lesson.
    document.addEventListener("click", function (e) {
      var t = e.target;
      if (!t || !t.getAttribute) return;
      var text = t.getAttribute("data-copy");
      if (text == null) return;
      copyText(t, text);
    });
    // Host 2: report.py inlined the payload before this file ever ran.
    if (window.DOCKET_PAYLOAD) render(window.DOCKET_PAYLOAD);
    // Host 1: the webview posts it, and posts it again on every gate.
    // Host state (process liveness + the run the host's projection names)
    // rides beside the payload, or arrives alone when only IT changed -
    // the now-line re-folds without a full re-render.
    window.addEventListener("message", function (e) {
      if (!e.data) return;
      if (e.data.type === "payload") {
        if (e.data.host !== undefined) window.DOCKET_HOST = e.data.host;
        render(e.data.payload);
      } else if (e.data.type === "host") {
        window.DOCKET_HOST = e.data.host;
        if (state.payload) renderNowLine(state.payload);
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  // render() is the host entry point. findingsView / verdictView are exported
  // beside it because they are the two decisions this file makes that a test
  // can check without a browser - see report.py --self-test, which loads this
  // file under node and calls them directly.
  window.DocketDashboard = {
    render: render,
    findingsView: findingsView,
    verdictView: verdictView,
    findingsTabModel: findingsTabModel,
    nowLineModel: nowLineModel,
    needsYouModel: needsYouModel,
    runsFilterModel: runsFilterModel,
    openAttempt: openAttempt,
    callExplorerModel: callExplorerModel,
    stageEconModel: stageEconModel,
    promptsModel: promptsModel,
    agentsModel: agentsModel,
    artifactsBrowserModel: artifactsBrowserModel,
    copyText: copyText,
    // V4.4 test seams: the topology authority and the player, exposed so
    // report.py --self-test and dashboard_host.js can EXECUTE the
    // architecture contract instead of grepping for it.
    topology: TOPOLOGY,
    archScenarios: SCENARIOS_ARCH,
    stationLabelBoxes: stationLabelBoxes,
    archPlayer: function () { return player; },
    archState: function () { return archState; },
    archRedraw: redrawArch
  };
})();
