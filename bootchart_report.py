#!/usr/bin/env python3
"""
Boot time tracking report generator for febd7a6c (iq-8275-evk).

bootchart-data.json is the only file ever hand-edited (by appending a run).
bootchart-overview.html is ALWAYS fully derived from the JSON by this script
-- never hand-authored. Re-running `render` regenerates it byte-for-byte from
whatever is currently in the JSON.

Usage:
    python bootchart_report.py add-run new_run.json   # append a run, then render
    python bootchart_report.py add-run -              # read run JSON from stdin
    python bootchart_report.py render                 # just regenerate the HTML

new_run.json must contain a single run object matching the schema below:
{
  "timestamp": "YYYY-MM-DD HH:MM",
  "metrics": {
    "nhlos":           {"value": "5.035 s", "note": "4.318 s firmware + 0.717 s loader"},
    "kernel":          {"value": "1.573 s", "note": ""},
    "initramfs":       {"value": "0.458 s", "note": "0.092 s -> 0.550 s"},
    "sysinit_svc":     {"value": "2.734 s", "note": "0.550 s -> 3.284 s",
                         "hitters": [{"name": "systemd-tmpfiles-setup.service", "time": "0.057 s"}, ...]},
    "total_sysinit":   {"value": "9.342 s", "note": ""},
    "total_multiuser": {"value": "19.005 s", "note": "~19.79 s incl. graphical.target",
                         "hitters": [{"name": "android-tools-adbd.service", "time": "10.416 s"}, ...]}
  },
  "critical_chain": ["docker.service", "network-online.target", "..."]   // optional, kept for backup/.txt context
}
A metric row's `hitters` list (where present) IS rank -- position 0 is #1, NOT a fixed identity
across runs. NHLOS/kernel/initramfs carry no `hitters` key: there's no per-component timing source
for them today (no initcall_debug on this build, no bootloader trace).

Older entries from before hitters were merged into the metrics table may still carry a flat
top-level "hitters" list instead of a nested one. Rendering falls back to treating that as
total_multiuser's hitters, so no data migration is needed.
"""
import html
import json
import re
import sys
from pathlib import Path, PureWindowsPath

BASE_DIR = Path(__file__).resolve().parent
BOOT_CHARTS_DIR = BASE_DIR / "Boot-Charts"  # flat: every device's bootchart-data/-overview files land directly here, no per-device subdir
DEFAULT_DATA = BOOT_CHARTS_DIR / "bootchart-data.json"
DEFAULT_HTML = BOOT_CHARTS_DIR / "bootchart-overview.html"
BOOT_LOGS_DIR = BASE_DIR / "Boot-Logs"  # nested Boot-Logs/<target>/<build-folder>/<timestamp>.txt

MAX_RUNS = 30  # bootchart-data.json / .html keep only the most recent N runs;
               # full history lives on in Boot-Logs/*.txt regardless of this cap.

METRIC_ROWS = [
    ("nhlos", "NHLOS (PBL/SBL/XBL firmware + ABL loader)", False),
    ("kernel", "Kernel time", False),
    ("initramfs", "Initramfs (<code>Run /init</code> &rarr; epoch advance)", False),
    ("sysinit_svc", "systemd &rarr; sysinit.target", False),
    ("total_sysinit", "Total time till sysinit.target", True),
    ("total_multiuser", "Total time till multi-user.target", True),
]

# Notes that are identical on every boot (constant text, not derived per-run)
# are shown once under the row's own label instead of being repeated in every
# cell -- see render_metrics_table / _hitters_html.
ROW_CAPTIONS = {
    "initramfs": "dmesg: Run /init → System time advanced to built-in epoch",
    "sysinit_svc": "dmesg epoch advance → journalctl: Reached target System Initialization",
}

DELTA_THRESHOLD = 0.5
TIME_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_seconds(value):
    if not value:
        return None
    m = TIME_RE.search(value)
    return float(m.group()) if m else None


def format_delta(prev_value, cur_value):
    """Return (label, css_class) for the delta annotation, or ("", "") if not computable."""
    p, c = parse_seconds(prev_value), parse_seconds(cur_value)
    if p is None or c is None:
        return "", ""
    delta = c - p
    if delta >= DELTA_THRESHOLD:
        cls = "regressed"
    elif delta <= -DELTA_THRESHOLD:
        cls = "improved"
    else:
        cls = "unchanged"
    sign = "+" if delta >= 0 else "−"
    return f"({sign}{abs(delta):.3f} s)", cls


def esc(value):
    return html.escape(str(value), quote=False)


def load_data(data_path):
    if not data_path.exists():
        return {"device": "unknown", "runs": []}
    with open(data_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data, data_path):
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _validate_boot(boot):
    required = {"timestamp", "metrics"}
    missing = required - boot.keys()
    if missing:
        raise ValueError(f"boot object missing required keys: {missing}")
    missing_metrics = {k for k, _, _ in METRIC_ROWS} - boot["metrics"].keys()
    if missing_metrics:
        raise ValueError(f"boot.metrics missing required keys: {missing_metrics}")


def add_run(data, run):
    if "boots" in run:
        required_top = {"timestamp", "build_path", "boots"}
        missing = required_top - run.keys()
        if missing:
            raise ValueError(f"multi-boot entry missing required keys: {missing}")
        if not run["boots"]:
            raise ValueError("multi-boot entry has an empty 'boots' list")
        for boot in run["boots"]:
            _validate_boot(boot)
    else:
        _validate_boot(run)
    runs = data.setdefault("runs", [])
    runs.append(run)
    del runs[:-MAX_RUNS]  # trim from the front; full history is preserved in Boot-Logs/*.txt
    return data


def _flatten(runs):
    """Expands entries into a flat list of per-boot records, normalizing
    legacy flat (single-boot) entries to a 1-item boots list. Each record:
    {"boot": <boot dict>, "idx": <position within its entry>, "n": <boots in that entry>}.
    All rendering iterates this flat list so delta/rank/footer logic doesn't
    need to special-case entry boundaries -- boot 0 of entry K just diffs
    against the last boot of entry K-1, like any two consecutive boots."""
    flat = []
    for run in runs:
        boots = run.get("boots") or [run]
        n = len(boots)
        for idx, boot in enumerate(boots):
            flat.append({"boot": boot, "idx": idx, "n": n})
    return flat


def _metric_hitters(boot, key):
    """Returns the hitters list for a given metric row, or [] if none.
    Legacy entries (captured before hitters were merged into the metrics
    table) carry a flat top-level "hitters" list -- treated as
    total_multiuser's hitters, since that's what it always meant before."""
    m = boot["metrics"].get(key, {})
    if "hitters" in m:
        return m["hitters"]
    if key == "total_multiuser":
        return boot.get("hitters") or []
    return []


def _format_run_txt(target, run):
    """Human-readable dump of a single run, for the Boot-Logs/ backup -- the
    full-history record that survives bootchart-data.json's MAX_RUNS trim."""
    lines = [
        f"Device: {target}",
        f"Timestamp: {run['timestamp']}",
        f"Build path: {run.get('build_path', '')}",
        "",
        "Metrics:",
    ]
    for key, label, _ in METRIC_ROWS:
        m = run["metrics"].get(key, {})
        note = f"  ({m['note']})" if m.get("note") else ""
        lines.append(f"  {label}: {m.get('value', '')}{note}")
        for i, h in enumerate(_metric_hitters(run, key)):
            h_note = f"  ({h['note']})" if h.get("note") else ""
            lines.append(f"    #{i + 1}. {h['name']}: {h['time']}{h_note}")

    chain = run.get("critical_chain")
    if chain:
        lines.append("")
        lines.append("Critical chain: " + " -> ".join(chain))

    if run.get("source"):
        lines.append("")
        lines.append("Source: " + run["source"])

    return "\n".join(lines) + "\n"


def boot_logs_run_dir(target, build_path, boot_logs_dir=BOOT_LOGS_DIR):
    """Returns the nested Boot-Logs/<target>/<build-folder-name> directory for
    a given target + build_path, shared by save_run_backup's per-boot .txt
    backups and boot_capture.py's pulled raw-log folders (Logs-1, Logs-2, ...)
    so both land side-by-side in the same directory."""
    return boot_logs_dir / target / _build_folder_name(build_path) if build_path else boot_logs_dir


def save_run_backup(target, run, boot_logs_dir=BOOT_LOGS_DIR):
    """Writes a permanent per-run backup to
    Boot-Logs/<target>/<build-folder-name>/<timestamp>.txt, since
    bootchart-data.json only retains the most recent MAX_RUNS entries. Adds a
    numeric suffix if a backup for the same minute-granularity timestamp
    already exists (e.g. consecutive boots in a multi-boot entry can finish
    within the same minute), so no boot's backup is silently overwritten.
    Pre-existing flat backups directly under boot_logs_dir (from before this
    nested layout) are left in place -- only new backups go into the nested
    <target>/<build-folder> path."""
    build_path = run.get("build_path", "")
    run_dir = boot_logs_run_dir(target, build_path, boot_logs_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = run["timestamp"].replace(":", "-")
    txt_path = run_dir / f"{safe_ts}.txt"
    n = 2
    while txt_path.exists():
        txt_path = run_dir / f"{safe_ts}_{n}.txt"
        n += 1
    txt_path.write_text(_format_run_txt(target, run), encoding="utf-8")
    return txt_path


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
  :root {
    --bg: #ffffff;
    --panel: #f7f8fa;
    --border: #000000;
    --text: #1c1f26;
    --muted: #6b7280;
    --accent: #2563eb;
    --good: #1a9c63;
    --warn: #b8860b;
    --bad: #d1403f;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
    overflow-x: auto;
  }
  .container { padding: 40px 20px; width: max-content; min-width: 100%; }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  .subtitle { color: var(--muted); font-size: 0.95rem; margin-bottom: 28px; max-width: 1100px; }
  .subtitle code { background: var(--panel); padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border); }
  h2 { font-size: 1.1rem; margin-top: 36px; margin-bottom: 12px; border-left: 3px solid var(--accent); padding-left: 10px; }
  table { border-collapse: collapse; background: var(--bg); border: 2px solid var(--border); }
  th, td { text-align: left; padding: 12px 16px; border: 1px solid var(--border); white-space: nowrap; }
  th { background: var(--panel); color: var(--muted); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.04em; }
  th.run-col { color: var(--accent); text-transform: none; letter-spacing: normal; font-size: 0.85rem; }
  th.run-col.latest { color: var(--text); background: #e8eefc; }
  tr.total td { font-weight: 600; color: var(--accent); background: #eef2fd; }
  td.timing { font-family: "SF Mono", Consolas, monospace; font-size: 0.95rem; }
  td.timing .note { display: block; color: var(--muted); font-size: 0.78rem; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; white-space: normal; margin-top: 2px; }
  td.timing .delta { font-size: 0.78rem; margin-left: 6px; font-family: "SF Mono", Consolas, monospace; }
  .delta.unchanged { color: var(--muted); }
  .delta.improved { color: var(--good); }
  .delta.regressed { color: var(--bad); }
  td.buildpath { font-family: "SF Mono", Consolas, monospace; font-size: 0.78rem; color: var(--muted); white-space: normal; word-break: break-all; text-align: center; }
  th.entry-start, td.entry-start { border-left: 2px solid var(--accent); }
  td:first-child .row-caption { display: block; font-weight: 400; font-size: 0.75rem; color: var(--muted); margin-top: 2px; white-space: normal; }
  td.timing ol.hitters { list-style: none; margin: 6px 0 0; padding: 0; white-space: normal; }
  td.timing ol.hitters li { font-size: 0.78rem; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: var(--muted); margin-top: 2px; }
  td.timing ol.hitters li .hitter-name { font-family: "SF Mono", Consolas, monospace; }
"""


def _boot_header_label(rec):
    boot = rec["boot"]
    if rec["n"] == 1:
        return boot["timestamp"]
    time_part = boot["timestamp"].split(" ")[-1]
    return f"Boot {rec['idx'] + 1} · {time_part}"


def _header_cells(flat):
    return "".join(
        f'<th class="run-col{" latest" if i == len(flat) - 1 else ""}'
        f'{" entry-start" if rec["idx"] == 0 and i > 0 else ""}">'
        f'{esc(_boot_header_label(rec))}</th>'
        for i, rec in enumerate(flat)
    )


def _strip_performance_suffix(build_path):
    """Boot analysis is only ever run against a build's '...\\performance'
    subdirectory, so that segment is implied and dropped from display/paths."""
    p = PureWindowsPath(build_path)
    if p.name.lower() == "performance":
        p = p.parent
    return str(p)


def _build_folder_name(build_path):
    return PureWindowsPath(_strip_performance_suffix(build_path)).name or "unknown-build"


def render_build_path_row(runs):
    if not any(r.get("build_path") for r in runs):
        return ""
    cells = "".join(
        f'<td class="buildpath" colspan="{len(run.get("boots") or [run])}">{esc(_strip_performance_suffix(run.get("build_path", "")))}</td>'
        for run in runs
    )
    return f"<tr><td>Nightly build path</td>{cells}</tr>"


def _hitters_html(hitters):
    """Ranked sub-list rendered inside a metric's value cell. Each hitter's
    "note" (e.g. "largest single cost", "critical path"/"off critical path")
    is precomputed at capture time in boot_capture.build_run, not here."""
    if not hitters:
        return ""
    items = []
    for i, h in enumerate(hitters):
        note = h.get("note", "")
        note_html = f" &middot; {esc(note)}" if note else ""
        items.append(
            f'<li class="rank-{i + 1}">#{i + 1} <span class="hitter-name">{esc(h["name"])}</span> '
            f'&mdash; {esc(h["time"])}{note_html}</li>'
        )
    return f'<ol class="hitters">{"".join(items)}</ol>'


def render_metrics_table(runs):
    flat = _flatten(runs)
    header_cells = _header_cells(flat)
    rows_html = [render_build_path_row(runs)]
    for key, label, is_total in METRIC_ROWS:
        caption = ROW_CAPTIONS.get(key)
        label_html = f'{label}<span class="row-caption">{esc(caption)}</span>' if caption else label
        cells = []
        for i, rec in enumerate(flat):
            boot = rec["boot"]
            m = boot["metrics"][key]
            value, note = m.get("value", ""), m.get("note", "")
            delta_html = ""
            if i > 0:
                prev_value = flat[i - 1]["boot"]["metrics"][key].get("value", "")
                delta_label, delta_cls = format_delta(prev_value, value)
                if delta_label:
                    delta_html = f'<span class="delta {delta_cls}">{delta_label}</span>'
            note_html = f'<span class="note">{esc(note)}</span>' if note and note != caption else ""
            hitters_html = _hitters_html(_metric_hitters(boot, key))
            cls = "timing" + (" entry-start" if rec["idx"] == 0 and i > 0 else "")
            cells.append(f'<td class="{cls}">{esc(value)}{delta_html}{note_html}{hitters_html}</td>')
        row_class = ' class="total"' if is_total else ""
        rows_html.append(f"<tr{row_class}><td>{label_html}</td>{''.join(cells)}</tr>")
    return f"""
  <table>
    <thead><tr><th>Stage</th>{header_cells}</tr></thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>"""


def render_html(data):
    runs = data.get("runs", [])
    device = esc(data.get("device", "unknown device"))
    if not runs:
        body = "<p>No runs collected yet. Run <code>bootchart_report.py add-run &lt;run.json&gt;</code> to add the first one.</p>"
        metrics_html = ""
    else:
        metrics_html = render_metrics_table(runs)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Boot Time Analysis &mdash; {device}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

  <h1>Boot Time Analysis</h1>
  <div class="subtitle">Device <code>{device}</code> &mdash; history of collected boot runs. Data source: <code>bootchart-data.json</code>, generated by <code>bootchart_report.py</code>. <b>Boot analysis is performed only on performance builds.</b> High hitters for each stage (where measurable) are ranked inline beneath that stage's timing.</div>

  <h2>Boot Time Summary</h2>
  {metrics_html}

</div>
</body>
</html>
"""


def render(data_path=DEFAULT_DATA, html_path=DEFAULT_HTML):
    data = load_data(data_path)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(data), encoding="utf-8")
    print(f"Rendered {html_path} from {data_path} ({len(data.get('runs', []))} run(s)).")


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd = argv[1]
    if cmd == "render":
        render()
        return 0
    if cmd == "add-run":
        if len(argv) < 3:
            print("usage: bootchart_report.py add-run <run.json|->")
            return 1
        src = argv[2]
        raw = sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8")
        run = json.loads(raw)
        data = load_data(DEFAULT_DATA)
        add_run(data, run)
        save_data(data, DEFAULT_DATA)
        render()
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
