"""A mock billing/ERP/CRM backend with a REST-shaped interface, in process, on SQLite.

It stands in for the customer's system of record: `handle("GET", "/customers/C-1003/invoices", {...})`
returns a status code and a JSON body, the way an HTTP API would. It knows nothing about masking,
confirmation or the agent. It returns raw data (real-looking emails and phone numbers included), and that
is the point: the safety layer lives in `gateway.py`, on our side of the wire.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .seed import AS_OF

UNPAID = ("open", "overdue", "partially_paid")
INVOICE_STATUSES = ("open", "overdue", "paid", "partially_paid", "void")


@dataclass
class Response:
    status: int
    body: dict[str, Any]


def _err(status: int, code: str, message: str) -> Response:
    return Response(status, {"error": {"code": code, "message": message}})


def money(cents: int) -> str:
    return f"${cents / 100:,.2f}"


class Backend:
    def __init__(self, db_path: str | Path, as_of: date = AS_OF,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.as_of = as_of
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.request_count = 0
        self._failures: list[int] = []
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Response]]] = [
            ("GET", re.compile(r"^/customers$"), self._search_customers),
            ("GET", re.compile(r"^/customers/([^/]+)$"), self._get_customer),
            ("GET", re.compile(r"^/customers/([^/]+)/invoices$"), self._list_invoices),
            ("GET", re.compile(r"^/customers/([^/]+)/disputes$"), self._list_disputes),
            ("GET", re.compile(r"^/customers/([^/]+)/payments$"), self._payments),
            ("GET", re.compile(r"^/invoices/([^/]+)$"), self._get_invoice),
            ("POST", re.compile(r"^/disputes/([^/]+)/notes$"), self._add_dispute_note),
            ("POST", re.compile(r"^/invoices/([^/]+)/reminders$"), self._create_reminder),
        ]

    # -- fault injection, so the retry path can be tested ---------------------------------------
    def fail_next(self, n: int = 1, status: int = 503) -> None:
        """Make the next `n` requests fail with `status` (a flaky upstream)."""
        self._failures.extend([status] * n)

    def close(self) -> None:
        self.conn.close()

    # -- the one entry point --------------------------------------------------------------------
    def handle(self, method: str, path: str, query: dict[str, str] | None = None,
               body: dict[str, Any] | None = None) -> Response:
        with self.lock:
            self.request_count += 1
            if self._failures:
                status = self._failures.pop(0)
                return _err(status, "upstream_unavailable", "The billing system is temporarily unavailable.")
            for verb, pattern, fn in self._routes:
                match = pattern.match(path)
                if match and verb == method:
                    return fn(*match.groups(), query=query or {}, body=body or {})
            if any(p.match(path) for _, p, _ in self._routes):
                return _err(405, "method_not_allowed", f"{method} is not supported on {path}")
            return _err(404, "not_found", f"No route for {path}")

    # -- shared helpers -------------------------------------------------------------------------
    def _status_of(self, row: sqlite3.Row, paid: int) -> str:
        if row["status"] == "void":
            return "void"
        if paid >= row["amount_cents"]:
            return "paid"
        if date.fromisoformat(row["due_date"]) < self.as_of:
            return "overdue"
        return "partially_paid" if paid else "open"

    def _invoice_view(self, row: sqlite3.Row, paid: int, in_dispute: bool) -> dict[str, Any]:
        status = self._status_of(row, paid)
        due = date.fromisoformat(row["due_date"])
        balance = 0 if status in ("paid", "void") else row["amount_cents"] - paid
        return {
            "id": row["id"], "customer_id": row["customer_id"], "po_number": row["po_number"],
            "issue_date": row["issue_date"], "due_date": row["due_date"], "status": status,
            "amount_cents": row["amount_cents"], "paid_cents": paid, "balance_cents": balance,
            "days_overdue": (self.as_of - due).days if status == "overdue" else 0,
            "in_dispute": in_dispute, "notes": row["notes"],
        }

    def _invoices_for(self, customer_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT i.*, COALESCE((SELECT SUM(amount_cents) FROM payments p WHERE p.invoice_id = i.id), 0) AS paid,
                      EXISTS(SELECT 1 FROM disputes d WHERE d.invoice_id = i.id AND d.status = 'open') AS disputed
               FROM invoices i WHERE i.customer_id = ? ORDER BY i.due_date, i.id""", (customer_id,)).fetchall()
        return [self._invoice_view(r, r["paid"], bool(r["disputed"])) for r in rows]

    def _customer_exists(self, customer_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM customers WHERE id = ?", (customer_id,)).fetchone() is not None

    @staticmethod
    def _int(query: dict[str, str], key: str, default: int, lo: int, hi: int) -> int | Response:
        raw = query.get(key)
        if raw is None:
            return default
        if not raw.lstrip("-").isdigit() or not lo <= int(raw) <= hi:
            return _err(400, "bad_request", f"{key} must be an integer from {lo} to {hi}")
        return int(raw)

    # -- GET handlers ---------------------------------------------------------------------------
    def _search_customers(self, *, query, body) -> Response:
        term = query.get("search", "").strip()
        if len(term) < 2:
            return _err(400, "bad_request", "search must be at least 2 characters")
        limit = self._int(query, "limit", 5, 1, 10)
        if isinstance(limit, Response):
            return limit
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.conn.execute(
            "SELECT id, name, industry, status FROM customers WHERE name LIKE ? ESCAPE '\\' OR id = ? ORDER BY name LIMIT ?",
            (f"%{escaped}%", term.upper(), limit)).fetchall()
        return Response(200, {"customers": [dict(r) for r in rows]})

    def _get_customer(self, customer_id: str, *, query, body) -> Response:
        row = self.conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        if row is None:
            return _err(404, "not_found", f"Customer {customer_id} does not exist")
        contacts = [dict(r) for r in self.conn.execute(
            "SELECT name, role, email, phone FROM contacts WHERE customer_id = ? ORDER BY id", (customer_id,))]
        invoices = self._invoices_for(customer_id)
        unpaid = [i for i in invoices if i["status"] in UNPAID]
        open_disputes = self.conn.execute(
            "SELECT COUNT(*) FROM disputes WHERE customer_id = ? AND status = 'open'", (customer_id,)).fetchone()[0]
        return Response(200, {"customer": {
            **{k: row[k] for k in ("id", "name", "industry", "tier", "status", "payment_terms_days", "credit_limit_cents")},
            "contacts": contacts,
            "summary": {
                "invoice_count": len(invoices),
                "unpaid_balance_cents": sum(i["balance_cents"] for i in unpaid),
                "overdue_balance_cents": sum(i["balance_cents"] for i in invoices if i["status"] == "overdue"),
                "open_dispute_count": open_disputes,
            }}})

    def _list_invoices(self, customer_id: str, *, query, body) -> Response:
        if not self._customer_exists(customer_id):
            return _err(404, "not_found", f"Customer {customer_id} does not exist")
        limit = self._int(query, "limit", 20, 1, 50)
        if isinstance(limit, Response):
            return limit
        wanted = query.get("status")
        items = self._invoices_for(customer_id)
        if wanted == "unpaid":
            items = [i for i in items if i["status"] in UNPAID]
        elif wanted == "disputed":
            items = [i for i in items if i["in_dispute"]]
        elif wanted:
            if wanted not in INVOICE_STATUSES:
                return _err(400, "bad_request", f"unknown status {wanted!r}")
            items = [i for i in items if i["status"] == wanted]
        return Response(200, {"invoices": items[:limit], "total_matching": len(items),
                              "total_balance_cents": sum(i["balance_cents"] for i in items)})

    def _get_invoice(self, invoice_id: str, *, query, body) -> Response:
        row = self.conn.execute(
            """SELECT i.*, COALESCE((SELECT SUM(amount_cents) FROM payments p WHERE p.invoice_id = i.id), 0) AS paid,
                      EXISTS(SELECT 1 FROM disputes d WHERE d.invoice_id = i.id AND d.status = 'open') AS disputed
               FROM invoices i WHERE i.id = ?""", (invoice_id,)).fetchone()
        if row is None:
            return _err(404, "not_found", f"Invoice {invoice_id} does not exist")
        payments = [dict(r) for r in self.conn.execute(
            "SELECT id, paid_on, amount_cents, method FROM payments WHERE invoice_id = ? ORDER BY paid_on", (invoice_id,))]
        disputes = [dict(r) for r in self.conn.execute(
            "SELECT id, status, reason_code, amount_cents FROM disputes WHERE invoice_id = ? ORDER BY id", (invoice_id,))]
        return Response(200, {"invoice": self._invoice_view(row, row["paid"], bool(row["disputed"])),
                              "payments": payments, "disputes": disputes})

    def _list_disputes(self, customer_id: str, *, query, body) -> Response:
        if not self._customer_exists(customer_id):
            return _err(404, "not_found", f"Customer {customer_id} does not exist")
        sql, args = "SELECT * FROM disputes WHERE customer_id = ?", [customer_id]
        if "status" in query:
            sql, args = sql + " AND status = ?", args + [query["status"]]
        if "invoice_id" in query:
            sql, args = sql + " AND invoice_id = ?", args + [query["invoice_id"]]
        out = []
        for d in self.conn.execute(sql + " ORDER BY opened_on, id", args).fetchall():
            notes = [dict(r) for r in self.conn.execute(
                "SELECT created_at, author, text FROM dispute_notes WHERE dispute_id = ? ORDER BY id", (d["id"],))]
            out.append({k: d[k] for k in d.keys()} | {"notes": notes})
        return Response(200, {"disputes": out})

    def _payments(self, customer_id: str, *, query, body) -> Response:
        if not self._customer_exists(customer_id):
            return _err(404, "not_found", f"Customer {customer_id} does not exist")
        limit = self._int(query, "limit", 10, 1, 50)
        if isinstance(limit, Response):
            return limit
        rows = self.conn.execute(
            """SELECT p.id, p.invoice_id, p.paid_on, p.amount_cents, p.method, i.due_date
               FROM payments p JOIN invoices i ON i.id = p.invoice_id
               WHERE p.customer_id = ? ORDER BY p.paid_on DESC, p.id DESC""", (customer_id,)).fetchall()
        payments = [{k: r[k] for k in ("id", "invoice_id", "paid_on", "amount_cents", "method")} for r in rows[:limit]]
        # Lateness is judged on invoices that are fully paid: the last payment against the due date.
        days_late = []
        for inv in self._invoices_for(customer_id):
            if inv["status"] == "paid":
                last = self.conn.execute("SELECT MAX(paid_on) FROM payments WHERE invoice_id = ?", (inv["id"],)).fetchone()[0]
                days_late.append((date.fromisoformat(last) - date.fromisoformat(inv["due_date"])).days)
        return Response(200, {"payments": payments, "summary": {
            "payment_count": len(rows), "total_paid_cents": sum(r["amount_cents"] for r in rows),
            "paid_invoice_count": len(days_late),
            "late_invoice_count": sum(1 for d in days_late if d > 0),
            "avg_days_late": round(sum(days_late) / len(days_late), 1) if days_late else None,
            "last_payment_on": rows[0]["paid_on"] if rows else None}})

    # -- POST handlers (the only ones that change anything) --------------------------------------
    def _add_dispute_note(self, dispute_id: str, *, query, body) -> Response:
        if self.conn.execute("SELECT 1 FROM disputes WHERE id = ?", (dispute_id,)).fetchone() is None:
            return _err(404, "not_found", f"Dispute {dispute_id} does not exist")
        text = str(body.get("text", "")).strip()
        if not 1 <= len(text) <= 500:
            return _err(400, "bad_request", "text must be 1 to 500 characters")
        created = self.clock().isoformat(timespec="seconds")
        cur = self.conn.execute("INSERT INTO dispute_notes (dispute_id, created_at, author, text) VALUES (?,?,?,?)",
                                (dispute_id, created, str(body.get("author", "opsagent")), text))
        self.conn.commit()
        return Response(201, {"note": {"id": cur.lastrowid, "dispute_id": dispute_id, "created_at": created, "text": text}})

    def _create_reminder(self, invoice_id: str, *, query, body) -> Response:
        inv = self.conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if inv is None:
            return _err(404, "not_found", f"Invoice {invoice_id} does not exist")
        paid = self.conn.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM payments WHERE invoice_id = ?", (invoice_id,)).fetchone()[0]
        view = self._invoice_view(inv, paid, False)
        if view["status"] not in UNPAID:
            return _err(409, "invoice_not_payable", f"Invoice {invoice_id} is {view['status']}; nothing to remind about")
        contact = self.conn.execute("SELECT name, email FROM contacts WHERE customer_id = ? ORDER BY id LIMIT 1",
                                    (inv["customer_id"],)).fetchone()
        tone = body.get("tone", "friendly")
        first = contact["name"].split()[0] if contact else "there"
        subject = f"Reminder: invoice {invoice_id} ({money(view['balance_cents'])}) due {view['due_date']}"
        if tone == "firm":
            text = (f"Hello {first}, invoice {invoice_id} for {money(view['balance_cents'])} is "
                    f"{'now ' + str(view['days_overdue']) + ' days past due' if view['status'] == 'overdue' else 'due on ' + view['due_date']}. "
                    "Please arrange payment this week or tell us what is blocking it.")
        else:
            text = (f"Hi {first}, a quick note that invoice {invoice_id} for {money(view['balance_cents'])} "
                    f"{'was due on ' if view['status'] == 'overdue' else 'is due on '}{view['due_date']}. "
                    "If it is already on its way, thank you, and please ignore this.")
        count = self.conn.execute("SELECT COUNT(*) FROM reminder_drafts").fetchone()[0]
        draft_id = f"RD-{7001 + count}"
        created = self.clock().isoformat(timespec="seconds")
        self.conn.execute("INSERT INTO reminder_drafts VALUES (?,?,?,?,?,?,?,?,?)",
                          (draft_id, invoice_id, inv["customer_id"], created, tone,
                           contact["email"] if contact else "", subject, text, "draft"))
        self.conn.commit()
        return Response(201, {"draft": {"id": draft_id, "invoice_id": invoice_id, "status": "draft", "tone": tone,
                                        "to": contact["email"] if contact else None, "subject": subject, "body": text}})
