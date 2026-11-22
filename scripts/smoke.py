"""End-to-end smoke test against whatever WWOS_DATABASE_URL points at (offline LLM, no spend).

Creates a uniquely named tenant, runs the lead workflow, approves the outbound
email through the approval queue, and verifies the email was delivered.
"""
import sys
import uuid

from sqlalchemy import select

from wwos import approvals, comms, db, workflows
from wwos.bootstrap import create_tenant
from wwos.models import Deal, OutboundMessage


def main() -> int:
    db.init_db()
    tid, _ = create_tenant(f"smoke-{uuid.uuid4().hex[:8]}", "500")
    with db.tenant_session(tid) as s:
        workflows.start_lead_workflow(s, {"email": "smoke@example.com", "first_name": "Smoke", "company": "Smoke Co",
                                          "need": "We need a full review of our operating model and costs",
                                          "estimated_value": "10000"})
    workflows.drain()
    with db.tenant_session(tid) as s:
        for req in approvals.pending(s):
            approvals.decide(s, req.id, approve=True, actor="owner:smoke")
    workflows.drain()
    with db.tenant_session(tid) as s:
        stage = s.scalars(select(Deal)).one().stage
        status = s.scalars(select(OutboundMessage)).one().status
    ok = stage == "proposal" and status == "sent" and len(comms.OutboxTransport.delivered) == 1
    print(f"deal stage={stage} email={status} delivered={len(comms.OutboxTransport.delivered)} -> {'OK' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
