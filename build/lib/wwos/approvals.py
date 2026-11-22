"""Owner approval queue for actions reserved to the owner."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ApprovalRequest, OutboundMessage, utcnow
from .policy import audit


class ApprovalError(ValueError):
    pass


def pending(s: Session) -> list[ApprovalRequest]:
    return list(s.scalars(select(ApprovalRequest).where(ApprovalRequest.status == "pending")
                          .order_by(ApprovalRequest.created_at)))


def decide(s: Session, approval_id: str, *, approve: bool, actor: str, note: str = "") -> ApprovalRequest:
    from .workflows import enqueue

    req = s.get(ApprovalRequest, approval_id)
    if req is None:
        raise ApprovalError("approval request not found")
    if req.status != "pending":
        raise ApprovalError(f"approval already {req.status}")
    req.status = "approved" if approve else "rejected"
    req.decided_by = actor
    req.decided_at = utcnow()
    audit(s, actor, f"approval.{req.status}", req.id, action=req.action, note=note)
    if approve:
        enqueue(s, kind="approval.execute", idempotency_key=f"approval:{req.id}",
                payload={"approval_id": req.id})
    elif req.action == "comms.send_email":
        msg = s.get(OutboundMessage, req.params.get("message_id"))
        if msg is not None and msg.status in ("pending_approval", "draft"):
            msg.status = "rejected"
    return req
