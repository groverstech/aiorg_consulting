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

import logging
import re
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .crm import _norm_email, log_activity
from .db import system_session, tenant_session
from .models import Contact, OutboundMessage, utcnow


class DeliveryRejected(Exception):
    """The mail server definitely did NOT accept the message. Safe to retry if ``retryable``."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class DeliveryAmbiguous(Exception):
    """The message may or may not have been accepted (e.g. connection lost after the body was
    sent). Never retried automatically: a human reconciles it (see ``resolve``)."""


class Transport(Protocol):
    def send(self, *, to: str, subject: str, body: str, message_id: str) -> None:
        """Return normally only when the server accepted the message. Raise DeliveryRejected
        when it definitely did not; anything else is treated as ambiguous."""


class OutboxTransport:
    """No-network transport. Keeps a process-local record for inspection/tests."""

    delivered: list[dict] = []

    def send(self, *, to: str, subject: str, body: str, message_id: str) -> None:
        OutboxTransport.delivered.append({"to": to, "subject": subject, "body": body, "id": message_id})


def rfc_message_id(message_id: str, sender: str) -> str:
    """Stable RFC 5322 Message-ID per outbox row, so any manual resend is recognisable as the same
    message by recipients' mail systems (which commonly de-duplicate on Message-ID)."""
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else "wwos.local"
    return f"<wwos.{message_id}@{domain}>"


class SMTPTransport:
    """SMTP delivery that classifies every failure by the protocol phase it happened in.

    Before the message body is transmitted (connect, TLS, auth, MAIL, RCPT, the DATA command)
    nothing can have been delivered, so failures are DeliveryRejected (4xx/connection = retryable,
    5xx = permanent). After the body is transmitted, only an explicit reply code is definitive;
    a timeout or dropped connection there is DeliveryAmbiguous.
    """

    def __init__(self) -> None:
        cfg = get_settings()
        if not (cfg.smtp_host and cfg.smtp_from):
            raise RuntimeError("SMTP transport selected but WWOS_SMTP_HOST / WWOS_SMTP_FROM are not set")
        if cfg.smtp_security not in ("starttls", "ssl", "none"):
            raise RuntimeError("WWOS_SMTP_SECURITY must be starttls, ssl or none")
        self.cfg = cfg

    def _message(self, to: str, subject: str, body: str, message_id: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.cfg.smtp_from
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = rfc_message_id(message_id, self.cfg.smtp_from)
        msg["X-WWOS-Message-Id"] = message_id
        msg.set_content(body)
        return msg

    def send(self, *, to: str, subject: str, body: str, message_id: str) -> None:
        cfg = self.cfg
        msg = self._message(to, subject, body, message_id)
        smtp = None
        try:
            try:  # ---- phase 1: nothing transmitted yet -> any failure is a definite non-delivery
                cls = smtplib.SMTP_SSL if cfg.smtp_security == "ssl" else smtplib.SMTP
                smtp = cls(cfg.smtp_host, cfg.smtp_port, timeout=cfg.smtp_timeout_seconds)
                smtp.ehlo()
                if cfg.smtp_security == "starttls":
                    smtp.starttls()
                    smtp.ehlo()
                if cfg.smtp_user:
                    smtp.login(cfg.smtp_user, cfg.smtp_password)
                code, resp = smtp.mail(cfg.smtp_from)
                if code != 250:
                    raise DeliveryRejected(f"MAIL FROM refused: {code} {resp!r}", retryable=400 <= code < 500)
                code, resp = smtp.rcpt(to)
                if code not in (250, 251):
                    raise DeliveryRejected(f"RCPT TO refused: {code} {resp!r}", retryable=400 <= code < 500)
                code, resp = smtp.docmd("DATA")
                if code != 354:
                    raise DeliveryRejected(f"DATA refused: {code} {resp!r}", retryable=400 <= code < 500)
            except DeliveryRejected:
                raise
            except smtplib.SMTPAuthenticationError as e:
                raise DeliveryRejected(f"authentication failed: {e}", retryable=False) from e
            except smtplib.SMTPResponseException as e:
                raise DeliveryRejected(f"refused: {e.smtp_code} {e.smtp_error!r}",
                                       retryable=400 <= e.smtp_code < 500) from e
            except (OSError, smtplib.SMTPException) as e:
                raise DeliveryRejected(f"could not reach mail server: {e}", retryable=True) from e
            # ---- phase 2: the body goes on the wire; from here only an explicit reply is definitive
            try:
                payload = re.sub(rb"(?:\r\n|\n|\r(?!\n))", b"\r\n", msg.as_bytes())  # CRLF line ends
                payload = re.sub(rb"(?m)^\.", b"..", payload)  # RFC 5321 dot-stuffing
                if not payload.endswith(b"\r\n"):
                    payload += b"\r\n"
                smtp.send(payload + b".\r\n")
                code, resp = smtp.getreply()
            except (OSError, smtplib.SMTPException) as e:
                raise DeliveryAmbiguous(f"connection lost after the message was transmitted: {e}") from e
            if code == 250:
                return
            raise DeliveryRejected(f"message refused after DATA: {code} {resp!r}", retryable=400 <= code < 500)
        finally:
            if smtp is not None:
                try:
                    smtp.quit()
                except Exception:  # noqa: BLE001 - the outcome is already decided
                    try:
                        smtp.close()
                    except Exception:  # noqa: BLE001
                        pass


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
    _check_partner_message(s, to, subject + "\n" + body)
    msg = OutboundMessage(idempotency_key=idempotency_key, to_address=_norm_email(to), subject=subject.strip(),
                          body=body, contact_id=contact_id, status="draft", created_by=actor)
    s.add(msg)
    s.flush()
    return msg, True


SENDABLE = ("draft", "pending_approval")


def _check_partner_message(s: Session, to: str, text: str) -> None:
    """Messages to partner domains: never to an inactive partner, never containing a client's identity."""
    from .partners import client_leaks_in, partners_for_address

    matches = partners_for_address(s, to)
    if not matches:
        return
    inactive = [p.name for p in matches if p.status == "inactive"]
    if inactive:
        raise ValueError(f"partner {inactive[0]} is inactive; nothing may be sent to them")
    if client_leaks_in(s, text):
        raise ValueError("message to a partner identifies a client; use an anonymised partner brief")


def resolve(s: Session, message_id: str, *, delivered: bool, actor: str) -> OutboundMessage:
    """Owner/operator reconciliation of a message whose outcome is unknown (or that failed).

    delivered=True  -> recorded as sent (e.g. confirmed in the mail server log or by the recipient).
    delivered=False -> re-queued for one more delivery attempt with the same Message-ID.
    """
    from .policy import audit

    msg = s.get(OutboundMessage, message_id)
    if msg is None:
        raise ValueError("message not found")
    if msg.status not in ("unknown", "failed", "manual"):
        raise ValueError(f"message is {msg.status}; only unknown, failed or manual messages can be reconciled")
    if delivered:
        msg.status, msg.sent_at = "sent", utcnow()
        log_activity(s, kind="email", summary=f"Email confirmed delivered: {msg.subject}",
                     contact_id=msg.contact_id, actor=actor)
    elif msg.status == "manual":
        raise ValueError("this message is waiting for you to send it by hand; mark it sent once you have")
    else:
        msg.status, msg.attempts = "queued", 0
    msg.last_error = f"reconciled by {actor}: {'delivered' if delivered else 're-queued'}"
    audit(s, actor, "email.reconciled", msg.id, delivered=delivered)
    return msg


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
    _check_partner_message(s, msg.to_address, msg.subject + "\n" + msg.body)
    msg.status = "queued"
    return msg


MAX_SEND_ATTEMPTS = 3
SENDING_TIMEOUT = timedelta(minutes=10)
RETRY_BACKOFF = timedelta(seconds=60)


def dispatch(limit: int = 50, tenant_id: str | None = None) -> int:
    """Deliver queued messages. Each message moves queued -> sending (committed) -> sent/failed
    (committed). A crash mid-send leaves it in 'sending'; after a timeout it becomes 'unknown'
    for a human to check rather than being re-sent: at-most-once delivery."""
    sent = 0
    now = utcnow()
    with system_session(immediate=True) as s:
        s.execute(update(OutboundMessage).where(OutboundMessage.status == "sending",
                                                OutboundMessage.updated_at < now - SENDING_TIMEOUT)
                  .values(status="unknown", last_error="delivery outcome unknown (worker stopped mid-send)"))
        q = select(OutboundMessage.id, OutboundMessage.attempts, OutboundMessage.updated_at).where(
            OutboundMessage.status == "queued").limit(limit)
        if tenant_id:
            q = q.where(OutboundMessage.tenant_id == tenant_id)
        ids = []
        for mid, attempts, updated in s.execute(q).all():
            if attempts and updated is not None:
                updated = updated if updated.tzinfo else updated.replace(tzinfo=now.tzinfo)
                if now - updated < RETRY_BACKOFF * (2 ** (attempts - 1)):
                    continue  # exponential backoff between delivery attempts
            ids.append(mid)
    settings = get_settings()
    if settings.email_transport == "manual" and _transport_override is None:
        # No mail account configured (zero-spend): approved messages wait for the owner to send them by hand.
        with system_session(immediate=True) as s:
            for mid in ids:
                s.execute(update(OutboundMessage).where(OutboundMessage.id == mid, OutboundMessage.status == "queued")
                          .values(status="manual", last_error="no mail account configured: send this e-mail "
                                  "yourself, then mark it sent"))
        return 0
    with system_session() as s:
        sent_24h = s.execute(select(func.count(OutboundMessage.id)).where(
            OutboundMessage.status == "sent", OutboundMessage.sent_at >= now - timedelta(days=1))).scalar_one()
    budget_left = max(0, settings.email_daily_limit - sent_24h)
    if len(ids) > budget_left:
        logging.getLogger("wwos.comms").warning("daily e-mail limit %s reached; %s message(s) wait",
                                                settings.email_daily_limit, len(ids) - budget_left)
        ids = ids[:budget_left]
    for mid in ids:
        with system_session(immediate=True) as s:
            claimed = s.execute(update(OutboundMessage).where(OutboundMessage.id == mid,
                                                              OutboundMessage.status == "queued")
                                .values(status="sending", attempts=OutboundMessage.attempts + 1,
                                        updated_at=utcnow()))
            if claimed.rowcount != 1:
                continue
        with system_session(immediate=True) as s:
            msg = s.get(OutboundMessage, mid)
            to, subject, body, attempts, owner = msg.to_address, msg.subject, msg.body, msg.attempts, msg.tenant_id
        try:  # re-check partner rules at delivery time (a partner may have been deactivated meanwhile)
            with tenant_session(owner) as ts:
                _check_partner_message(ts, to, subject + "\n" + body)
        except ValueError as e:
            with system_session(immediate=True) as s:
                msg = s.get(OutboundMessage, mid)
                msg.status, msg.last_error = "failed", f"blocked at delivery: {e}"[:2000]
            continue
        try:
            transport().send(to=to, subject=subject, body=body, message_id=mid)
        except DeliveryRejected as e:  # definitely not delivered
            with system_session(immediate=True) as s:
                msg = s.get(OutboundMessage, mid)
                msg.last_error = f"rejected: {e}"[:2000]
                msg.status = "queued" if e.retryable and attempts < MAX_SEND_ATTEMPTS else "failed"
            continue
        except Exception as e:  # noqa: BLE001 - DeliveryAmbiguous or anything unclassified
            with system_session(immediate=True) as s:
                msg = s.get(OutboundMessage, mid)
                msg.last_error = f"delivery outcome unknown: {type(e).__name__}: {e}"[:2000]
                msg.status = "unknown"  # never auto-retried (ADR-011); see resolve()
            logging.getLogger("wwos.comms").error("message %s: delivery outcome unknown: %s", mid, e)
            continue
        with tenant_session(owner, immediate=True) as s:
            msg = s.get(OutboundMessage, mid)
            msg.status = "sent"
            msg.sent_at = utcnow()
            msg.last_error = ""
            log_activity(s, kind="email", summary=f"Email sent: {msg.subject}", contact_id=msg.contact_id,
                         actor=msg.created_by)
        sent += 1
    return sent
