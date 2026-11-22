from decimal import Decimal

import pytest

from wwos import crm, documents
from wwos.db import tenant_session
from wwos.models import Contact


def test_contact_validation_and_dedupe(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        with pytest.raises(crm.CRMValidationError):
            crm.upsert_contact(s, email="not-an-email", first_name="A")
        with pytest.raises(crm.CRMValidationError):
            crm.upsert_contact(s, email="a@b.com", first_name="  ")
        c1, created1 = crm.upsert_contact(s, email="Jane@Acme.com ", first_name="Jane", company="Acme")
        c2, created2 = crm.upsert_contact(s, email="jane@acme.com", first_name="Janet")
        assert created1 and not created2 and c1.id == c2.id
        assert c1.email == "jane@acme.com"


def test_deal_rules_and_stage_machine(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        c, _ = crm.upsert_contact(s, email="b@c.com", first_name="B")
        with pytest.raises(crm.CRMValidationError):
            crm.create_deal(s, contact_id=c.id, title="x", value=-5)
        with pytest.raises(crm.CRMValidationError):
            crm.create_deal(s, contact_id=c.id, title="x", currency="usd")
        d = crm.create_deal(s, contact_id=c.id, title="Ops review", value="1500.50")
        with pytest.raises(crm.CRMValidationError):
            crm.move_deal(s, d.id, "won")  # cannot skip from lead to won
        for stage in ["qualified", "proposal", "negotiation", "won"]:
            crm.move_deal(s, d.id, stage)
        assert s.get(Contact, c.id).lifecycle == "customer"
        with pytest.raises(crm.CRMValidationError):
            crm.move_deal(s, d.id, "lost")  # won is terminal
        summary = crm.pipeline_summary(s)
        assert summary["won"] == {"count": 1, "value": "1500.50"}


SAMPLES = {
    "proposal": {"client_name": "Acme", "objective": "Grow", "scope_items": ["A", "B"], "fee": "12000",
                 "currency": "USD", "valid_days": 30},
    "sow": {"client_name": "Acme", "deliverables": ["Report"], "milestones": [{"name": "Kickoff", "date": "2026-12-01"}],
            "fee": "12000", "currency": "USD", "start_date": "2026-12-01"},
    "invoice": {"client_name": "Acme", "invoice_number": "INV-001", "currency": "USD", "due_days": 30,
                "tax_rate": "10", "line_items": [{"description": "Consulting", "quantity": 3, "unit_price": "1000"},
                                                 {"description": "Travel", "quantity": 1, "unit_price": "250.55"}]},
    "status_report": {"client_name": "Acme", "period": "Nov 2026", "progress": ["Done"], "risks": ["None"],
                      "next_steps": ["Next"]},
}


@pytest.mark.parametrize("kind", list(SAMPLES))
def test_every_document_kind_renders(tenant, kind):
    tid, _ = tenant
    with tenant_session(tid) as s:
        doc = documents.generate(s, kind=kind, title=kind, data=SAMPLES[kind])
        assert "Acme" in doc.html and "Acme" in doc.markdown
        assert doc.html.startswith("<!doctype html>")


def test_invoice_arithmetic():
    _, md, ctx = documents.render("invoice", SAMPLES["invoice"])
    assert ctx["subtotal"] == Decimal("3250.55")
    assert ctx["tax"] == Decimal("325.06")
    assert ctx["total"] == Decimal("3575.61")
    assert "3,575.61" in md


def test_missing_fields_rejected_and_html_escaped():
    with pytest.raises(documents.DocumentError, match="fee"):
        documents.render("proposal", {**SAMPLES["proposal"], "fee": ""})
    html, _, _ = documents.render("proposal", {**SAMPLES["proposal"], "client_name": "<script>alert(1)</script>"})
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


@pytest.mark.parametrize("kind", list(SAMPLES))
def test_every_html_document_escapes_client_input(kind):
    data = {**SAMPLES[kind], "client_name": '<img src=x onerror="alert(1)">'}
    html, md, _ = documents.render(kind, data)
    assert "<img" not in html
    assert "&lt;img" in html
