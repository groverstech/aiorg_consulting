"""Authority and permission policy.

Two independent gates protect every action an agent takes:

1. **Tool permission** - an agent may only invoke tools listed on its record.
   Provisioning tools additionally require ``can_provision_agents``.
2. **Owner authority** - actions in the categories reserved for the owner by the
   mandate (section 9) never execute directly. They become an
   ``ApprovalRequest`` and run only after an owner approves them.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from .config import get_settings
from .models import Agent, AuditLog

# Categories the owner reserved for themselves (mandate amendment, section 9).
OWNER_AUTHORITY_CATEGORIES = {
    "budget_increase": "New cloud expenditure or budget increases beyond authorised limits",
    "credentials": "Missing credentials or external account permissions",
    "legal_commitment": "Binding legal commitments / signing agreements",
    "financial_transfer": "Financial transfers",
    "regulatory_filing": "Regulatory filings requiring authorised submission",
    "business_model_change": "Material changes to the business model",
    "irreversible_production": "Irreversible actions affecting production data or infrastructure",
    "business_policy": "Fundamental business policy decisions",
    "external_communication": "Outbound communication to external parties (owner policy, see ADR-006)",
    "confidential_disclosure": "Any disclosure to a partner firm (Indian or international); always owner-approved",
}

# Tools whose execution always requires owner approval, mapped to category.
APPROVAL_REQUIRED_TOOLS = {
    "finance.transfer_funds": "financial_transfer",
    "legal.sign_agreement": "legal_commitment",
    "compliance.submit_filing": "regulatory_filing",
    "budget.request_increase": "budget_increase",
    "data.purge_tenant_records": "irreversible_production",
    "partners.send_brief": "confidential_disclosure",
}

PROVISIONING_TOOLS = {"agents.provision", "agents.suspend", "agents.revise", "agents.replace"}


class PermissionDenied(PermissionError):
    pass


def audit(session: Session, actor: str, event: str, target: str = "", /, **detail) -> None:
    """Append an audit record. Positional-only params so detail may contain any key (e.g. ``action``)."""
    session.add(AuditLog(actor=actor, action=event, target=target, detail=detail))


def check_tool_permission(agent: Agent, tool: str) -> None:
    if agent.status != "active" and agent.status != "testing":
        raise PermissionDenied(f"agent {agent.name} is {agent.status}; it may not act")
    if tool not in (agent.tools or []):
        raise PermissionDenied(f"agent {agent.name} is not permitted to use {tool}")
    if tool in PROVISIONING_TOOLS and not agent.can_provision_agents:
        raise PermissionDenied(f"agent {agent.name} may not manage other agents")


def approval_category(tool: str, args: dict, session: Session | None = None) -> str | None:
    """Return the owner-authority category for this call, or None if it may run directly."""
    if tool in APPROVAL_REQUIRED_TOOLS:
        return APPROVAL_REQUIRED_TOOLS[tool]
    if tool == "comms.send_email":
        if session is not None:
            from .models import OutboundMessage
            from .partners import partner_for_address

            msg = session.get(OutboundMessage, args.get("message_id"))
            if msg is not None and partner_for_address(session, msg.to_address) is not None:
                return "confidential_disclosure"  # partners: always owner-approved, whatever the policy
        if get_settings().external_email_requires_approval:
            return "external_communication"
    return None
