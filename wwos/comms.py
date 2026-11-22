"""Outbound communications via a transactional outbox.

Messages are persisted (``OutboundMessage``) before any delivery attempt and
keyed by an idempotency key, so a retried workflow can never send the same
email twice. Delivery goes through a pluggable transport:

* ``outbox`` (default) - records the message as delivered locally; no network.
  Used in tests and until SMTP credentials are configured.
* ``smtp`` - real delivery via SMTP (STARTTLS), configured by WWOS_SMTP_*.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .crm import _norm_email, log_activity
from .models import OutboundMessage, utcnow


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
    msg = OutboundMessage(idempotency_key=idempotency_key, to_address=_norm_email(to), subject=subject.strip(),
                          body=body, contact_id=contact_id, status="draft", created_by=actor)
    s.add(msg)
    s.flush()
    return msg, True


def deliver(s: Session, message_id: str) -> OutboundMessage:
    """Send a message exactly once. Safe to call repeatedly."""
    msg = s.get(OutboundMessage, message_id)
    if msg is None:
        raise ValueError("message not found")
    if msg.status == "sent":
        return msg
    msg.attempts += 1
    try:
        transport().send(to=msg.to_address, subject=msg.subject, body=msg.body, message_id=msg.id)
    except Exception as e:  # delivery failures are recorded and surfaced for retry
        msg.status = "failed"
        msg.last_error = str(e)[:2000]
        raise
    msg.status = "sent"
    msg.sent_at = utcnow()
    msg.last_error = ""
    log_activity(s, kind="email", summary=f"Email sent: {msg.subject}", contact_id=msg.contact_id,
                 actor=msg.created_by)
    return msg
