import pytest

from wwos.config import ConfigurationError, assert_safe_for_environment, production_problems

GOOD = {
    "WWOS_ENV": "production",
    "WWOS_DATABASE_URL": "postgresql+psycopg://u:p@db/wwos",
    "WWOS_LLM_PROVIDER": "anthropic",
    "WWOS_EMAIL_TRANSPORT": "smtp",
    "WWOS_SMTP_HOST": "smtp.example.com",
    "WWOS_SMTP_FROM": "ops@example.com",
    "WWOS_SMTP_SECURITY": "starttls",
    "WWOS_COOKIE_SECURE": "true",
    "WWOS_PUBLIC_ORIGIN": "https://ops.example.com",
    "WWOS_ADMIN_TOKEN": "x" * 40,
}


def test_good_production_config_passes(monkeypatch):
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    assert production_problems() == []
    assert_safe_for_environment()


@pytest.mark.parametrize("key,value,fragment", [
    ("WWOS_DATABASE_URL", "sqlite:///x.db", "PostgreSQL"),
    ("WWOS_SMTP_SECURITY", "none", "SMTP_SECURITY"),
    ("WWOS_COOKIE_SECURE", "false", "COOKIE_SECURE"),
    ("WWOS_PUBLIC_ORIGIN", "http://ops.example.com", "PUBLIC_ORIGIN"),
    ("WWOS_ADMIN_TOKEN", "short", "ADMIN_TOKEN"),
])
def test_unsafe_production_config_refused(monkeypatch, key, value, fragment):
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError, match=fragment):
        assert_safe_for_environment()


def test_development_is_permissive(monkeypatch):
    monkeypatch.setenv("WWOS_ENV", "development")
    monkeypatch.setenv("WWOS_DATABASE_URL", "sqlite:///x.db")
    assert_safe_for_environment()
