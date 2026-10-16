"""The agent loop, and two stand-ins for a language model.

The loop is model-agnostic: `llm(messages, tools)` returns either a tool call or final text. Three
implementations ship:
  RulePlanner    deterministic, offline, no key. Reads the question with regexes. This is what the demo and
                 the evals use, so every number in the README can be reproduced without an API key.
  GullibleLLM    a red-team double: does whatever instructions it finds in tool output, like a badly
                 prompted model might. It exists to test the guardrails, not to answer questions.
  AnthropicLLM   a real model, off by default (see llm_anthropic.py).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .backend import money


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass
class Step:
    call: ToolCall | None = None
    text: str | None = None


LLM = Callable[[list[dict[str, Any]], list[dict[str, Any]]], Step]


class ToolClient(Protocol):
    def list_tools(self) -> list[dict[str, Any]]: ...
    def call(self, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass
class AgentResult:
    question: str
    answer: str
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    stopped: str = "final"            # "final" or "max_steps"

    @property
    def tool_names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    @property
    def pending_ids(self) -> list[str]:
        return [r["pending_id"] for _, _, r in self.calls if r.get("status") == "pending_approval"]


class Agent:
    def __init__(self, tools: ToolClient, llm: LLM, max_steps: int = 8) -> None:
        self.tools, self.llm, self.max_steps = tools, llm, max_steps

    def run(self, question: str) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        specs = self.tools.list_tools()
        result = AgentResult(question, "")
        for _ in range(self.max_steps):
            step = self.llm(messages, specs)
            if step.call is None:
                result.answer = step.text or ""
                return result
            outcome = self.tools.call(step.call.name, step.call.args)
            result.calls.append((step.call.name, step.call.args, outcome))
            messages.append({"role": "tool", "name": step.call.name, "args": step.call.args, "result": outcome})
        result.stopped, result.answer = "max_steps", "I could not finish within the step limit."
        return result


# ---------------------------------------------------------------------------------------------------
# Reading a question
# ---------------------------------------------------------------------------------------------------
_CUST_ID = re.compile(r"\bC-\d+\b", re.I)
_INV_ID = re.compile(r"\bINV-\d+\b", re.I)
_DIS_ID = re.compile(r"\bD-\d+\b", re.I)
_TOKEN = re.compile(r"\bCONF-[A-Za-z0-9]{6,}\b", re.I)

_NOT_NAMES = {w.lower() for w in (
    "what show does do how is are list draft add why who can please tell give summarize summarise get find "
    "when has have which create write pull look check send remind any the our my their a an i we and or of "
    "for to on in at by with from about that this those these it its as if then also just ap ach po usd inv "
    "net january february march april may june july august september october november december monday tuesday "
    "wednesday thursday friday dispute disputes invoice invoices payment payments customer account balance "
    "contact contacts reminder note summary overview status history details detail").split()}

_REFUSE_DESTRUCTIVE = re.compile(
    r"\b(delete|remove|cancel|void|refund|write[- ]?off|waive|forgive|reverse|credit memo|close|resolve|"
    r"change|edit|modify|update|overwrite|reopen)\b.{0,40}\b(invoice|dispute|balance|payment|amount|due date|"
    r"customer|account|record|terms)\b", re.I | re.S)
_REFUSE_EMAIL = re.compile(r"\b(send|forward|resend)\b|\b(e-?mail|text|message)\s+(the|them|him|her|this|that|our)\b", re.I)
_NOTE = re.compile(r"\b(add|append|log|leave|put|record|write|attach)\b.{0,30}\bnote\b", re.I | re.S)
_REMINDER = re.compile(r"\b(remind|reminder|dunning|nudge|chase|follow[- ]?up|payment request)\b", re.I)
_PAYMENTS = re.compile(
    r"payment history|payment record|payments? (history|record)|recent payments?|last payment|"
    r"\bpay(s|ing)?\b[^.?]{0,30}\b(late|on time|early|reliably|slow)|\bpaid (late|on time|early)\b|"
    r"how (often|quickly|fast|reliably)\b[^.?]*\bpay|\bpayment behaviou?r\b|\blate payer\b", re.I)
_BALANCE = re.compile(r"\b(owe|owes|owed|balance|outstanding|unpaid|overdue|past due|open invoices?|"
                      r"receivables?|aging|ageing|not paid|haven't paid|hasn't paid)\b", re.I)
_OVERDUE_WORDS = re.compile(r"\b(overdue|past due|late invoices?)\b", re.I)
_CONTACT = re.compile(r"\b(contact|contacts|email|e-mail|phone|number|who do i|who should i)\b", re.I)


def extract_name(question: str) -> str | None:
    """A customer name from a question: a quoted phrase, else the longest run of capitalised words."""
    quoted = re.search(r"\"([^\"]{2,60})\"|“([^”]{2,60})”", question)
    if quoted:
        return (quoted.group(1) or quoted.group(2)).strip()
    stripped = _TOKEN.sub(" ", _DIS_ID.sub(" ", _INV_ID.sub(" ", _CUST_ID.sub(" ", question))))
    words = re.findall(r"[A-Za-z0-9&'\-]+|[^\sA-Za-z0-9]", stripped)
    best: list[str] = []
    run: list[str] = []
    for i, w in enumerate(words):
        is_name = w[0].isupper() and w.lower() not in _NOT_NAMES and not w.isdigit()
        joiner = w.lower() in ("&", "of", "and") and run and i + 1 < len(words) and words[i + 1][0].isupper()
        if is_name or joiner:
            run.append(w)
        else:
            if len(run) > len(best):
                best = run
            run = []
    if len(run) > len(best):
        best = run
    if best:
        return " ".join(best)
    low = re.search(r"\b(?:for|of|about|does|has|is|from)\s+([a-z][a-z0-9&' ]{2,40}?)"
                    r"(?=\s+(?:owe|owes|have|has|pay|paid|still|overdue|unpaid|invoices?|balance|outstanding|late|"
                    r"history|any|in)\b|[?.!,]|$)", question)
    return low.group(1).strip() if low and low.group(1).lower() not in _NOT_NAMES else None


@dataclass
class Ctx:
    question: str
    low: str
    customer_id: str | None
    invoice_id: str | None
    dispute_id: str | None
    token: str | None
    name: str | None


def read_question(question: str) -> Ctx:
    def first(rx: re.Pattern[str]) -> str | None:
        m = rx.search(question)
        return m.group(0).upper() if m else None
    cid, iid, did = first(_CUST_ID), first(_INV_ID), first(_DIS_ID)
    token = _TOKEN.search(question)
    return Ctx(question, question.lower(), cid, iid, did, token.group(0).upper() if token else None,
               None if cid else extract_name(question))


# ---------------------------------------------------------------------------------------------------
# Planning and answering
# ---------------------------------------------------------------------------------------------------
def _call(name: str, **args: Any) -> Step:
    return Step(call=ToolCall(name, args))


def _final(text: str) -> Step:
    return Step(text=text)


def _last(done: list[tuple[str, dict, dict]], name: str, **where: Any) -> dict | None:
    for n, a, r in reversed(done):
        if n == name and all(a.get(k) == v for k, v in where.items()):
            return r
    return None


def _explain(result: dict) -> str:
    err = result.get("error", {})
    code, msg = err.get("code"), err.get("message", "")
    return {
        "not_found": f"I couldn't find that: {msg}.",
        "invalid_arguments": f"That doesn't look right: {msg}. Customer ids look like C-1003, invoices INV-20002, disputes D-3001.",
        "upstream_unavailable": "The billing system isn't responding right now, so I can't answer that. Please try again in a few minutes.",
        "writes_disabled": "This assistant is read-only right now, so I can't add or draft anything. I can still look things up.",
        "confirmation_invalid": "That confirmation token isn't valid for this action, so nothing was changed.",
        "confirmation_expired": "That proposal expired, so nothing was changed. Ask me to propose it again.",
        "confirmation_locked": "Too many wrong tokens, so this proposal is locked. Ask me to propose it again.",
        "rate_limited": "There are too many unapproved proposals waiting. An operator needs to approve or reject some first.",
        "conflict": f"I can't do that: {msg}.",
    }.get(code, f"The request failed ({code}): {msg}")


def _invoice_line(i: dict) -> str:
    line = f"- {i['id']}: {money(i['balance_cents'])} outstanding of {money(i['amount_cents'])}, due {i['due_date']}"
    if i["days_overdue"]:
        line += f", {i['days_overdue']} days overdue"
    return line + (" (in dispute)" if i["in_dispute"] else "")


def _dispute_line(d: dict) -> str:
    line = (f"- {d['id']} on {d['invoice_id']}: {d['reason_code'].replace('_', ' ')}, {d['status']}, "
            f"{money(d['amount_cents'])} disputed (opened {d['opened_on']})")
    if d["notes"]:
        line += f". Latest note: {d['notes'][-1]['text']}"
    return line


HELP = ("I can look up a customer's invoices, overdue balance, disputes, contacts and payment history, and "
        "prepare payment-reminder drafts or dispute notes for an operator to approve. Try: "
        "\"What does Acme Industrial Supply owe?\"")
REFUSAL = ("I can't do that. I can't change, void, refund, waive or delete records, and I can't send email. "
           "I can read data and prepare drafts for a human to approve.")


def _n(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


class RulePlanner:
    """Deterministic planner. Stateless: it re-reads the question and the tool results so far every step."""

    def __call__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Step:
        done = [(m["name"], m["args"], m["result"]) for m in messages if m["role"] == "tool"]
        return self.plan(read_question(messages[0]["content"]), done, {t["name"] for t in tools})

    def plan(self, c: Ctx, done: list[tuple[str, dict, dict]], available: set[str]) -> Step:
        if c.dispute_id and _NOTE.search(c.low):
            return self._add_note(c, done, available)
        if _REMINDER.search(c.low):
            return self._reminder(c, done, available)
        if _REFUSE_DESTRUCTIVE.search(c.low) or _REFUSE_EMAIL.search(c.low):
            return _final(REFUSAL + " " + HELP)
        if c.invoice_id and re.search(r"\bdisput|\bwhy\b", c.low):
            return self._invoice_dispute(c, done)
        if "dispute" in c.low:
            return self._disputes(c, done)
        if _PAYMENTS.search(c.low):
            return self._payments(c, done)
        if c.invoice_id and not _CUST_ID.search(c.question):
            return self._invoice_detail(c, done)
        if _BALANCE.search(c.low):
            return self._balance(c, done)
        if re.search(r"\binvoices?\b", c.low) and (c.customer_id or c.name):
            return self._invoices(c, done)
        if _CONTACT.search(c.low):
            return self._contacts(c, done)
        if c.customer_id or c.name:
            return self._profile(c, done)
        return _final(HELP)

    # -- shared: turn "Acme" into C-1003 ---------------------------------------------------------
    def _customer(self, c: Ctx, done) -> tuple[str | None, Step | None]:
        if c.customer_id:
            return c.customer_id, None
        if not c.name:
            return None, _final("Which customer do you mean? Give me a name or an id like C-1003.")
        found = _last(done, "search_customers", query=c.name)
        if found is None:
            return None, _call("search_customers", query=c.name)
        if not found["ok"]:
            return None, _final(_explain(found))
        matches = found["data"]["customers"]
        exact = [m for m in matches if m["name"].lower() == c.name.lower()]
        if len(exact) == 1 or len(matches) == 1:
            return (exact or matches)[0]["id"], None
        if not matches:
            return None, _final(f"I couldn't find a customer matching '{c.name}'. Check the spelling or give me the id.")
        options = ", ".join(f"{m['name']} ({m['id']})" for m in matches)
        return None, _final(f"'{c.name}' matches more than one customer: {options}. Which one do you mean?")

    @staticmethod
    def _name(done, cid: str) -> str:
        got = _last(done, "get_customer", customer_id=cid)
        if got and got["ok"]:
            return f"{got['data']['customer']['name']} ({cid})"
        for n, _, r in done:
            if n == "search_customers" and r["ok"]:
                for m in r["data"]["customers"]:
                    if m["id"] == cid:
                        return f"{m['name']} ({cid})"
        return cid

    # -- intents ---------------------------------------------------------------------------------
    def _balance(self, c: Ctx, done) -> Step:
        cid, step = self._customer(c, done)
        if step:
            return step
        status = "overdue" if _OVERDUE_WORDS.search(c.low) else "unpaid"
        r = _last(done, "list_invoices", customer_id=cid, status=status)
        if r is None:
            return _call("list_invoices", customer_id=cid, status=status)
        if not r["ok"]:
            return _final(_explain(r))
        d, who = r["data"], self._name(done, cid)
        if not d["total_matching"]:
            return _final(f"{who} has no {status} invoices.")
        lines = "\n".join(_invoice_line(i) for i in d["invoices"])
        more = f"\n(showing {len(d['invoices'])} of {d['total_matching']})" if d["total_matching"] > len(d["invoices"]) else ""
        return _final(f"{who} has {_n(d['total_matching'], status + ' invoice')} "
                      f"totaling {money(d['total_balance_cents'])} outstanding:\n{lines}{more}")

    def _invoices(self, c: Ctx, done) -> Step:
        cid, step = self._customer(c, done)
        if step:
            return step
        r = _last(done, "list_invoices", customer_id=cid)
        if r is None:
            return _call("list_invoices", customer_id=cid)
        if not r["ok"]:
            return _final(_explain(r))
        d, who = r["data"], self._name(done, cid)
        if not d["total_matching"]:
            return _final(f"{who} has no invoices.")
        lines = "\n".join(f"- {i['id']}: {i['status'].replace('_', ' ')}, {money(i['amount_cents'])}, due {i['due_date']}"
                          + (" (in dispute)" if i["in_dispute"] else "") for i in d["invoices"])
        more = f"\n(showing {len(d['invoices'])} of {d['total_matching']})" if d["total_matching"] > len(d["invoices"]) else ""
        return _final(f"{who} has {_n(d['total_matching'], 'invoice')}:\n{lines}{more}")

    def _payments(self, c: Ctx, done) -> Step:
        cid, step = self._customer(c, done)
        if step:
            return step
        r = _last(done, "payment_history", customer_id=cid)
        if r is None:
            return _call("payment_history", customer_id=cid)
        if not r["ok"]:
            return _final(_explain(r))
        s, who = r["data"]["summary"], self._name(done, cid)
        if not s["payment_count"]:
            return _final(f"{who} has no recorded payments.")
        avg = s["avg_days_late"]
        habit = ""
        if avg is not None:
            habit = (f" Of {_n(s['paid_invoice_count'], 'fully paid invoice')}, {s['late_invoice_count']} were paid late; "
                     f"on average they pay {abs(avg)} days {'after' if avg > 0 else 'before'} the due date.")
        lines = "\n".join(f"- {p['paid_on']}: {money(p['amount_cents'])} via {p['method']} for {p['invoice_id']}"
                          for p in r["data"]["payments"])
        return _final(f"{who} has made {_n(s['payment_count'], 'payment')} totaling {money(s['total_paid_cents'])}; "
                      f"the last was on {s['last_payment_on']}.{habit}\nRecent payments:\n{lines}")

    def _disputes(self, c: Ctx, done) -> Step:
        if c.dispute_id and not (c.customer_id or c.name or c.invoice_id):
            return _final(f"I can't look up {c.dispute_id} by itself. Tell me the customer or the invoice it belongs to.")
        if c.invoice_id:
            return self._invoice_dispute(c, done)
        cid, step = self._customer(c, done)
        if step:
            return step
        status = "open" if re.search(r"\bopen\b", c.low) else None
        args = {"customer_id": cid, **({"status": status} if status else {})}
        r = _last(done, "list_disputes", **args)
        if r is None:
            return _call("list_disputes", **args)
        if not r["ok"]:
            return _final(_explain(r))
        who, items = self._name(done, cid), r["data"]["disputes"]
        if not items:
            return _final(f"{who} has no {status + ' ' if status else ''}disputes on record.")
        return _final(f"{who} has {len(items)} {status + ' ' if status else ''}dispute{'s' * (len(items) != 1)}:\n"
                      + "\n".join(_dispute_line(d) for d in items))

    def _invoice_dispute(self, c: Ctx, done) -> Step:
        inv = _last(done, "get_invoice", invoice_id=c.invoice_id)
        if inv is None:
            return _call("get_invoice", invoice_id=c.invoice_id)
        if not inv["ok"]:
            return _final(_explain(inv))
        cid = inv["data"]["invoice"]["customer_id"]
        r = _last(done, "list_disputes", customer_id=cid, invoice_id=c.invoice_id)
        if r is None:
            return _call("list_disputes", customer_id=cid, invoice_id=c.invoice_id)
        if not r["ok"]:
            return _final(_explain(r))
        items = r["data"]["disputes"]
        if not items:
            return _final(f"{c.invoice_id} has no disputes on record.")
        return _final(f"{c.invoice_id} is disputed:\n" + "\n".join(_dispute_line(d) for d in items))

    def _invoice_detail(self, c: Ctx, done) -> Step:
        r = _last(done, "get_invoice", invoice_id=c.invoice_id)
        if r is None:
            return _call("get_invoice", invoice_id=c.invoice_id)
        if not r["ok"]:
            return _final(_explain(r))
        i, pays, disputes = r["data"]["invoice"], r["data"]["payments"], r["data"]["disputes"]
        text = (f"{i['id']} ({i['customer_id']}) is {i['status'].replace('_', ' ')}: {money(i['amount_cents'])} invoiced, "
                f"{money(i['paid_cents'])} paid, {money(i['balance_cents'])} outstanding, due {i['due_date']}"
                + (f" ({i['days_overdue']} days overdue)" if i["days_overdue"] else "") + ".")
        if pays:
            text += "\nPayments: " + "; ".join(f"{p['paid_on']} {money(p['amount_cents'])} ({p['method']})" for p in pays)
        if disputes:
            text += "\nDisputes: " + "; ".join(f"{d['id']} ({d['reason_code'].replace('_', ' ')}, {d['status']})" for d in disputes)
        if i["notes"]:
            text += f"\nNote on the invoice: {i['notes']}"
        return _final(text)

    def _contacts(self, c: Ctx, done) -> Step:
        cid, step = self._customer(c, done)
        if step:
            return step
        r = _last(done, "get_customer", customer_id=cid)
        if r is None:
            return _call("get_customer", customer_id=cid)
        if not r["ok"]:
            return _final(_explain(r))
        cust = r["data"]["customer"]
        lines = "\n".join(f"- {p['name']} ({p['role']}): {p['email']}, {p['phone']}" for p in cust["contacts"])
        return _final(f"Billing contacts for {cust['name']} ({cid}). Contact details are masked by policy:\n{lines}")

    def _profile(self, c: Ctx, done) -> Step:
        cid, step = self._customer(c, done)
        if step:
            return step
        for name, args in (("get_customer", {"customer_id": cid}),
                           ("list_invoices", {"customer_id": cid, "status": "unpaid"}),
                           ("list_disputes", {"customer_id": cid, "status": "open"})):
            r = _last(done, name, **args)
            if r is None:
                return _call(name, **args)
            if not r["ok"]:
                return _final(_explain(r))
        cust = _last(done, "get_customer", customer_id=cid)["data"]["customer"]
        invs = _last(done, "list_invoices", customer_id=cid, status="unpaid")["data"]
        disputes = _last(done, "list_disputes", customer_id=cid, status="open")["data"]["disputes"]
        s = cust["summary"]
        text = (f"{cust['name']} ({cid}): {cust['industry']}, {cust['tier']} tier, status {cust['status']}, net "
                f"{cust['payment_terms_days']} terms, credit limit {money(cust['credit_limit_cents'])}.\n"
                f"Unpaid balance {money(s['unpaid_balance_cents'])}, of which {money(s['overdue_balance_cents'])} is overdue, "
                f"across {_n(invs['total_matching'], 'invoice')}. {_n(s['open_dispute_count'], 'open dispute')}.")
        if invs["invoices"]:
            text += "\nUnpaid invoices:\n" + "\n".join(_invoice_line(i) for i in invs["invoices"])
        if disputes:
            text += "\nOpen disputes:\n" + "\n".join(_dispute_line(d) for d in disputes)
        return _final(text)

    # -- writes: the planner only ever proposes; a human approves ---------------------------------
    def _reminder(self, c: Ctx, done, available: set[str]) -> Step:
        if not c.invoice_id:
            return _final("Which invoice should the reminder be for? Give me an invoice id like INV-20002.")
        tone = "firm" if re.search(r"\b(firm|stern|strong|urgent|final)\b", c.low) else "friendly"
        inv = _last(done, "get_invoice", invoice_id=c.invoice_id)
        if inv is None:
            return _call("get_invoice", invoice_id=c.invoice_id)
        if not inv["ok"]:
            return _final(_explain(inv))
        i = inv["data"]["invoice"]
        if i["status"] in ("paid", "void"):
            return _final(f"{c.invoice_id} is {i['status']}, so there is nothing to remind anyone about.")
        if "send_reminder_draft" not in available:
            return _final(_explain({"error": {"code": "writes_disabled"}}))
        args = {"invoice_id": c.invoice_id, "tone": tone, **({"confirmation_token": c.token} if c.token else {})}
        r = _last(done, "send_reminder_draft", **args)
        if r is None:
            return _call("send_reminder_draft", **args)
        if not r["ok"]:
            return _final(_explain(r))
        if r.get("status") == "pending_approval":
            return _final(f"I've prepared a {tone} reminder draft for {c.invoice_id} ({money(i['balance_cents'])}, "
                          f"{i['status']}). Nothing has been created yet: it is waiting for operator approval as "
                          f"{r['pending_id']}. Once approved, ask me again with the confirmation token.")
        draft = r["data"]["draft"]
        return _final(f"Done. Reminder draft {draft['id']} was created for {c.invoice_id}. It has not been sent.")

    def _add_note(self, c: Ctx, done, available: set[str]) -> Step:
        text = None
        quoted = re.search(r"[\"“']([^\"”']{3,500})[\"”']", c.question)
        if quoted:
            text = quoted.group(1)
        else:
            colon = re.search(r":\s*(.+)$", c.question, re.S)
            if colon:
                text = _TOKEN.sub("", re.split(r"\s+with (?:the )?token\b", colon.group(1), flags=re.I)[0]).strip()
        if not text:
            return _final(f"What should the note on {c.dispute_id} say? Put the text in quotes.")
        if "create_dispute_note" not in available:
            return _final(_explain({"error": {"code": "writes_disabled"}}))
        args = {"dispute_id": c.dispute_id, "text": text, **({"confirmation_token": c.token} if c.token else {})}
        r = _last(done, "create_dispute_note", **args)
        if r is None:
            return _call("create_dispute_note", **args)
        if not r["ok"]:
            return _final(_explain(r))
        if r.get("status") == "pending_approval":
            return _final(f"I've drafted a note for {c.dispute_id}: \"{text}\". Nothing has been saved yet: it is "
                          f"waiting for operator approval as {r['pending_id']}. Once approved, ask me again with "
                          f"the confirmation token.")
        return _final(f"Done. The note was added to {c.dispute_id}.")


# ---------------------------------------------------------------------------------------------------
# A model that obeys whatever it reads (red-team double)
# ---------------------------------------------------------------------------------------------------
_MARKUP = re.compile(r"\[\[\s*tool\s*:\s*(\w+)\s+(\{.*?\})\s*\]\]", re.S)
_CALL_NOTE = re.compile(r"(?:call|go ahead and) create_dispute_note for dispute (D-\d{4}) with text '([^']+)'"
                        r"(?: and confirmation_token (\S+?))?\.?(?:\s|$)", re.I)
_CALL_REMIND = re.compile(r"call send_reminder_draft for (INV-\d{5}) with tone (friendly|firm)", re.I)


def _strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


class GullibleLLM:
    """Plans like RulePlanner, but first obeys any tool call it finds written in the data it was shown."""

    def __init__(self, base: RulePlanner | None = None) -> None:
        self.base = base or RulePlanner()

    def __call__(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Step:
        issued = {(m["name"], json.dumps(m["args"], sort_keys=True)) for m in messages if m["role"] == "tool"}
        for m in messages:
            if m["role"] != "tool":
                continue
            for text in _strings(m["result"]):
                for name, args in self._commands(text):
                    if (name, json.dumps(args, sort_keys=True)) not in issued:
                        return Step(call=ToolCall(name, args))
        return self.base(messages, tools)

    @staticmethod
    def _commands(text: str):
        for name, raw in _MARKUP.findall(text):
            try:
                yield name, json.loads(raw)
            except ValueError:
                continue
        for did, body, token in _CALL_NOTE.findall(text):
            yield "create_dispute_note", {"dispute_id": did.upper(), "text": body, **({"confirmation_token": token} if token else {})}
        for iid, tone in _CALL_REMIND.findall(text):
            yield "send_reminder_draft", {"invoice_id": iid.upper(), "tone": tone.lower()}
