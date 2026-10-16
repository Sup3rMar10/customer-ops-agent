"""The tool catalogue: names, descriptions, argument rules. One definition feeds the validator, the model's
tool list and the MCP server, so they cannot drift apart (a test checks the MCP side against this file)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CUSTOMER_ID = r"^C-\d{4}$"
INVOICE_ID = r"^INV-\d{5}$"
DISPUTE_ID = r"^D-\d{4}$"


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Param:
    type: str                      # "string" or "integer"
    description: str
    required: bool = False
    pattern: str | None = None
    enum: tuple[str, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    min_length: int = 0
    max_length: int | None = None
    default: Any = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: dict[str, Param] = field(default_factory=dict)
    write: bool = False

    def input_schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for name, p in self.params.items():
            prop: dict[str, Any] = {"type": p.type, "description": p.description}
            if p.pattern:
                prop["pattern"] = p.pattern
            if p.enum:
                prop["enum"] = list(p.enum)
            if p.minimum is not None:
                prop["minimum"] = p.minimum
            if p.maximum is not None:
                prop["maximum"] = p.maximum
            if p.type == "string":
                prop["minLength"] = p.min_length
                if p.max_length:
                    prop["maxLength"] = p.max_length
            props[name] = prop
        return {"type": "object", "properties": props, "additionalProperties": False,
                "required": [n for n, p in self.params.items() if p.required]}


_CUSTOMER = Param("string", "Customer id, for example C-1003.", True, pattern=CUSTOMER_ID)
_TOKEN = Param("string", "Confirmation token. Leave empty to propose the action. Only a human operator can "
                         "obtain a token, so never invent one.", max_length=64)
_INVOICE_STATUS = ("open", "overdue", "paid", "partially_paid", "void", "disputed", "unpaid")

TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec("search_customers", "Find customers by (part of) their name. Use this when you only have a name.", {
        "query": Param("string", "Part of the customer name, 2 to 60 characters.", True, min_length=2, max_length=60),
        "limit": Param("integer", "Maximum matches to return.", minimum=1, maximum=10, default=5)}),
    ToolSpec("get_customer", "Get a customer's profile, billing contacts and balance summary.",
             {"customer_id": _CUSTOMER}),
    ToolSpec("list_invoices", "List a customer's invoices, optionally filtered by status ('unpaid' means open, "
                              "overdue or partially paid).", {
        "customer_id": _CUSTOMER,
        "status": Param("string", "Only invoices in this status.", enum=_INVOICE_STATUS),
        "limit": Param("integer", "Maximum invoices to return.", minimum=1, maximum=50, default=20)}),
    ToolSpec("get_invoice", "Get one invoice with its payments and disputes.",
             {"invoice_id": Param("string", "Invoice id, for example INV-20002.", True, pattern=INVOICE_ID)}),
    ToolSpec("list_disputes", "List a customer's disputes with their notes, optionally for one invoice.", {
        "customer_id": _CUSTOMER,
        "status": Param("string", "Only disputes in this status.", enum=("open", "resolved", "rejected")),
        "invoice_id": Param("string", "Only disputes on this invoice.", pattern=INVOICE_ID)}),
    ToolSpec("payment_history", "A customer's recent payments and how late they usually pay.", {
        "customer_id": _CUSTOMER,
        "limit": Param("integer", "Maximum payments to list.", minimum=1, maximum=50, default=10)}),
    ToolSpec("create_dispute_note", "WRITE. Add a note to a dispute. Needs human approval: the first call only "
                                    "proposes the change and returns a pending id.", {
        "dispute_id": Param("string", "Dispute id, for example D-3001.", True, pattern=DISPUTE_ID),
        "text": Param("string", "The note, 1 to 500 characters.", True, min_length=1, max_length=500),
        "confirmation_token": _TOKEN}, write=True),
    ToolSpec("send_reminder_draft", "WRITE. Create a payment reminder DRAFT for an unpaid invoice. It is never "
                                    "sent. Needs human approval: the first call only proposes it.", {
        "invoice_id": Param("string", "Invoice id, for example INV-20002.", True, pattern=INVOICE_ID),
        "tone": Param("string", "Tone of the draft.", enum=("friendly", "firm"), default="friendly"),
        "confirmation_token": _TOKEN}, write=True),
)

BY_NAME = {t.name: t for t in TOOLS}
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def validate(spec: ToolSpec, args: Any) -> dict[str, Any]:
    """Check arguments against the spec. Unknown arguments, wrong types and out-of-range values are rejected,
    not coerced. Returns a clean copy with defaults left out (the backend applies its own)."""
    if not isinstance(args, dict):
        raise ToolError("invalid_arguments", "arguments must be an object")
    for name in args:
        if name not in spec.params:
            raise ToolError("invalid_arguments", f"unknown argument {name!r} for {spec.name}")
    clean: dict[str, Any] = {}
    for name, p in spec.params.items():
        value = args.get(name)
        if value is None or (value == "" and not p.required):   # an empty optional argument means "not given"
            if p.required:
                raise ToolError("invalid_arguments", f"{name} is required")
            continue
        if p.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolError("invalid_arguments", f"{name} must be an integer")
            if p.minimum is not None and value < p.minimum or p.maximum is not None and value > p.maximum:
                raise ToolError("invalid_arguments", f"{name} must be between {p.minimum} and {p.maximum}")
        else:
            if not isinstance(value, str):
                raise ToolError("invalid_arguments", f"{name} must be a string")
            value = value.strip()
            if _CONTROL.search(value):
                raise ToolError("invalid_arguments", f"{name} contains control characters")
            if len(value) < p.min_length or (p.max_length and len(value) > p.max_length):
                raise ToolError("invalid_arguments", f"{name} must be {p.min_length} to {p.max_length} characters")
            if p.pattern and not re.fullmatch(p.pattern, value):
                raise ToolError("invalid_arguments", f"{name} is not a valid id (expected pattern {p.pattern})")
            if p.enum and value not in p.enum:
                raise ToolError("invalid_arguments", f"{name} must be one of {', '.join(p.enum)}")
        clean[name] = value
    return clean
