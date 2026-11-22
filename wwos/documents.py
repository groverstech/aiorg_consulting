"""Business document generation (proposal, statement of work, invoice, status report).

Templates are Jinja2 with autoescaping, rendered to both HTML (for clients / PDF
printing) and Markdown (for agents and email). Inputs are validated per kind so
a document never goes out with missing commercial terms.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy.orm import Session

from .models import Deal, Document

TEMPLATE_DIR = Path(__file__).parent / "templates" / "documents"

REQUIRED_FIELDS: dict[str, list[str]] = {
    "proposal": ["client_name", "objective", "scope_items", "fee", "currency", "valid_days"],
    "sow": ["client_name", "deliverables", "milestones", "fee", "currency", "start_date"],
    "invoice": ["client_name", "invoice_number", "line_items", "currency", "due_days"],
    "status_report": ["client_name", "period", "progress", "risks", "next_steps"],
}

COMPANY = {
    "name": "White Waters Consulting Group",
}


class DocumentError(ValueError):
    pass


def _env(autoescape: bool) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        # Explicit boolean: select_autoescape() matches by extension and would miss *.html.j2.
        autoescape=autoescape,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _money(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
    except InvalidOperation as e:
        raise DocumentError(f"invalid amount {value!r}") from e
    if not d.is_finite():
        raise DocumentError(f"invalid amount {value!r}")
    if d < 0:
        raise DocumentError("amounts cannot be negative")
    return d.quantize(Decimal("0.01"))


def _prepare(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    if kind not in REQUIRED_FIELDS:
        raise DocumentError(f"unknown document kind {kind!r}")
    missing = [f for f in REQUIRED_FIELDS[kind] if data.get(f) in (None, "", [])]
    if missing:
        raise DocumentError(f"{kind} is missing required fields: {', '.join(missing)}")
    ctx = dict(data)
    ctx["company"] = COMPANY
    ctx["issued"] = data.get("issued") or date.today().isoformat()
    if "fee" in ctx:
        ctx["fee"] = _money(ctx["fee"])
    if kind == "invoice":
        items = []
        for item in ctx["line_items"]:
            qty = _money(item.get("quantity", 1))
            unit = _money(item["unit_price"])
            items.append({"description": item["description"], "quantity": qty, "unit_price": unit,
                          "amount": (qty * unit).quantize(Decimal("0.01"))})
        subtotal = sum((i["amount"] for i in items), Decimal("0.00"))
        tax_rate = _money(ctx.get("tax_rate", 0))
        tax = (subtotal * tax_rate / 100).quantize(Decimal("0.01"))
        ctx.update(line_items=items, subtotal=subtotal, tax_rate=tax_rate, tax=tax, total=subtotal + tax)
    return ctx


def render(kind: str, data: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    ctx = _prepare(kind, data)
    html = _env(True).get_template(f"{kind}.html.j2").render(**ctx)
    md = _env(False).get_template(f"{kind}.md.j2").render(**ctx)
    return html, md, ctx


def generate(s: Session, *, kind: str, title: str, data: dict[str, Any], deal_id: str | None = None,
             actor: str = "system") -> Document:
    if deal_id is not None and s.get(Deal, deal_id) is None:
        raise DocumentError("deal not found")
    html, md, ctx = render(kind, data)
    stored = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in data.items()}
    if kind == "invoice":
        stored["total"] = str(ctx["total"])
    doc = Document(kind=kind, title=title, deal_id=deal_id, html=html, markdown=md, data=stored,
                   created_by=actor)
    s.add(doc)
    s.flush()
    return doc
