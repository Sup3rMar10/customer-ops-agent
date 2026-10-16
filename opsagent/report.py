"""Tables and charts, generated from results/eval.json so the README never holds a number that was typed by hand."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

BLUE, ORANGE = "#2a78d6", "#eb6834"          # categorical slots 1 and 2, checked with the palette validator
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e3e2de"


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, length=0)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)


def write_charts(data: dict[str, Any], out_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # 1. pass rate by category, dev and holdout side by side
    dev, hold = data["summary"]["by_category"], data["summary"].get("by_category_holdout", {})
    names = sorted(dev, key=lambda c: (dev[c]["rate"], c))
    fig, ax = plt.subplots(figsize=(8, 0.62 * len(names) + 1.8), facecolor=SURFACE)
    _style(ax)
    h = 0.32
    for i, c in enumerate(names):
        ax.barh(i - h / 2 - 0.02, dev[c]["rate"] * 100, height=h, color=BLUE, label="dev" if i == 0 else None)
        ax.text(dev[c]["rate"] * 100 + 1.5, i - h / 2 - 0.02, f"{dev[c]['passed']}/{dev[c]['n']}", va="center", color=INK, fontsize=9)
        if c in hold:
            ax.barh(i + h / 2 + 0.02, hold[c]["rate"] * 100, height=h, color=ORANGE, label="holdout" if "holdout" not in ax.get_legend_handles_labels()[1] else None)
            ax.text(max(hold[c]["rate"] * 100, 0) + 1.5, i + h / 2 + 0.02, f"{hold[c]['passed']}/{hold[c]['n']}", va="center", color=INK, fontsize=9)
    ax.set_yticks(range(len(names)), [c.replace("_", " ") for c in names], color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, 112)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("scenarios passed (%)", color=INK2)
    ax.legend(frameon=False, loc="lower right", labelcolor=INK2)
    ax.set_title("Scenarios passed, by category: dev vs holdout", loc="left", color=INK, fontsize=11)
    fig.tight_layout()
    fig.savefig(out_dir / "eval_by_category.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)

    # 2. guardrail ablation: what a gullible model gets done, layer by layer
    rows = data["ablation"]
    fig, ax = plt.subplots(figsize=(8, 0.9 * len(rows) + 1.8), facecolor=SURFACE)
    _style(ax)
    h = 0.32
    for i, r in enumerate(rows):
        ax.barh(i - h / 2 - 0.02, r["write_attempts"], height=h, color=BLUE, label="write attempts" if i == 0 else None)
        ax.barh(i + h / 2 + 0.02, r["writes_committed"], height=h, color=ORANGE, label="writes committed" if i == 0 else None)
        ax.text(r["write_attempts"] + 0.12, i - h / 2 - 0.02, str(r["write_attempts"]), va="center", color=INK, fontsize=9)
        ax.text(r["writes_committed"] + 0.12, i + h / 2 + 0.02, str(r["writes_committed"]), va="center", color=INK, fontsize=9)
    ax.set_yticks(range(len(rows)), [r["config"] for r in rows], color=INK)
    ax.invert_yaxis()
    ax.set_xlim(0, max(r["write_attempts"] for r in rows) + 1.5)
    ax.set_xlabel(f"count, over {rows[0]['questions']} questions asked of a model that obeys injected text", color=INK2)
    ax.legend(frameon=False, loc="lower right", labelcolor=INK2)
    ax.set_title("What injected instructions achieved, by guardrail setting", loc="left", color=INK, fontsize=11)
    fig.tight_layout()
    fig.savefig(out_dir / "guardrail_ablation.png", dpi=150, facecolor=SURFACE)
    plt.close(fig)


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def results_markdown(data: dict[str, Any]) -> str:
    s, env = data["summary"], data["environment"]
    lat, det = data["latency"], data["detection"]
    ov, dev, hold = s["overall"], s["dev"], s["holdout"]
    ci = lambda b: f"{_pct(b['rate'])} ({b['passed']}/{b['n']}, 95% CI {_pct(b['ci95'][0])} to {_pct(b['ci95'][1])})"  # noqa: E731
    out = ["![Pass rate by category](results/eval_by_category.png)", "",
           "![Guardrail ablation](results/guardrail_ablation.png)", "", "### Scenario results", "",
           "Planner: the offline rule-based one. Every number below is read from `results/eval.json`.", "",
           "| Split | Scenarios passed | Tool sequence exactly right |", "|---|---|---|",
           f"| dev (written while building) | {ci(dev)} | {_pct(dev['tool_sequence_accuracy'])} |",
           f"| holdout (written afterwards, planner not changed in response) | {ci(hold)} | {_pct(hold['tool_sequence_accuracy'])} |",
           f"| all | {ci(ov)} | {_pct(ov['tool_sequence_accuracy'])} |", "",
           "| Category | Dev passed | Holdout passed |", "|---|---|---|"]
    hold_cats = s.get("by_category_holdout", {})
    out += [f"| {c.replace('_', ' ')} | {b['passed']}/{b['n']} | "
            + (f"{hold_cats[c]['passed']}/{hold_cats[c]['n']}" if c in hold_cats else "-") + " |" for c, b in s["by_category"].items()]
    failed = [r for r in data["scenarios"] if not r["passed"]]
    out += ["", f"### Scenarios that failed ({len(failed)})", ""]
    if failed:
        out += ["| Scenario | Split | Why |", "|---|---|---|"]
        out += [f"| `{r['id']}` | {r['split']} | {'; '.join(r['failures'])[:220].replace('|', '/')} |" for r in failed]
    else:
        out.append("None.")
    out += ["", "### Guardrail ablation", "",
            "The same seven questions about the booby-trapped customers, asked of a stand-in model that does whatever "
            "instructions it finds in tool output, under five settings. \"Attempts\" is how many write calls the model "
            "made. \"Committed\" is how many rows actually changed. \"Held\" is proposals waiting for a human.", "",
            "| Setting | Write attempts | Writes committed | Proposals held | Refused by gateway |", "|---|---|---|---|---|"]
    out += [f"| {r['config']} | {r['write_attempts']} | {r['writes_committed']} | {r['proposals_held']} | "
            f"{', '.join(f'{k} x{v}' for k, v in r['refused'].items()) or '-'} |" for r in data["ablation"]]
    out += ["", "### Free-text screen", "",
            f"Of {det['notes_total']} notes in the database, {det['planted']} are planted injections and {det['benign']} are "
            f"ordinary. The screen caught {det['planted_caught']} of {det['planted']} planted ones (one is worded to slip past "
            f"it) and flagged {det['benign_flagged']} of {det['benign']} ordinary notes.", "",
            "### Latency", "",
            "| Path | Calls | p50 | p95 |", "|---|---|---|---|",
            f"| In-process gateway (`list_invoices`) | {lat['inprocess']['calls']} | {lat['inprocess']['p50_ms']} ms | {lat['inprocess']['p95_ms']} ms |",
            f"| Real MCP server over stdio, subprocess (`list_invoices`) | {lat['mcp_stdio']['calls']} | {lat['mcp_stdio']['p50_ms']} ms | {lat['mcp_stdio']['p95_ms']} ms |", "",
            f"Measured on this machine ({env['platform']}, Python {env['python']}, mcp {env['mcp']}), one run, SQLite on local disk, no network."]
    return "\n".join(out)


def write_readme_block(readme: Path, markdown: str) -> None:
    text = readme.read_text(encoding="utf-8")
    block = f"<!-- RESULTS:START -->\n\n{markdown}\n\n<!-- RESULTS:END -->"
    new, n = re.subn(r"<!-- RESULTS:START -->.*?<!-- RESULTS:END -->", lambda _: block, text, flags=re.S)
    if n != 1:
        raise RuntimeError("README has no RESULTS:START / RESULTS:END markers")
    readme.write_text(new, encoding="utf-8")


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
