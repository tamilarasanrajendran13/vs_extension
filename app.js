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
    comprehension: "COMP", context: "CTX", plan: "PLAN", "test-spec": "SPEC",
    develop: "DEV", review: "REV", security: "SEC", qa: "QA", mutation: "MUT"
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

  var state = { payload: null, filter: "all", open: null };

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
    var counts = t.outcome_counts || {};
    var order = t.outcomes || ["merged", "halted", "failed", "running"];
    var LABELS = { halted: "awaiting a human" };
    var CLS = { halted: "is-halt", failed: "is-fail" };
    var figs = [["tickets", num(t.tickets), ""]];
    order.forEach(function (o) {
      figs.push([LABELS[o] || o, num(counts[o] != null ? counts[o] : t[o]),
                 CLS[o] || ""]);
    });
    figs.push(["first pass", pct(t.first_pass_rate), ""]);
    figs.push(["median cycle", hours(t.median_cycle_hours), ""]);
    var host = $(".figures");
    host.textContent = "";
    figs.forEach(function (f) {
      var d = el("div", "figure");
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
          rtot + " runs across " + t.tickets + " ticket" + (t.tickets === 1 ? "" : "s") + ":");
        runStrip.appendChild(lead);
        ro.forEach(function (o) {
          var chip = el("span", "rss-chip v-" + o);
          chip.appendChild(el("span", "rss-dot", ""));
          chip.appendChild(document.createTextNode(
            " " + (o === "halted" ? "awaiting human" : o) + " "));
          chip.appendChild(el("span", "rss-n", String(rc[o])));
          runStrip.appendChild(chip);
        });
      } else {
        runStrip.hidden = true;
      }
    }
  }

  // ---- the gate walk ----------------------------------------------------

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

    var rows = (p.tickets || []).filter(function (t) {
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
      iss.appendChild(el("span", "sum", t.summary || ""));
      row.appendChild(iss);

      var track = el("div", "track");
      (t.gates || []).forEach(function (g) {
        var cell = el("div", "cell");
        var mk = el("span", "mark " + (g.halt ? "halt" : g.result));
        mk.title = g.name + ": " + (g.halt ? "awaiting a human" : g.result) +
          (g.detail ? " - " + g.detail : "");
        cell.appendChild(mk);
        track.appendChild(cell);
      });
      row.appendChild(track);

      var disp = el("div", "disposition");
      var verdict = t.outcome || "unknown";
      var label = verdict === "halted" ? "awaiting human" : verdict;
      disp.appendChild(el("span", "verdict " + verdict, label));

      // "clean run" is only true if every gate actually answered. A run that
      // merged with Snyk unreachable and mutmut timed out is not clean, it is
      // unmeasured, and a dashboard that calls it clean is doing the exact
      // thing the hollow marks exist to prevent.
      var mute = (t.gates || []).filter(function (g) { return g.result === "unknown"; });
      var why = t.reason;
      var whyCls = "why";
      if (!why && verdict === "merged") {
        if (mute.length) {
          why = "merged with " + mute.length + (mute.length === 1 ? " gate" : " gates") +
            " unmeasured: " + mute.map(function (g) { return g.name; }).join(", ");
          whyCls = "why unanswered";
        } else {
          why = "every gate answered";
        }
      }
      disp.appendChild(el("span", whyCls, why || ""));
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
          var tot = el("span", "cost-total", " / " + (total || "-") + " total");
          tot.title = "total across all " + t.run_count + " runs";
          cost.appendChild(tot);
        }
      } else {
        put(cost, money(t.cost_usd), "no cost recorded for this run");
      }
      disp.appendChild(cost);
      row.appendChild(disp);

      row.addEventListener("click", function () {
        state.open = state.open === t.issue ? null : t.issue;
        renderWalk(state.payload);
      });
      host.appendChild(row);

      if (state.open === t.issue) host.appendChild(detail(t));
    });
  }

  function detail(t) {
    var d = el("div", "detail");

    // ===== 1. WHAT HAPPENED - the sentence you read first =====
    if (t.narrative) {
      var narr = el("div", "narrative");
      narr.appendChild(el("span", "narr-dot v-" + (t.outcome || "unknown"), ""));
      narr.appendChild(el("span", "narr-text", t.narrative));
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
    fact("iterations", t.iterations != null ? String(t.iterations) : null);
    fact("cost", money(t.cost_usd));
    if (t.budget_usd) {
      var pctUsed = t.cost_usd != null ? Math.round((t.cost_usd / t.budget_usd) * 100) : null;
      fact("budget", "$" + t.budget_usd.toFixed(2) + (pctUsed != null ? " (" + pctUsed + "% used)" : ""),
           pctUsed != null && pctUsed > 80 ? "warn" : "");
    }
    fact("tokens", t.tokens_in != null ? num(t.tokens_in) + " in / " + (num(t.tokens_out) || "?") + " out" : null);
    fact("cycle", hours(t.cycle_hours));
    if (t.git_sha_start) fact("commit", t.git_sha_start.slice(0, 8));
    if (t.pr_url) {
      var prf = el("span", "fact");
      prf.appendChild(el("span", "fact-l", "PR"));
      var a = el("a", "fact-v fact-link", t.pr_url.replace(/^https?:\/\//, ""));
      a.href = t.pr_url; a.target = "_blank"; a.rel = "noopener";
      prf.appendChild(a);
      facts.appendChild(prf);
    }
    if (facts.children.length) d.appendChild(facts);

    // ===== 2. THE GATE JOURNEY - a real table, score vs threshold =====
    var ran = (t.gates || []).filter(function (g) { return g.result !== "never_reached"; });
    if (ran.length) {
      d.appendChild(el("div", "sub-head", "Gate journey"));
      var gt = el("div", "gate-journey");
      var gh = el("div", "gj-row head");
      ["Gate", "Verdict", "Score", "", "Took", "What it found"].forEach(function (h) {
        gh.appendChild(el("span", null, h));
      });
      gt.appendChild(gh);
      (t.gates || []).forEach(function (g) {
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

    if (t.timeline && t.timeline.length) {
      d.appendChild(el("div", "sub-head", "Who did what - " + t.timeline.length + " events"));
      var tl = el("div", "timeline");
      t.timeline.forEach(function (e) {
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
      if (t.timeline_truncated) {
        d.appendChild(el("div", "tl-more", "+ " + t.timeline_truncated + " more events (capped)."));
      }
    }

    // ===== 4. ARTIFACTS - what the run produced, grouped by run =====
    // A ticket run 14 times has 14 sets of artifacts. Showing them flat makes
    // 'context' appear 14 times with no way to tell which run made which. Group
    // under each run instead, newest first.
    var runsForArts = (t.runs && t.runs.length > 1) ? t.runs : [t];
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
          lbl.appendChild(el("span", "art-run-status v-" + (r.outcome || "unknown"),
            r.outcome === "halted" ? "awaiting human" : (r.outcome || "")));
          lbl.appendChild(el("span", "art-run-n", arts.length + " files"));
          d.appendChild(lbl);
        }
        var ar = el("div", "timeline art-group");
        arts.forEach(function (a) {
          var row = el("div", "tl-row art");
          row.appendChild(el("span", "tl-actor", a.kind || "?"));
          row.appendChild(el("span", "tl-what", a.rel_path || ""));
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
        var row = el("div", "run-row");
        row.appendChild(el("span", "run-when", (r.started || "").replace("T", " ").slice(5, 16)));
        row.appendChild(el("span", "run-status v-" + (r.outcome || "unknown"),
          r.outcome === "halted" ? "awaiting human" : (r.outcome || "\u2014")));
        row.appendChild(el("span", "run-gate", r.stopped_at || ""));
        row.appendChild(el("span", "run-why", r.reason || ""));
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

  // ---- gate ledger ------------------------------------------------------

  function renderAgentRoster(p) {
    var host = document.querySelector(".agent-grid");
    if (!host) return;
    host.textContent = "";
    (p.agents || []).forEach(function (a) {
      var card = el("div", "agent-card panel");
      var head = el("div", "agent-head");
      head.appendChild(el("span", "agent-role", a.title || a.role));
      if (a.stage) head.appendChild(el("span", "agent-stage", a.stage));
      card.appendChild(head);

      if (a.does) {
        card.appendChild(el("div", "agent-does", a.does));
      } else {
        card.appendChild(el("div", "agent-does agent-undesc",
          "In the ledger as '" + a.role + "' - no description on file. Add it to AGENT_INFO."));
      }

      if (a.reads || a.writes) {
        var io = el("div", "agent-io");
        if (a.reads) {
          var rd = el("div", "aio");
          rd.appendChild(el("span", "aio-l", "reads"));
          rd.appendChild(el("span", "aio-v", a.reads));
          io.appendChild(rd);
        }
        if (a.writes) {
          var wr = el("div", "aio");
          wr.appendChild(el("span", "aio-l", "writes"));
          wr.appendChild(el("span", "aio-v", a.writes));
          io.appendChild(wr);
        }
        card.appendChild(io);
      }

      // live stats from the ledger
      var stats = el("div", "agent-stats");
      function stat(label, val, unkMsg) {
        var s = el("div", "astat");
        s.appendChild(put(el("span", "astat-v"), val, unkMsg));
        s.appendChild(el("span", "astat-l", label));
        stats.appendChild(s);
      }
      stat("calls", num(a.calls));
      stat("cost", money(a.cost_usd), "not recorded");
      stat("tokens in", num(a.tokens_in), "not recorded");
      stat("tokens out", num(a.tokens_out), "not recorded");
      card.appendChild(stats);

      if (a.models && a.models.length) {
        var m = el("div", "agent-models");
        a.models.forEach(function (mo) { m.appendChild(el("span", "amodel", mo)); });
        card.appendChild(m);
      }
      host.appendChild(card);
    });
  }

  // Build the pipeline diagram as an SVG: the gate spine PLUS the loops that
  // make it a pipeline and not a straight line - a failed check routing back to
  // develop, a failing slice being coached and retried, an ambiguous ticket
  // going to the author, and any gate halting for a human. Positions are
  // computed here; colour and type come from .arch-svg rules in app.css.
  function buildArchSVG(spine, info) {
    function abbr(g) { return GATE_ABBR[g] || g.slice(0, 4).toUpperCase(); }
    function nameOf(g) { return (info[g] || {}).label || g; }

    var nodes = [{ k: "jira", ab: "JIRA", nm: "ticket in", kind: "io" }];
    spine.forEach(function (g) { nodes.push({ k: g, ab: abbr(g), nm: nameOf(g), kind: "gate" }); });
    nodes.push({ k: "merge", ab: "PR", nm: "merged", kind: "io" });
    var n = nodes.length;

    var W = 1240, H = 452, pad = 80;
    var gap = (W - pad * 2) / (n - 1);
    var NW = 82, NH = 46, spineY = 232;
    var top = spineY - NH / 2, bot = spineY + NH / 2;
    var cx = nodes.map(function (_, i) { return pad + i * gap; });
    var at = {};
    nodes.forEach(function (nd, i) { at[nd.k] = i; });
    function X(k) { return at[k] != null ? cx[at[k]] : null; }

    var S = [];
    S.push('<svg class="arch-svg" viewBox="0 0 ' + W + ' ' + H + '" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Docket pipeline flow with its loops">');
    S.push('<defs>' + mk("ah-fwd") + mk("ah-loop") + mk("ah-human") + '</defs>');

    // human lane (top): the author + you
    var barX1 = X("comprehension") - NW / 2, barX2 = X("merge") + NW / 2;
    var barY = 40, barH = 34, barBot = barY + barH;
    S.push('<rect class="arch-human-bar" x="' + barX1 + '" y="' + barY + '" width="' + (barX2 - barX1) + '" height="' + barH + '" rx="8"/>');
    S.push(txt((barX1 + barX2) / 2, barY + barH / 2 + 4, "arch-human-lbl",
      "You & the author - answer ambiguous tickets, ratify context, resolve halts, approve the merge"));
    humanUp("comprehension", "ask author");
    humanUp("context", "ratify");
    humanUp("merge", "approve");
    if (X("qa") != null) {
      S.push(pth("arch-halt", "M " + X("qa") + " " + top + " L " + X("qa") + " " + (barBot + 3), "ah-human"));
      S.push(txt(X("qa") + 5, (top + barBot) / 2, "arch-loop-lbl arch-start", "or halt"));
    }

    // the spine + forward arrows
    nodes.forEach(function (nd, i) {
      var x = cx[i];
      S.push('<rect class="arch-node k-' + nd.kind + '" x="' + (x - NW / 2) + '" y="' + top + '" width="' + NW + '" height="' + NH + '" rx="7"/>');
      S.push(txt(x, spineY - 3, "arch-node-ab", nd.ab));
      S.push(txt(x, spineY + 13, "arch-node-nm", nd.nm));
      if (i < n - 1) {
        S.push(pth("arch-fwd", "M " + (x + NW / 2) + " " + spineY + " L " + (cx[i + 1] - NW / 2) + " " + spineY, "ah-fwd"));
      }
    });

    // fallback loops (below): a failed check -> back to develop
    var dx = X("develop");
    if (dx != null) {
      var backs = ["review", "security", "qa", "mutation"], drawn = 0;
      backs.forEach(function (k) {
        var sx = X(k);
        if (sx == null) return;
        var depth = 40 + drawn * 24; drawn++;
        S.push(pth("arch-loop", "M " + sx + " " + bot + " C " + sx + " " + (bot + depth) + " " + dx + " " + (bot + depth) + " " + dx + " " + bot, "ah-loop"));
      });
      var deepest = bot + 40 + (drawn - 1) * 24;
      S.push(txt((dx + X("mutation")) / 2, deepest + 18, "arch-loop-lbl",
        "a failed check -> back to develop, fix, and re-run every gate after it"));

      // coaching self-loop on develop
      S.push(pth("arch-coach", "M " + (dx - 18) + " " + bot + " C " + (dx - 48) + " " + (bot + 32) + " " + (dx + 48) + " " + (bot + 32) + " " + (dx + 18) + " " + bot, "ah-loop"));
      S.push(txt(dx, bot + 32 + 15, "arch-loop-lbl", "slice fails -> lead coaches the worker -> retry"));
    }

    S.push('</svg>');
    return S.join("");

    function humanUp(k, label) {
      var x = X(k);
      if (x == null) return;
      S.push(pth("arch-human", "M " + x + " " + top + " L " + x + " " + (barBot + 3), "ah-human"));
      S.push(txt(x + 5, (top + barBot) / 2, "arch-loop-lbl arch-start", label));
    }
    function mk(id) {
      return '<marker id="' + id + '" class="mk-' + id + '" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z"/></marker>';
    }
    function pth(cls, d, mkid) {
      return '<path class="' + cls + '" d="' + d + '" fill="none" marker-end="url(#' + mkid + ')"/>';
    }
    function txt(x, y, cls, s) {
      return '<text class="' + cls + '" x="' + x + '" y="' + y + '">' + esc(s) + '</text>';
    }
    function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  }

  function renderArchitecture(p) {
    var host = document.querySelector(".arch");
    if (!host || host.dataset.built) return;
    host.dataset.built = "1";
    var order = p.gate_order || [];
    var info = p.gate_info || {};

    // 1. the pipeline, as a flow WITH its loops - not a straight line
    host.appendChild(el("div", "arch-h", "The pipeline"));
    host.appendChild(el("div", "arch-p",
      "A ticket flows left to right through the gates, but it rarely goes " +
      "straight through. A failed check sends it back to develop; an ambiguous " +
      "ticket goes back to the author; a failing slice is coached and retried; " +
      "any gate can halt and wait for you. Those loops are where quality is " +
      "enforced - the straight path is the exception, not the rule."));
    var spine = (order && order.length) ? order :
      ["comprehension", "context", "plan", "test-spec", "develop",
       "review", "security", "qa", "mutation"];
    var diagram = el("div", "arch-diagram");
    diagram.innerHTML = buildArchSVG(spine, info);
    host.appendChild(diagram);
    var leg = el("div", "arch-legend");
    [["lg-fwd", "forward - pass to the next gate"],
     ["lg-loop", "fallback - a failed check returns to develop"],
     ["lg-coach", "coaching - a failing slice is retried under the lead"],
     ["lg-human", "human - you or the author step in, or a gate halts"]
    ].forEach(function (l) {
      var it = el("div", "arch-lg-item");
      it.appendChild(el("span", "arch-lg-swatch " + l[0]));
      it.appendChild(el("span", "arch-lg-t", l[1]));
      leg.appendChild(it);
    });
    host.appendChild(leg);

    // 2. data flow
    host.appendChild(el("div", "arch-h", "Data flow"));
    host.appendChild(el("div", "arch-p",
      "The VS Code extension is the only component that can call a model " +
      "(vscode.lm). The Python loop orchestrates; every step is written to an " +
      "append-only SQLite ledger. This dashboard is a read-only view of that " +
      "ledger - it never calls a model and never writes."));
    var df = el("div", "arch-dataflow");
    [["Jira", "ticket + acceptance criteria"],
     ["VS Code extension", "the model gateway (vscode.lm)"],
     ["Python loop", "orchestrates the 9 gates"],
     ["ledger.db", "append-only record of everything"],
     ["this dashboard", "read-only view"]].forEach(function (s, i, arr) {
      var box = el("div", "df-box");
      box.appendChild(el("div", "df-name", s[0]));
      box.appendChild(el("div", "df-note", s[1]));
      df.appendChild(box);
      if (i < arr.length - 1) df.appendChild(el("span", "df-arrow", "\u2193"));
    });
    host.appendChild(df);

    // 3. RBAC / the governor
    host.appendChild(el("div", "arch-h", "Guardrails - the governor (RBAC)"));
    host.appendChild(el("div", "arch-p",
      "Every tool action an agent takes - every file edit, every shell command - " +
      "passes through the governor before it runs. The governor enforces the " +
      "blast radius the lead agent declared: it allows, asks, or denies."));
    var gov = el("div", "arch-rbac");
    [["allow", "within the declared blast radius - proceeds automatically"],
     ["ask", "borderline - pauses for human confirmation"],
     ["deny", "outside the blast radius - blocked outright"]].forEach(function (d) {
      var row = el("div", "rbac-row v-" + d[0]);
      row.appendChild(el("span", "rbac-decision", d[0]));
      row.appendChild(el("span", "rbac-desc", d[1]));
      row.appendChild(put(el("span", "rbac-count"),
        (p.governor && p.governor[d[0]] != null) ? num(p.governor[d[0]]) : null,
        "not recorded"));
      gov.appendChild(row);
    });
    host.appendChild(gov);
    host.appendChild(el("div", "arch-foot",
      "Because the governor sits on every action, an autonomous agent cannot " +
      "touch anything the lead did not sanction - and every allow/ask/deny is in " +
      "the ledger, visible per run."));
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
      tr.className = "gate-tr";

      // name only. The description goes on its own full-width row below, so a
      // long line can never bleed across the ran / passed / caught / score
      // columns the way it does when it lives in this narrow first cell.
      var nameCell = el("td", "gate-name-cell");
      nameCell.appendChild(el("div", "gate-full", g.label || g.name));
      tr.appendChild(nameCell);

      tr.appendChild(put(el("td"), num(g.ran), "never ran in scope"));
      tr.appendChild(put(el("td"), num(g.pass)));

      var caught = el("td", "caught" + (g.caught ? " has" : ""));
      caught.textContent = g.caught;
      caught.title = g.caught
        ? g.label + " stopped " + g.caught + " run(s) that every upstream gate let through"
        : "this gate has never stopped anything in scope";
      tr.appendChild(caught);

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

      tr.appendChild(put(el("td"), pct(g.pass_rate), "never ran in scope"));
      body.appendChild(tr);

      // description on its own row, spanning the full table width so it wraps
      // instead of overflowing the first column
      if (g.desc) {
        var dtr = document.createElement("tr");
        dtr.className = "gate-desc-tr";
        var dtd = el("td", "gate-desc");
        dtd.colSpan = 6;
        dtd.textContent = g.desc;
        dtr.appendChild(dtd);
        body.appendChild(dtr);
      }
    });
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

  function renderPrompts(p) {
    introOnce("prompt-body", "prompt-intro",
      "Every prompt change bumps a version - that is the rule this table pays " +
      "off. For each version it shows how many calls it drove, across how many " +
      "runs, and how many of those runs merged. If a version's merge rate rises " +
      "after a prompt change, the change probably helped; if it falls, look " +
      "there first. It is correlation, not proof - too much moves at once to say " +
      "a version caused a merge - but it tells you where to look.");
    var rows = p.prompt_versions;
    if (rows === null || rows === undefined) return;
    var body = fillTable("prompt-body", rows,
      "No event carries a prompt_version.", 5);
    if (!rows || !rows.length) return;
    rows.forEach(function (v) {
      var tr = document.createElement("tr");
      tr.appendChild(el("td", null, v.version));
      tr.appendChild(put(el("td"), num(v.calls)));
      tr.appendChild(put(el("td"), num(v.runs)));
      var m = el("td");
      m.textContent = v.merged + "/" + v.runs;
      tr.appendChild(m);
      tr.appendChild(put(el("td"), money(v.cost_per_call), "no cost on these events"));
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
      head.appendChild(el("span", "inv-rows",
        (t.rows === null ? "unknown" : Number(t.rows).toLocaleString()) + " rows"));
      card.appendChild(head);

      // what this table is FOR - the thing a bare bar chart never told you
      var desc = TABLE_INFO[t.table];
      card.appendChild(el("div", "inv-desc" + (desc ? "" : " inv-undesc"),
        desc || "A table this ledger records; no description on file yet."));

      // Say what could not be worked out. A table quietly missing from the
      // drill-down is worse than a table that explains why it is not there.
      if (!t.joinable) {
        card.appendChild(el("div", "inv-note", t.note ||
          "cannot be tied to a run"));
      } else {
        card.appendChild(el("div", "inv-note",
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
    renderWalk(payload);
    renderTaxonomy(payload);
    renderGates(payload);
    renderGateLegend(payload);
    renderAgentRoster(payload);
    renderArchitecture(payload);
    renderAgents(payload);
    renderPrompts(payload);
    renderModels(payload);
    renderArtifacts(payload);
    renderInventory(payload);
    renderShape(payload);
  }

  function boot() {
    window.addEventListener("hashchange", function () { route(true); });
    // Host 2: report.py inlined the payload before this file ever ran.
    if (window.DOCKET_PAYLOAD) render(window.DOCKET_PAYLOAD);
    // Host 1: the webview posts it, and posts it again on every gate.
    window.addEventListener("message", function (e) {
      if (e.data && e.data.type === "payload") render(e.data.payload);
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }

  window.DocketDashboard = { render: render };
})();
