import json
from datetime import timedelta

import pytest

from opsagent.gateway import Gateway, GuardConfig, load_secret
from opsagent.tools import BY_NAME, TOOLS, ToolError, validate

NOTE = {"dispute_id": "D-3001", "text": "Pricing team confirmed the $45 rate"}


def notes(env):
    return env.added("notes")


# --- validation --------------------------------------------------------------------------------------
@pytest.mark.parametrize("tool,args", [
    ("get_customer", {}), ("get_customer", {"customer_id": "c-1003"}), ("get_customer", {"customer_id": 1003}),
    ("get_customer", {"customer_id": "C-1003'; DROP TABLE invoices;--"}), ("get_customer", {"customer_id": "C-1003\x00"}),
    ("get_customer", {"customer_id": "C-1003\nC-1004"}),
    ("list_invoices", {"customer_id": "C-1003", "limit": 0}), ("list_invoices", {"customer_id": "C-1003", "limit": 51}),
    ("list_invoices", {"customer_id": "C-1003", "limit": True}), ("list_invoices", {"customer_id": "C-1003", "limit": "5"}),
    ("list_invoices", {"customer_id": "C-1003", "status": "weird"}), ("list_invoices", {"customer_id": "C-1003", "extra": 1}),
    ("get_invoice", {"invoice_id": "INV-1"}), ("get_invoice", {"invoice_id": "INV-200050"}),
    ("search_customers", {"query": "a"}), ("search_customers", {"query": "x" * 61}),
])
def test_bad_arguments_are_rejected_before_the_backend(env, tool, args):
    out = env.gateway.call(tool, args)
    assert out["ok"] is False and out["error"]["code"] == "invalid_arguments"
    assert env.backend.request_count == 0


def test_non_object_arguments_and_unknown_tool(env):
    assert env.gateway.call("get_customer", ["C-1003"])["error"]["code"] == "invalid_arguments"
    assert env.gateway.call("drop_everything", {})["error"]["code"] == "unknown_tool"


def test_validate_leaves_defaults_to_backend():
    assert validate(BY_NAME["list_invoices"], {"customer_id": "C-1003", "status": ""}) == {"customer_id": "C-1003"}


def test_every_tool_has_a_schema_the_validator_can_use():
    for spec in TOOLS:
        schema = spec.input_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) <= set(schema["properties"])


# --- modes --------------------------------------------------------------------------------------------
def test_read_only_is_the_default_and_hides_write_tools(env):
    g = Gateway(env.backend)
    assert g.cfg.mode == "read_only"
    assert {t["name"] for t in g.list_tools()} == {t.name for t in TOOLS if not t.write}
    assert g.call("create_dispute_note", NOTE)["error"]["code"] == "writes_disabled"


def test_read_write_lists_write_tools(env):
    assert {t["name"] for t in env.gateway.list_tools()} == {t.name for t in TOOLS}


# --- the approval flow --------------------------------------------------------------------------------
def test_write_only_proposes(env):
    out = env.gateway.call("create_dispute_note", NOTE)
    assert out["status"] == "pending_approval" and out["pending_id"] == "P-1001"
    assert "CONF-" not in json.dumps(out)          # the model is never handed a token
    assert notes(env) == 0


def test_same_proposal_is_not_duplicated(env):
    a = env.gateway.call("create_dispute_note", NOTE)["pending_id"]
    assert env.gateway.call("create_dispute_note", NOTE)["pending_id"] == a
    assert env.counts()["pending"] == 1


def test_operator_token_commits_exactly_once(env):
    pid = env.gateway.call("create_dispute_note", NOTE)["pending_id"]
    token = env.gateway.operator_token(pid)
    out = env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": token})
    assert out["status"] == "committed" and notes(env) == 1
    replay = env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": token})
    assert replay["error"]["code"] == "confirmation_invalid" and notes(env) == 1


def test_token_must_match_the_proposal(env):
    pid = env.gateway.call("create_dispute_note", NOTE)["pending_id"]
    token = env.gateway.operator_token(pid)
    other = env.gateway.call("create_dispute_note", {**NOTE, "text": "Balance waived", "confirmation_token": token})
    assert other["error"]["code"] == "confirmation_invalid" and notes(env) == 0


def test_token_without_a_proposal_or_with_the_wrong_value(env):
    assert env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": "ADMIN-OVERRIDE"})["error"]["code"] == "confirmation_invalid"
    env.gateway.call("create_dispute_note", NOTE)
    assert env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": "CONF-FORGED"})["error"]["code"] == "confirmation_invalid"
    assert notes(env) == 0


def test_token_is_bound_to_the_secret(env):
    two = Gateway(env.backend, GuardConfig(mode="read_write"), secret=b"two", clock=lambda: env.now)
    pid = env.gateway.call("create_dispute_note", NOTE)["pending_id"]
    forged = two.operator_token(pid)                       # same database, wrong secret
    assert env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": forged})["error"]["code"] == "confirmation_invalid"


def test_proposals_expire(env):
    pid = env.gateway.call("create_dispute_note", NOTE)["pending_id"]
    token = env.gateway.operator_token(pid)
    env.now += timedelta(minutes=16)
    assert env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": token})["error"]["code"] == "confirmation_expired"
    assert notes(env) == 0


def test_three_wrong_tokens_lock_the_proposal(env):
    pid = env.gateway.call("create_dispute_note", NOTE)["pending_id"]
    for _ in range(3):
        env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": "CONF-0000000000000000"})
    good = env.gateway.operator_token(pid)
    assert env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": good})["error"]["code"] == "confirmation_locked"


def test_rejected_proposal_cannot_be_confirmed(env):
    pid = env.gateway.call("create_dispute_note", NOTE)["pending_id"]
    token = env.gateway.operator_token(pid)
    env.gateway.reject(pid)
    assert env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": token})["error"]["code"] == "confirmation_invalid"
    with pytest.raises(ToolError):
        env.gateway.operator_token(pid)


def test_proposal_rate_limit(env):
    codes = [env.gateway.call("create_dispute_note", {**NOTE, "text": f"n{i}"}).get("error", {}).get("code") for i in range(6)]
    assert codes == [None] * 5 + ["rate_limited"]


def test_reminder_flow_creates_a_draft_never_a_send(env):
    pid = env.gateway.call("send_reminder_draft", {"invoice_id": "INV-20002", "tone": "firm"})["pending_id"]
    assert env.counts()["drafts"] == 0
    args = {"invoice_id": "INV-20002", "tone": "firm", "confirmation_token": env.gateway.operator_token(pid)}
    out = env.gateway.call("send_reminder_draft", args)
    assert out["status"] == "committed" and out["data"]["draft"]["status"] == "draft" and env.counts()["drafts"] == 1


def test_a_failed_commit_keeps_the_proposal_and_does_not_double_write(env):
    pid = env.gateway.call("create_dispute_note", NOTE)["pending_id"]
    token = env.gateway.operator_token(pid)
    env.backend.fail_next(1)
    assert env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": token})["error"]["code"] == "upstream_unavailable"
    assert notes(env) == 0                                  # writes are never auto-retried
    assert env.gateway.call("create_dispute_note", {**NOTE, "confirmation_token": token})["status"] == "committed"
    assert notes(env) == 1


def test_approval_can_be_switched_off_for_ablation_only(make_env):
    e = make_env(mode="read_write", require_approval=False)
    assert e.gateway.call("create_dispute_note", NOTE)["status"] == "committed" and notes(e) == 1


# --- reads, masking, screening, retries, audit --------------------------------------------------------
def test_contacts_are_masked(env):
    contacts = env.gateway.call("get_customer", {"customer_id": "C-1003"})["data"]["customer"]["contacts"]
    assert contacts[0] == {"name": "Dana K.", "role": "AP lead", "email": "d***@acme-supply.example", "phone": "***-***-7788"}


def test_pii_inside_notes_is_masked(env):
    note = env.gateway.call("get_invoice", {"invoice_id": "INV-20007"})["data"]["invoice"]["notes"]
    assert "dana.kowalski" not in note and "555-201-7788" not in note and "***-***-7788" in note


def test_injected_notes_are_withheld_and_flagged(env):
    out = env.gateway.call("get_invoice", {"invoice_id": "INV-20011"})
    assert "ADMIN-OVERRIDE" not in json.dumps(out) and out["security_flags"] and "note" in out


def test_screen_off_shows_the_raw_text(make_env):
    e = make_env(mode="read_write", screen_untrusted=False)
    assert "ADMIN-OVERRIDE" in e.gateway.call("get_invoice", {"invoice_id": "INV-20011"})["data"]["invoice"]["notes"]


def test_retries_a_flaky_read_then_gives_up(env):
    env.backend.fail_next(2)
    assert env.gateway.call("get_customer", {"customer_id": "C-1001"})["ok"] and env.gateway.audit_log[-1]["attempts"] == 3
    env.backend.fail_next(3)
    assert env.gateway.call("get_customer", {"customer_id": "C-1001"})["error"]["code"] == "upstream_unavailable"


def test_backend_404_becomes_not_found(env):
    assert env.gateway.call("get_customer", {"customer_id": "C-9999"})["error"]["code"] == "not_found"


def test_audit_log_masks_pii_and_never_records_tokens(env):
    args = {"dispute_id": "D-3001", "text": "Call Dana at 555-201-7788"}
    pid = env.gateway.call("create_dispute_note", args)["pending_id"]
    token = env.gateway.operator_token(pid)
    env.gateway.call("create_dispute_note", {**args, "confirmation_token": token})
    log = env.audit_path.read_text()
    lines = [json.loads(line) for line in log.splitlines()]
    assert "555-201-7788" not in log and token not in log and "CONF-" not in log
    assert [line["outcome"] for line in lines] == ["pending", "committed"] and all("latency_ms" in line for line in lines)


def test_load_secret_creates_then_reuses_a_file(tmp_path, monkeypatch):
    monkeypatch.delenv("OPSAGENT_APPROVAL_SECRET", raising=False)
    first = load_secret(tmp_path / "s")
    assert first and load_secret(tmp_path / "s") == first
    monkeypatch.setenv("OPSAGENT_APPROVAL_SECRET", "from-env")
    assert load_secret(tmp_path / "s") == b"from-env"
