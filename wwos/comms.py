"""Outbound communications via a transactional outbox.

Messages are persisted (``OutboundMessage``) and keyed by an idempotency key.
Business code only *queues* a message (``request_send``); the separate
``dispatch`` loop sends it after the queuing transaction has committed, with its
own committed state transitions. A retried or rolled-back workflow step can
therefore never send the same email twice. Delivery uses a pluggable transport:

* ``outbox`` (default) - records the message as delivered locally; no network.
  Used in tests and until SMTP credentials are configured.
* ``smtp`` - real delivery via SMTP (STARTTLS), configured by WWOS_SMTP_*.
"""
from __future__ import annotations

import smtplib
from datetime import timedelta
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .crm import _norm_email, log_activity
from .db import system_session, tenant_session
from .models import Contact, OutboundMessage, utcnow


class Transport(Protocol):
    def send(self, *, to: str, subject: str, body: str, message_id: str) -> None: ...


class OutboxTransport:
    """No-network transport. Keeps a process-local record for inspection/tests."""

    delivered: list[dict] = []

    def send(self, *, to: str, subject: str, body: str, message_id: str) -> None:
        OutboxTransport.delivered.append({"to": to, "subject": subject, "body": body, "id": message_id})


class SMTPTransport:
    def __init__(self) -> None:
        cfg = get_settings()
        if not (cfg.smtp_host and cfg.smtp_from):
            raise RuntimeError("SMTP transport selected but WWOS_SMTP_HOST / WWOS_SMTP_FROM are not set")
        self.cfg = cfg

    def send(self, *, to: str, subject: str, body: str, message_id: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.cfg.smtp_from
        msg["To"] = to
        msg["Subject"] = subject
        msg["X-WWOS-Message-Id"] = message_id
        msg.set_content(body)
        with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if self.cfg.smtp_user:
                smtp.login(self.cfg.smtp_user, self.cfg.smtp_password)
            smtp.send_message(msg)


_transport_override: Transport | None = None


def set_transport(t: Transport | None) -> None:
    global _transport_override
    _transport_override = t


def transport() -> Transport:
    if _transport_override is not None:
        return _transport_override
    return SMTPTransport() if get_settings().email_transport == "smtp" else OutboxTransport()


def draft_email(s: Session, *, idempotency_key: str, to: str, subject: str, body: str,
                contact_id: str | None = None, actor: str = "system") -> tuple[OutboundMessage, bool]:
    """Persist a draft. Returns (message, created); an existing key returns the original."""
    existing = s.scalars(select(OutboundMessage).where(OutboundMessage.idempotency_key == idempotency_key)).first()
    if existing:
        return existing, False
    if not subject.strip() or not body.strip():
        raise ValueError("subject and body are required")
    if contact_id is not None and s.get(Contact, contact_id) is None:  # tenant-scoped lookup
        raise ValueError("contact not found")
    msg = OutboundMessage(idempotency_key=idempotency_key, to_address=_norm_email(to), subject=subject.strip(),
                          body=body, contact_id=contact_id, status="draft", created_by=actor)
    s.add(msg)
    s.flush()
    return msg, True


SENDABLE = ("draft", "pending_approval")


def request_send(s: Session, message_id: str) -> OutboundMessage:
    """Mark a message for delivery. Nothing leaves the system until this transaction commits and
    the dispatcher picks it up, so a rolled-back workflow step can never send mail."""
    msg = s.get(OutboundMessage, message_id)
    if msg is None:
        raise ValueError("message not found")
    if msg.status in ("queued", "sending", "sent"):
        return msg  # idempotent
    if msg.status not in SENDABLE:
        raise ValueError(f"message is {msg.status} and cannot be sent")
    msg.status = "queued"
    return msg


MAX_SEND_ATTEMPTS = 3
SENDING_TIMEOUT = timedelta(minutes=10)


def dispatch(limit: int = 50, tenant_id: str | None = None) -> int:
    """Deliver queued messages. Each message moves queued -> sending (committed) -> sent/failed
    (committed). A crash mid-send leaves it in 'sending'; after a timeout it becomes 'unknown'
    for a human to check rather than being re-sent: at-most-once delivery."""
    sent = 0
    now = utcnow()
    with system_session() as s:
        s.execute(update(OutboundMessage).where(OutboundMessage.status == "sending",
                                                OutboundMessage.updated_at < now - SENDING_TIMEOUT)
                  .values(status="unknown", last_error="delivery outcome unknown (worker stopped mid-send)"))
        q = select(OutboundMessage.id).where(OutboundMessage.status == "queued").limit(limit)
        if tenant_id:
            q = q.where(OutboundMessage.tenant_id == tenant_id)
        ids = [i for (i,) in s.execute(q).all()]
    for mid in ids:
        with system_session() as s:
            claimed = s.execute(update(OutboundMessage).where(OutboundMessage.id == mid,
                                                              OutboundMessage.status == "queued")
                                .values(status="sending", attempts=OutboundMessage.attempts + 1,
                                        updated_at=utcnow()))
            if claimed.rowcount != 1:
                continue
        with system_session() as s:
            msg = s.get(OutboundMessage, mid)
            to, subject, body, attempts, owner = msg.to_address, msg.subject, msg.body, msg.attempts, msg.tenant_id
        try:
            transport().send(to=to, subject=subject, body=body, message_id=mid)
        except Exception as e:  # noqa: BLE001 - the transport refused; nothing was sent
            with system_session() as s:
                msg = s.get(OutboundMessage, mid)
                msg.last_error = f"{type(e).__name__}: {e}"[:2000]
                msg.status = "queued" if attempts < MAX_SEND_ATTEMPTS else "failed"
            continue
        with tenant_session(owner) as s:
            msg = s.get(OutboundMessage, mid)
            msg.status = "sent"
            msg.sent_at = utcnow()
            msg.last_error = ""
            log_activity(s, kind="email", summary=f"Email sent: {msg.subject}", contact_id=msg.contact_id,
                         actor=msg.created_by)
        sent += 1
    return sent
