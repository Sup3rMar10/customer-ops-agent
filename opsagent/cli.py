"""Command line: python -m opsagent demo | serve | eval | ask | seed | pending | approve-token | reject"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import Any

from .agent import Agent, AgentResult, RulePlanner
from .backend import Backend
from .gateway import Gateway, GuardConfig, load_secret
from .seed import build_database

DB_DEFAULT = os.environ.get("OPSAGENT_DB", "data/ops.db")
AUDIT_DEFAULT = os.environ.get("OPSAGENT_AUDIT", "data/audit.jsonl")
SECRET_FILE = os.environ.get("OPSAGENT_SECRET_FILE", "data/approval_secret")
ROOT = Path(__file__).resolve().parent.parent


def _ensure_db(path: str) -> Path:
    p = Path(path)
    if not p.exists():
        print(f"seeding {p} ...", file=sys.stderr)
        build_database(p)
    return p


def _gateway(db: str, writes: bool, audit: str | None = AUDIT_DEFAULT) -> tuple[Backend, Gateway]:
    backend = Backend(_ensure_db(db))
    cfg = GuardConfig(mode="read_write" if writes else "read_only")
    return backend, Gateway(backend, cfg, audit_path=audit, secret=load_secret(SECRET_FILE))


def _show(result: AgentResult) -> None:
    print(f"\n> {result.question}")
    for name, args, out in result.calls:
        shown = ", ".join(f"{k}={v!r}" for k, v in args.items() if k != "confirmation_token")
        status = out.get("status") or ("ok" if out.get("ok") else f"ERROR {out['error']['code']}")
        flags = f"  [{len(out['security_flags'])} free-text field(s) withheld]" if out.get("security_flags") else ""
        print(f"    tool: {name}({shown}) -> {status}{flags}")
    print("  " + result.answer.replace("\n", "\n  "))


def cmd_seed(args: argparse.Namespace) -> int:
    build_database(args.db, seed=args.seed)
    print(f"wrote {args.db}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    backend, gateway = _gateway(args.db, args.allow_writes)
    if args.llm == "anthropic":
        from .llm_anthropic import AnthropicLLM
        llm = AnthropicLLM()
    else:
        llm = RulePlanner()
    _show(Agent(gateway, llm).run(args.question))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from contextlib import nullcontext

    with tempfile.TemporaryDirectory() as tmp:
        db = build_database(Path(tmp) / "demo.db")
        backend = Backend(db)
        gateway = Gateway(backend, GuardConfig(mode="read_write"), audit_path=Path(tmp) / "audit.jsonl", secret=b"demo-secret")
        ctx: Any = nullcontext(gateway)
        if args.mcp:
            from .mcp_server import McpToolClient, build_server
            ctx = McpToolClient(build_server(gateway))
        with ctx as tools:
            agent = Agent(tools, RulePlanner())
            print("Customer ops agent demo. Offline: no model, no network, synthetic data."
                  + ("  Tools are called through a real MCP connection." if args.mcp else ""))
            for q in ["What does Acme Industrial Supply owe?", "How much does Northwind owe?",
                      "Why was invoice INV-20006 disputed?", "Show me the invoices for Contoso Freight",
                      "What's the email and phone for the AP lead at Acme Industrial Supply?",
                      "Delete invoice INV-20002"]:
                _show(agent.run(q))
            print("\n--- a write: the agent proposes, a human approves, then it commits ---")
            q = "Draft a firm reminder for INV-20002"
            first = agent.run(q)
            _show(first)
            token = gateway.operator_token(first.pending_ids[0])
            print(f"\n[operator] reviews {first.pending_ids[0]} and approves it by handing over the token {token}")
            second = agent.run(f"{q} with token {token}")
            _show(second)
            row = backend.conn.execute("SELECT id, status, to_contact, subject FROM reminder_drafts").fetchone()
            print(f"\n[database] {row['id']} status={row['status']} subject={row['subject']!r}  (a draft: nothing is ever sent)")
            print("\n--- audit log (last 5 lines; arguments masked, tokens never logged) ---")
            for line in (Path(tmp) / "audit.jsonl").read_text().splitlines()[-5:]:
                print(" ", line)
        backend.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .mcp_server import build_server
    backend, gateway = _gateway(args.db, args.allow_writes, args.audit)
    server = build_server(gateway)
    print(f"customer-ops MCP server ({'read-write: writes need approval' if args.allow_writes else 'read-only'}), "
          f"transport={args.transport}", file=sys.stderr)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    """Operator channel. Needs the approval secret, so only someone with access to this machine can approve."""
    backend, gateway = _gateway(args.db, True, None)
    rows = [r for r in gateway.pending() if r["status"] == "pending"]
    for r in rows:
        print(f"{r['id']}  {r['tool']}  {json.dumps(r['args'])}  expires {r['expires_at']}")
        print(f"    token: {gateway.operator_token(r['id'])}")
    if not rows:
        print("nothing pending")
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    _, gateway = _gateway(args.db, True, None)
    gateway.reject(args.pending_id)
    print(f"rejected {args.pending_id}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from . import evals, report

    results_dir = ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        template = build_database(Path(tmp) / "template.db")
        scenarios = evals.build_scenarios(template)
        print(f"running {len(scenarios)} scenarios ...", file=sys.stderr)
        results = [evals.run_scenario(s, template) for s in scenarios]
        summary = evals.summarize(results)
        print("running guardrail ablation ...", file=sys.stderr)
        ablation = evals.run_ablation(template)
        latency: dict[str, Any] = {"inprocess": evals.measure_inprocess(template)}
        if args.skip_mcp:
            latency["mcp_stdio"] = {"calls": 0, "p50_ms": None, "p95_ms": None}
        else:
            print("measuring MCP stdio latency ...", file=sys.stderr)
            mcp_db = Path(tmp) / "mcp.db"
            shutil.copyfile(template, mcp_db)
            latency["mcp_stdio"] = evals.measure_mcp_stdio(mcp_db)
        data = {"summary": summary, "scenarios": results, "ablation": ablation, "latency": latency,
                "detection": evals.detection_stats(template),
                "environment": {"python": platform.python_version(), "platform": platform.platform(), "mcp": version("mcp")}}
    (results_dir / "eval.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    report.write_charts(data, results_dir)
    for label in ("dev", "holdout", "overall"):
        b = summary[label]
        print(f"{label:8} {b['passed']}/{b['n']} passed  tool-sequence accuracy {b['tool_sequence_accuracy']}")
    for r in results:
        if not r["passed"]:
            print(f"  FAIL {r['id']} ({r['split']}): {'; '.join(r['failures'])}")
    for a in ablation:
        print(f"  ablation {a['config']:22} attempts={a['write_attempts']} committed={a['writes_committed']} held={a['proposals_held']}")
    if args.write_readme:
        report.write_readme_block(ROOT / "README.md", report.results_markdown(data))
        print("README results block updated")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="opsagent", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help: str, db: bool = True) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        if db:
            sp.add_argument("--db", default=DB_DEFAULT, help="mock backend database (created on first use)")
        sp.set_defaults(fn=fn)
        return sp

    sp = add("seed", cmd_seed, "(re)create the synthetic database")
    sp.add_argument("--seed", type=int, default=42)
    sp = add("demo", cmd_demo, "run a scripted tour, offline", db=False)
    sp.add_argument("--mcp", action="store_true", help="route tool calls through a real MCP client and server")
    sp = add("ask", cmd_ask, "ask one question")
    sp.add_argument("question")
    sp.add_argument("--llm", choices=["rule", "anthropic"], default="rule")
    sp.add_argument("--allow-writes", action="store_true", help="expose the write tools (they still need approval)")
    sp = add("serve", cmd_serve, "run the MCP server")
    sp.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--allow-writes", action="store_true")
    sp.add_argument("--audit", default=AUDIT_DEFAULT)
    add("pending", cmd_pending, "operator: list proposals waiting for approval, with their tokens")
    sp = add("reject", cmd_reject, "operator: reject a proposal")
    sp.add_argument("pending_id")
    sp = add("eval", cmd_eval, "run the scenarios, the guardrail ablation and the latency measurements", db=False)
    sp.add_argument("--write-readme", action="store_true", help="rewrite the results block in README.md")
    sp.add_argument("--skip-mcp", action="store_true", help="skip the subprocess latency measurement")
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
