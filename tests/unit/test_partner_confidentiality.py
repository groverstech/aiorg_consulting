"""Adversarial anonymisation tests (security review round 3, H1/H2/M2)."""
import pytest
from sqlalchemy import select

from wwos import comms, crm, documents, partners
from wwos.agents.runtime import execute_tool, find_agent
from wwos.db import tenant_session
from wwos.models import ApprovalRequest, Tenant
from wwos.tools import ToolContext


@pytest.fixture
def setup(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        c, _ = crm.upsert_contact(s, email="a.smithjones@acmeholdings.com", first_name="Alice",
                                  last_name="Smith Jones", phone="+1 555 0100", company="Acme Holdings Ltd")
        crm.upsert_contact(s, email="bob@acmeholdings.com", first_name="Robert", last_name="Quinlan",
                           company="Acme Holdings Ltd")
        deal = crm.create_deal(s, contact_id=c.id, title="Logistics cost engagement", value="1000")
        p = partners.register(s, name="Mumbai Partners", region="india",
                              contact_email="desk@mumbaipartners.in", actor="owner")
        partners.attest_nda(s, p.id, on_file=True, actor="owner")
        return tid, deal.id, p.id


SCOPES = [
    "Work for Acme Holdings (the Ltd).",
    "Call +15550100 for access.",
    "Report to A. Smith Jones.",
    "Data at acmeholdings dot com.",
    "Client is A c m e H o l d i n g s.",
    "Coordinate with Robert Quinlan.",  # another contact at the same company
    "Email ACMEHOLDINGS.COM staff.",
    "Contact a.smithjones at the client.",
    "Ask Alice (alice smith-jones) for data.",
]
FORBIDDEN = ["acmeholdings", "smithjones", "5550100", "robertquinlan", "quinlan", "alicesmith"]


@pytest.mark.parametrize("scope", SCOPES)
def test_obfuscated_identifiers_never_reach_a_brief(setup, scope):
    """Either the brief is refused, or nothing in it identifies the client (checked independently
    of the anonymiser's own fingerprint list, after removing spacing/punctuation)."""
    import re

    tid, deal_id, pid = setup
    with tenant_session(tid) as s:
        try:
            doc = partners.prepare_brief(s, partner_id=pid, deal_id=deal_id, scope=scope, actor="CEO")
        except partners.PartnerError:
            return
        for text in (doc.markdown, doc.html):
            flat = re.sub(r"[^a-z0-9]", "", text.lower())
            assert not [f for f in FORBIDDEN if f in flat], text


def test_generic_business_words_are_allowed(setup):
    tid, deal_id, pid = setup
    with tenant_session(tid) as s:
        doc = partners.prepare_brief(s, partner_id=pid, deal_id=deal_id, actor="CEO",
                                     scope="Analyse logistics costs and holdings of warehouse stock across three sites.")
        assert "Client-" in doc.markdown


def test_forged_partner_brief_is_impossible(setup):
    tid, deal_id, pid = setup
    with tenant_session(tid) as s:
        sales = ToolContext(s, s.get(Tenant, tid), find_agent(s, role="sales"))
        out, err = execute_tool(sales, "docs.generate", {"kind": "partner_brief", "title": "x", "deal_id": deal_id,
                                                         "data": {"partner_name": "Mumbai Partners", "client_code": "C",
                                                                  "scope": "Acme Holdings secrets", "region": "india"}})
        assert err and "only be produced" in out["error"]
        with pytest.raises(documents.DocumentError):
            documents.generate(s, kind="executive_report", title="x", data={})


def test_send_brief_rechecks_and_owner_sees_the_content(setup, tenant):
    from fastapi.testclient import TestClient

    from wwos.api import app

    tid, deal_id, pid = setup
    _, key = tenant
    with tenant_session(tid) as s:
        doc = partners.prepare_brief(s, partner_id=pid, deal_id=deal_id, scope="Review depot costs.", actor="CEO")
        ceo = ToolContext(s, s.get(Tenant, tid), find_agent(s, role="ceo"))
        out, err = execute_tool(ceo, "partners.send_brief", {"partner_id": pid, "document_id": doc.id})
        assert out["category"] == "confidential_disclosure"
    with TestClient(app) as c:
        c.post("/login", data={"api_key": key})
        page = c.get("/dashboard").text
        assert "desk@mumbaipartners.in" in page and "Review depot costs." in page
    # NDA withdrawn after the request: the approved send is still refused at execution.
    with tenant_session(tid) as s:
        partners.attest_nda(s, pid, on_file=False, actor="owner")
        req = s.scalars(select(ApprovalRequest)).one()
        from wwos import approvals

        approvals.decide(s, req.id, approve=True, actor="owner")
    from wwos import workflows

    workflows.drain()
    assert comms.OutboxTransport.delivered == []


def test_partner_subdomains_are_recognised(setup):
    tid, _, pid = setup
    with tenant_session(tid) as s:
        assert partners.partner_for_address(s, "ops@mail.mumbaipartners.in").id == pid
        assert partners.partner_for_address(s, "x@notmumbaipartners.in") is None


# ---- Re-verification round (N1-N4)
@pytest.mark.parametrize("scope", [
    "Client is Acme.",                      # short distinctive name
    "Асmе Holdings needs help.",            # Cyrillic homoglyphs
    "Acm3 H0ldings data.",                  # digit swaps
    "N0rthwind style rename of Acme.",
    "Ms A. Smith will brief you.",          # partial surname
    "alice dot s at acme",                  # partial e-mail
])
def test_round4_obfuscations_never_reach_a_brief(setup, scope):
    test_obfuscated_identifiers_never_reach_a_brief(setup, scope)
    tid, deal_id, pid = setup
    with tenant_session(tid) as s:
        with pytest.raises(partners.PartnerError):
            partners.prepare_brief(s, partner_id=pid, deal_id=deal_id, scope=scope, actor="CEO")


def test_generic_mailboxes_and_short_names_do_not_break_ordinary_text(tenant):
    tid, _ = tenant
    with tenant_session(tid) as s:
        c, _ = crm.upsert_contact(s, email="info@zenfleet.example", first_name="Wei", last_name="Li",
                                  company="Zenfleet Ltd")
        deal = crm.create_deal(s, contact_id=c.id, title="Fleet review", value="100")
        p = partners.register(s, name="P1", region="india", contact_email="a@p1.example", actor="owner")
        partners.attest_nda(s, p.id, on_file=True, actor="owner")
        doc = partners.prepare_brief(s, partner_id=p.id, deal_id=deal.id, actor="CEO",
                                     scope="We need information about the fleet; smithing and client lists.")
        assert "information" in doc.markdown and "client lists" in doc.markdown and "CClient" not in doc.markdown


def test_api_approvals_include_preview(setup, tenant):
    from fastapi.testclient import TestClient

    from wwos.api import app

    tid, deal_id, pid = setup
    _, key = tenant
    with tenant_session(tid) as s:
        doc = partners.prepare_brief(s, partner_id=pid, deal_id=deal_id, scope="Review depot costs.", actor="CEO")
        ceo = ToolContext(s, s.get(Tenant, tid), find_agent(s, role="ceo"))
        execute_tool(ceo, "partners.send_brief", {"partner_id": pid, "document_id": doc.id})
    with TestClient(app) as c:
        [row] = c.get("/api/approvals", headers={"Authorization": f"Bearer {key}"}).json()
        assert "Review depot costs." in row["preview"]["body"] and "desk@mumbaipartners.in" in row["preview"]["to"]
