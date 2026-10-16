import sys

import pytest

pytest.importorskip("mcp")

from mcp import StdioServerParameters  # noqa: E402

from opsagent.agent import Agent, RulePlanner  # noqa: E402
from opsagent.mcp_server import McpToolClient, build_server  # noqa: E402
from opsagent.tools import TOOLS  # noqa: E402


def test_listed_tools_match_the_registry(env):
    with McpToolClient(build_server(env.gateway)) as client:
        listed = {t["name"]: t for t in client.list_tools()}
    assert set(listed) == {t.name for t in TOOLS}
    for spec in TOOLS:   # the schema a model sees is the registry's, with id patterns and enums
        assert set(listed[spec.name]["input_schema"]["properties"]) == set(spec.params)
    assert listed["get_customer"]["input_schema"]["properties"]["customer_id"]["pattern"] == r"^C-\d{4}$"


def test_read_only_server_does_not_list_write_tools(make_env):
    e = make_env(mode="read_only")
    with McpToolClient(build_server(e.gateway)) as client:
        names = {t["name"] for t in client.list_tools()}
        assert not names & {"create_dispute_note", "send_reminder_draft"}
        assert client.call("create_dispute_note", {"dispute_id": "D-3001", "text": "x"})["ok"] is False


def test_results_and_errors_round_trip(env):
    with McpToolClient(build_server(env.gateway)) as client:
        ok = client.call("get_customer", {"customer_id": "C-1004"})
        assert ok["ok"] and ok["data"]["customer"]["name"] == "Blue Harbor Foods"
        assert client.call("get_customer", {"customer_id": "C-9999"})["error"]["code"] == "not_found"
        assert client.call("get_customer", {"customer_id": "bad"})["error"]["code"] == "invalid_arguments"


def test_write_over_mcp_is_still_only_a_proposal(env):
    with McpToolClient(build_server(env.gateway)) as client:
        out = client.call("create_dispute_note", {"dispute_id": "D-3001", "text": "hello"})
    assert out["status"] == "pending_approval" and env.added("notes") == 0


def test_the_agent_works_over_a_real_mcp_connection(env):
    with McpToolClient(build_server(env.gateway)) as client:
        r = Agent(client, RulePlanner()).run("What does Acme Industrial Supply owe?")
    assert r.tool_names == ["search_customers", "list_invoices"] and "$35,650.00" in r.answer


def test_stdio_subprocess_server(template, tmp_path):
    """The real deployment shape: a separate process speaking MCP over stdio."""
    import shutil
    db = tmp_path / "ops.db"
    shutil.copyfile(template, db)
    params = StdioServerParameters(command=sys.executable, args=["-m", "opsagent", "serve", "--db", str(db),
                                                                  "--audit", str(tmp_path / "audit.jsonl")])
    with McpToolClient(params, timeout=60) as client:
        assert "list_invoices" in {t["name"] for t in client.list_tools()}
        out = client.call("list_invoices", {"customer_id": "C-1003", "status": "overdue"})
    assert out["data"]["total_matching"] == 2
    assert (tmp_path / "audit.jsonl").read_text().count("list_invoices") == 1
