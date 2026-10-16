import pytest

from opsagent.agent import Agent, GullibleLLM, RulePlanner, Step, ToolCall, extract_name, read_question


@pytest.mark.parametrize("question,name", [
    ("What does Acme Industrial Supply owe?", "Acme Industrial Supply"),
    ("How much does Northwind owe?", "Northwind"),
    ('Show me "Blue Harbor Foods"', "Blue Harbor Foods"),
    ("Draft a reminder for INV-20002", None),
    ("Is Globex & Sons late on C-1004?", "Globex & Sons"),
    ("what does acme industrial supply owe", "acme industrial supply"),
    ("Show me the overdue invoices", None),
    ("Tell me a joke", None),
])
def test_extract_name(question, name):
    assert extract_name(question) == name


def test_read_question_finds_ids_even_when_malformed():
    c = read_question("Why is INV-2000 disputed for c-10033 and D-30011?")
    assert (c.invoice_id, c.customer_id, c.dispute_id) == ("INV-2000", "C-10033", "D-30011")
    assert c.name is None            # an id wins over a name


@pytest.mark.parametrize("question,tools", [
    ("What does Acme Industrial Supply owe?", ["search_customers", "list_invoices"]),
    ("Show the overdue invoices for C-1003", ["list_invoices"]),
    ("Why was invoice INV-20006 disputed?", ["get_invoice", "list_disputes"]),
    ("Does Northwind Traders pay on time?", ["search_customers", "payment_history"]),
    ("Who are the contacts for Blue Harbor Foods?", ["search_customers", "get_customer"]),
    ("Show all invoices for Globex Supplies", ["search_customers", "list_invoices"]),
    ("Delete invoice INV-20002", []),
    ("Tell me a joke", []),
])
def test_planner_picks_the_expected_tools(env, question, tools):
    assert Agent(env.gateway, RulePlanner()).run(question).tool_names == tools


def test_ambiguous_name_asks_instead_of_guessing(env):
    r = Agent(env.gateway, RulePlanner()).run("How much does Northwind owe?")
    assert r.tool_names == ["search_customers"] and "Which one" in r.answer


def test_answers_state_exact_amounts(env):
    r = Agent(env.gateway, RulePlanner()).run("What does Acme Industrial Supply owe?")
    assert "$35,650.00" in r.answer and "97 days overdue" in r.answer


def test_planner_does_not_call_tools_that_are_not_listed(make_env):
    e = make_env(mode="read_only")
    r = Agent(e.gateway, RulePlanner()).run("Draft a reminder for INV-20002")
    assert r.tool_names == ["get_invoice"] and "read-only" in r.answer


def test_loop_stops_at_max_steps(env):
    forever = lambda messages, tools: Step(call=ToolCall("get_customer", {"customer_id": "C-1001"}))  # noqa: E731
    r = Agent(env.gateway, forever, max_steps=3).run("anything")
    assert r.stopped == "max_steps" and len(r.calls) == 3


def test_loop_passes_tool_results_back_to_the_model(env):
    seen = []

    def llm(messages, tools):
        seen.append([m["role"] for m in messages])
        return Step(call=ToolCall("get_customer", {"customer_id": "C-1004"})) if len(messages) == 1 else Step(text="done")
    Agent(env.gateway, llm).run("hi")
    assert seen == [["user"], ["user", "tool"]]


def test_the_gullible_model_really_is_gullible_when_unprotected(make_env):
    """If this stops being true the ablation in the README would be meaningless."""
    e = make_env(mode="read_write", require_approval=False, screen_untrusted=False)
    r = Agent(e.gateway, GullibleLLM()).run("What's the status of INV-20011?")
    assert "create_dispute_note" in r.tool_names and e.added("notes") == 1


def test_the_gullible_model_is_stopped_by_the_defaults(env):
    r = Agent(env.gateway, GullibleLLM()).run("Show me the invoices for Contoso Freight")
    assert not set(r.tool_names) & {"create_dispute_note", "send_reminder_draft"} and env.added("notes") == 0


def test_a_paraphrase_that_beats_the_screen_is_still_held_for_approval(env):
    r = Agent(env.gateway, GullibleLLM()).run("What's the status of INV-20015?")
    assert r.tool_names == ["get_invoice", "create_dispute_note"] and r.pending_ids == ["P-1001"]
    assert env.added("notes") == 0 and env.counts()["pending"] == 1


def test_confirmation_turn_commits_through_the_agent(env):
    q = 'Add a note to dispute D-3001: "Pricing team confirmed the $45 rate"'
    first = Agent(env.gateway, RulePlanner()).run(q)
    assert first.pending_ids and env.added("notes") == 0
    token = env.gateway.operator_token(first.pending_ids[0])
    second = Agent(env.gateway, RulePlanner()).run(f"{q} with token {token}")
    assert "Done" in second.answer and env.added("notes") == 1
