"""Render a ComparisonResult to a single self-contained HTML file.

Inline CSS, no external assets, pure ASCII. Autoescaping is on so data values
are safe to embed. The report has a pass/fail banner, summary tiles, check
panels, and drill-down tables for mismatches, missing, and extra rows.
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, select_autoescape

from ..result import ComparisonResult

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>datacompare report - {{ r.name }}</title>
<style>
:root {
  --pass: #1a7f37; --pass-bg: #e6f4ea;
  --fail: #b42318; --fail-bg: #fbeae8;
  --ink: #1f2328; --muted: #656d76; --line: #d0d7de;
  --card: #ffffff; --bg: #f6f8fa; --accent: #0969da;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
  font-size: 14px; line-height: 1.5; }
.wrap { max-width: 1100px; margin: 0 auto; padding: 24px; }
.banner { border-radius: 10px; padding: 18px 22px; margin-bottom: 20px;
  display: flex; align-items: center; gap: 14px; font-size: 20px; font-weight: 700; }
.banner.pass { background: var(--pass-bg); color: var(--pass);
  border: 1px solid var(--pass); }
.banner.fail { background: var(--fail-bg); color: var(--fail);
  border: 1px solid var(--fail); }
.badge { font-size: 13px; font-weight: 600; padding: 2px 10px; border-radius: 20px;
  border: 1px solid currentColor; }
h1 { font-size: 18px; margin: 0 0 4px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 22px; }
.meta code { background: #eaeef2; padding: 1px 6px; border-radius: 4px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px; margin-bottom: 24px; }
.tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; }
.tile .n { font-size: 26px; font-weight: 700; }
.tile .l { color: var(--muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: .04em; }
.tile.warn .n { color: var(--fail); }
.tile.ok .n { color: var(--pass); }
section { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  margin-bottom: 20px; overflow: hidden; }
section > h2 { font-size: 15px; margin: 0; padding: 12px 16px; border-bottom: 1px solid var(--line);
  background: #f6f8fa; display: flex; justify-content: space-between; align-items: center; }
.pill { font-size: 12px; font-weight: 600; padding: 2px 9px; border-radius: 20px; }
.pill.pass { background: var(--pass-bg); color: var(--pass); }
.pill.fail { background: var(--fail-bg); color: var(--fail); }
.pill.off { background: #eaeef2; color: var(--muted); }
.body { padding: 12px 16px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line);
  vertical-align: top; word-break: break-word; }
th { color: var(--muted); font-weight: 600; background: #fbfcfd; position: sticky; top: 0; }
.scroll { max-height: 420px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
.src { color: var(--fail); }
.tgt { color: var(--pass); }
.mut { color: var(--muted); }
.empty { color: var(--muted); font-style: italic; padding: 8px 2px; }
.note { color: var(--muted); font-size: 12px; margin-top: 6px; }
footer { color: var(--muted); font-size: 12px; text-align: center; margin-top: 12px; }
code.k { background: #eaeef2; padding: 0 5px; border-radius: 4px; }
</style>
</head>
<body>
<div class="wrap">

  <div class="banner {{ 'pass' if r.passed else 'fail' }}">
    <span class="badge">{{ 'PASS' if r.passed else 'FAIL' }}</span>
    <span>{{ r.name }}</span>
  </div>

  <div class="meta">
    engine <code>{{ r.engine }}</code> &nbsp; format <code>{{ r.data_format }}</code>
    &nbsp; key <code>{{ r.key_columns | join(', ') }}</code>
    {% if r.auto_detected_keys %}<span class="mut">(auto-detected)</span>{% endif %}
    &nbsp; run <code>{{ r.timestamp }}</code>
  </div>

  {% if r.warnings %}
  <section>
    <h2>Warnings</h2>
    <div class="body">
      <ul>{% for w in r.warnings %}<li>{{ w }}</li>{% endfor %}</ul>
    </div>
  </section>
  {% endif %}

  <div class="tiles">
    <div class="tile"><div class="n">{{ s.source_rows }}</div><div class="l">Source rows</div></div>
    <div class="tile"><div class="n">{{ s.target_rows }}</div><div class="l">Target rows</div></div>
    <div class="tile ok"><div class="n">{{ s.matched_rows }}</div><div class="l">Matched rows</div></div>
    <div class="tile {{ 'warn' if s.mismatched_rows else 'ok' }}"><div class="n">{{ s.mismatched_rows }}</div><div class="l">Mismatched rows</div></div>
    <div class="tile {{ 'warn' if s.missing_rows else 'ok' }}"><div class="n">{{ s.missing_rows }}</div><div class="l">Missing (src only)</div></div>
    <div class="tile {{ 'warn' if s.extra_rows else 'ok' }}"><div class="n">{{ s.extra_rows }}</div><div class="l">Extra (tgt only)</div></div>
    <div class="tile {{ 'warn' if s.total_cell_mismatches else 'ok' }}"><div class="n">{{ s.total_cell_mismatches }}</div><div class="l">Cell diffs</div></div>
    <div class="tile"><div class="n">{{ s.match_pct }}%</div><div class="l">Match rate</div></div>
  </div>

  <section>
    <h2>Schema check
      {% if not sc.enabled %}<span class="pill off">disabled</span>
      {% elif sc.passed %}<span class="pill pass">pass</span>
      {% else %}<span class="pill fail">fail</span>{% endif %}
    </h2>
    <div class="body">
    {% if not sc.enabled %}<div class="empty">Schema check disabled for this test case.</div>
    {% elif sc.passed %}<div class="empty">Columns and types match.</div>
    {% else %}
      {% if sc.source_only_columns %}<p><b>Only in source:</b> {% for c in sc.source_only_columns %}<code class="k">{{ c }}</code> {% endfor %}</p>{% endif %}
      {% if sc.target_only_columns %}<p><b>Only in target:</b> {% for c in sc.target_only_columns %}<code class="k">{{ c }}</code> {% endfor %}</p>{% endif %}
      {% if sc.type_mismatches %}
      <table><thead><tr><th>Column</th><th>Source type</th><th>Target type</th></tr></thead><tbody>
      {% for t in sc.type_mismatches %}<tr><td><code class="k">{{ t.column }}</code></td><td class="src">{{ t.source_type }}</td><td class="tgt">{{ t.target_type }}</td></tr>{% endfor %}
      </tbody></table>{% endif %}
    {% endif %}
    </div>
  </section>

  <section>
    <h2>Null check {% if not nc.enabled %}<span class="pill off">disabled</span>{% else %}<span class="pill pass">reported</span>{% endif %}</h2>
    <div class="body">
    {% if not nc.enabled %}<div class="empty">Null check disabled for this test case.</div>
    {% elif not nc.source_nulls and not nc.target_nulls %}<div class="empty">No nulls found in either dataset.</div>
    {% else %}
      <table><thead><tr><th>Column</th><th>Source nulls</th><th>Target nulls</th></tr></thead><tbody>
      {% for col in null_cols %}
      <tr><td><code class="k">{{ col }}</code></td>
      <td>{{ nc.source_nulls.get(col, 0) }}</td>
      <td>{{ nc.target_nulls.get(col, 0) }}</td></tr>
      {% endfor %}
      </tbody></table>
    {% endif %}
    </div>
  </section>

  <section>
    <h2>Duplicate-key check
      {% if not dc.enabled %}<span class="pill off">disabled</span>
      {% elif dc.passed %}<span class="pill pass">pass</span>
      {% else %}<span class="pill fail">fail</span>{% endif %}
    </h2>
    <div class="body">
    {% if not dc.enabled %}<div class="empty">Duplicate check disabled for this test case.</div>
    {% elif dc.passed %}<div class="empty">No duplicate keys on either side.</div>
    {% else %}
      <p><b>Source duplicate keys:</b> {{ dc.source_duplicate_keys }} &nbsp;
         <b>Target duplicate keys:</b> {{ dc.target_duplicate_keys }}</p>
      <p class="note">Duplicate keys make the row join ambiguous, so this run is marked FAIL.</p>
    {% endif %}
    </div>
  </section>

  <section>
    <h2>Cell mismatches <span class="mut">showing {{ r.mismatches | length }} of {{ s.total_cell_mismatches }}</span></h2>
    <div class="body">
    {% if not r.mismatches %}<div class="empty">No cell mismatches.</div>
    {% else %}
      <div class="scroll"><table><thead><tr><th>Key</th><th>Column</th><th>Source</th><th>Target</th></tr></thead><tbody>
      {% for m in r.mismatches %}
      <tr><td><code class="k">{{ m.key | tojson }}</code></td><td>{{ m.column }}</td>
      <td class="src">{{ m.source }}</td><td class="tgt">{{ m.target }}</td></tr>
      {% endfor %}
      </tbody></table></div>
    {% endif %}
    </div>
  </section>

  <section>
    <h2>Missing rows (in source, not target) <span class="mut">showing {{ r.missing | length }} of {{ s.missing_rows }}</span></h2>
    <div class="body">
    {% if not r.missing %}<div class="empty">None.</div>
    {% else %}
      <div class="scroll"><table><thead><tr>{% for k in r.key_columns %}<th>{{ k }}</th>{% endfor %}</tr></thead><tbody>
      {% for row in r.missing %}<tr>{% for k in r.key_columns %}<td>{{ row.get(k, '') }}</td>{% endfor %}</tr>{% endfor %}
      </tbody></table></div>
    {% endif %}
    </div>
  </section>

  <section>
    <h2>Extra rows (in target, not source) <span class="mut">showing {{ r.extra | length }} of {{ s.extra_rows }}</span></h2>
    <div class="body">
    {% if not r.extra %}<div class="empty">None.</div>
    {% else %}
      <div class="scroll"><table><thead><tr>{% for k in r.key_columns %}<th>{{ k }}</th>{% endfor %}</tr></thead><tbody>
      {% for row in r.extra %}<tr>{% for k in r.key_columns %}<td>{{ row.get(k, '') }}</td>{% endfor %}</tr>{% endfor %}
      </tbody></table></div>
    {% endif %}
    </div>
  </section>

  <footer>Generated by datacompare {{ version }}</footer>
</div>
</body>
</html>
"""


def render_html(result: ComparisonResult, version: str = "1.0.0") -> str:
    env = Environment(autoescape=select_autoescape(["html", "xml"]))
    template = env.from_string(_TEMPLATE)
    null_cols = sorted(
        set(result.null_check.source_nulls) | set(result.null_check.target_nulls)
    )
    return template.render(
        r=result,
        s=result.summary,
        sc=result.schema_check,
        nc=result.null_check,
        dc=result.duplicate_check,
        null_cols=null_cols,
        version=version,
    )
