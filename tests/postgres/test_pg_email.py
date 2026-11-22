import pytest

from tests.unit import test_email_reliability as e

pytestmark = pytest.mark.postgres


def test_pg_concurrent_dispatchers_deliver_each_message_once(pg, tenant, smtp_env):
    e.test_concurrent_dispatchers_deliver_each_message_once(tenant, smtp_env)


def test_pg_ambiguous_outcome_not_resent(pg, tenant, smtp_env):
    e.test_ambiguous_outcome_is_never_resent_automatically(tenant, smtp_env, "drop_after_data")


@pytest.fixture
def smtp_env(monkeypatch):
    return e.smtp_env.__wrapped__(monkeypatch)
