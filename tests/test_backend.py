import hashlib
import sqlite3

from opsagent.backend import Backend
from opsagent.seed import build_database


def dump_hash(path):
    conn = sqlite3.connect(path)
    h = hashlib.sha256()
    for table in ("customers", "contacts", "invoices", "payments", "disputes", "dispute_notes"):
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2"):
            h.update(repr(row).encode())
    conn.close()
    return h.hexdigest()


def test_seed_is_deterministic(tmp_path):
    a = build_database(tmp_path / "a.db", seed=42)
    b = build_database(tmp_path / "b.db", seed=42)
    c = build_database(tmp_path / "c.db", seed=7)
    assert dump_hash(a) == dump_hash(b)
    assert dump_hash(a) != dump_hash(c)


def test_seed_shape(template):
    conn = sqlite3.connect(template)
    assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 30
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] > 150
    # every email in the data is on a reserved .example domain: nothing here can belong to a real person
    assert all(e.endswith(".example") for (e,) in conn.execute("SELECT email FROM contacts"))


def test_planted_invoice_numbers(env):
    inv = env.backend.handle("GET", "/invoices/INV-20005").body["invoice"]
    assert (inv["status"], inv["paid_cents"], inv["balance_cents"], inv["days_overdue"]) == ("overdue", 1_500_000, 700_000, 97)


def test_unpaid_total_matches_independent_sql(env):
    body = env.backend.handle("GET", "/customers/C-1003/invoices", {"status": "unpaid"}).body
    assert body["total_balance_cents"] == 3_565_000 and body["total_matching"] == 3


def test_filters_and_pagination(env):
    r = env.backend.handle("GET", "/customers/C-1006/invoices", {"limit": "1"}).body
    assert len(r["invoices"]) == 1 and r["total_matching"] == 3
    assert env.backend.handle("GET", "/customers/C-1006/invoices", {"status": "void"}).body["total_matching"] == 1
    assert env.backend.handle("GET", "/customers/C-1003/invoices", {"status": "disputed"}).body["total_matching"] == 1


def test_http_style_errors(env):
    assert env.backend.handle("GET", "/customers/C-9999").status == 404
    assert env.backend.handle("GET", "/nope").status == 404
    assert env.backend.handle("DELETE", "/customers/C-1001").status == 405
    assert env.backend.handle("GET", "/customers/C-1001/invoices", {"limit": "0"}).status == 400
    assert env.backend.handle("GET", "/customers/C-1001/invoices", {"status": "bogus"}).status == 400
    assert env.backend.handle("GET", "/customers", {"search": "a"}).status == 400


def test_search_escapes_like_wildcards(env):
    assert env.backend.handle("GET", "/customers", {"search": "%%"}).body["customers"] == []
    assert len(env.backend.handle("GET", "/customers", {"search": "Northwind"}).body["customers"]) == 2


def test_payment_summary(env):
    s = env.backend.handle("GET", "/customers/C-1004/payments").body["summary"]
    assert s["avg_days_late"] == -2.5 and s["late_invoice_count"] == 0 and s["total_paid_cents"] == 810_000


def test_fault_injection(env):
    env.backend.fail_next(2, status=503)
    assert [env.backend.handle("GET", "/customers/C-1001").status for _ in range(3)] == [503, 503, 200]


def test_reminder_refuses_paid_invoice(env):
    assert env.backend.handle("POST", "/invoices/INV-20001/reminders", body={"tone": "firm"}).status == 409
    r = env.backend.handle("POST", "/invoices/INV-20002/reminders", body={"tone": "firm"})
    assert r.status == 201 and r.body["draft"]["status"] == "draft" and "$8,250.00" in r.body["draft"]["subject"]
