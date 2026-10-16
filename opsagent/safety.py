"""Output safety: PII masking and a screen for instruction-like text in free-text fields.

The screen is a speed bump and nothing more. Regexes will miss paraphrases. The controls that actually
bound the damage are structural (read-only by default, writes only by approval) and live in `gateway.py`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
_PHONE = re.compile(r"(?<!\d)(?:\(\d{3}\)\s?|\d{3}[-. ])\d{3}[-. ]\d{4}(?!\d)")

WITHHELD = "[withheld: this free text contained instruction-like content and was not shown]"

# Fields that people or customers type into. Everything under these keys is untrusted data, never instructions.
UNTRUSTED_KEYS = {"notes", "note", "text"}

_TOOLS = r"(search_customers|get_customer|list_invoices|get_invoice|list_disputes|payment_history|create_dispute_note|send_reminder_draft)"
_INJECTION_RULES = {
    "override_instructions": re.compile(r"\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|earlier|all)\b.{0,20}\b(instructions?|prompts?|rules?)\b", re.I | re.S),
    "role_header": re.compile(r"(^|\n)\s*(system|assistant|developer)\s*:", re.I),
    "role_reassign": re.compile(r"\byou are (now|no longer)\b", re.I),
    "tool_markup": re.compile(r"\[\[\s*tool\s*:", re.I),
    "tool_request": re.compile(r"\b(call|invoke|run|execute|use)\b[^.\n]{0,40}\b" + _TOOLS + r"\b", re.I),
    "token_mention": re.compile(r"confirmation[_ ]?token", re.I),
    "mode_switch": re.compile(r"\b(admin|developer|debug)[- ]?(mode|override)\b", re.I),
    "secrecy": re.compile(r"\b(do not|don't|never)\b[^.\n]{0,20}\b(tell|inform|mention|show)\b[^.\n]{0,20}\b(user|human|operator)\b", re.I),
}


def mask_name(name: str) -> str:
    parts = name.split()
    return f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else name


def mask_text(text: str) -> str:
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    return _PHONE.sub(lambda m: "***-***-" + m.group(0)[-4:], text)


def injection_rule(text: str) -> str | None:
    """Name of the first rule that matches, or None."""
    for name, pattern in _INJECTION_RULES.items():
        if pattern.search(text):
            return name
    return None


@dataclass
class SafetyConfig:
    mask_pii: bool = True
    screen_untrusted: bool = True


def clean_output(obj: Any, cfg: SafetyConfig) -> tuple[Any, list[dict[str, str]]]:
    """Mask PII everywhere and withhold instruction-like free text. Returns the cleaned copy and the flags raised."""
    flags: list[dict[str, str]] = []

    def walk(value: Any, path: str, untrusted: bool, in_contacts: bool = False) -> Any:
        if isinstance(value, str):
            if untrusted and cfg.screen_untrusted:
                rule = injection_rule(value)
                if rule:
                    flags.append({"path": path, "rule": rule})
                    return WITHHELD
            return mask_text(value) if cfg.mask_pii else value
        if isinstance(value, dict):
            out = {}
            for key, child in value.items():
                if in_contacts and key == "name" and cfg.mask_pii and isinstance(child, str):
                    out[key] = mask_name(child)
                else:
                    out[key] = walk(child, f"{path}.{key}" if path else key, untrusted or key in UNTRUSTED_KEYS,
                                    in_contacts=key == "contacts")
            return out
        if isinstance(value, list):
            return [walk(item, f"{path}[{i}]", untrusted, in_contacts) for i, item in enumerate(value)]
        return value

    return walk(obj, "", False), flags
