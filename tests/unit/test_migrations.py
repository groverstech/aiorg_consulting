import sqlite3

import pytest
from sqlalchemy import select

from wwos import backup, db, migrate
from wwos.cli import main
from wwos.models import Contact, Deal

FIXTURE = "tests/fixtures/release_2026_11_22.sqlite"


def _copy_fixture(tmp_path):
    dest = tmp_path / "legacy.db"
    src, dst = sqlite3.connect(FIXTURE), sqlite3.connect(dest)
    src.backup(dst)
    dst.close()
    src.close()
    return f"sqlite:///{dest}"


def test_fresh_database_reaches_head_without_drift():
    assert migrate.current() == migrate.head()
    assert migrate.drift() == []


def test_previous_release_database_upgrades_with_data_and_backup(tmp_path, monkeypatch):
    url = _copy_fixture(tmp_path)
    before = backup.table_counts(url)
    db.configure(url)
    monkeypatch.chdir(tmp_path)
    r = migrate.upgrade(backup_dir=str(tmp_path / "bk"))
    assert r.before == "0001" and r.after == migrate.head() and r.backup
    assert migrate.drift() == []
    after = backup.table_counts(url)
    for table, n in before.items():
        assert after[table] == n, table
    assert backup.verify(r.backup)  # pre-migration backup is valid
    with db.system_session() as s:
        assert s.scalars(select(Deal)).one().stage == "proposal"
        assert s.scalars(select(Contact)).one().email == "old@x.com"


def test_adoption_refuses_unknown_schema(tmp_path):
    url = f"sqlite:///{tmp_path / 'odd.db'}"
    conn = sqlite3.connect(tmp_path / "odd.db")
    conn.execute("CREATE TABLE tenants (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    db.configure(url)
    with pytest.raises(migrate.MigrationError, match="does not match the baseline"):
        migrate.upgrade()


def test_destructive_revisions_and_downgrades_are_gated(monkeypatch):
    with pytest.raises(migrate.MigrationError):
        migrate.downgrade("0001", allow_destructive=True, backup_confirmed=False)
    monkeypatch.setattr(migrate, "_is_destructive", lambda rev: True)
    migrate.downgrade("0001", allow_destructive=True, backup_confirmed=True)
    with pytest.raises(migrate.MigrationError, match="allow-destructive"):
        migrate.upgrade()
    migrate.upgrade(allow_destructive=True)
    assert migrate.current() == migrate.head()


def test_cli_status_and_check(capsys):
    assert main(["db", "check"]) == 0
    main(["db", "status"])
    out = capsys.readouterr().out
    assert "drift: none" in out and f"head={migrate.head()}" in out


def test_production_refuses_unmigrated_schema(tmp_path, monkeypatch):
    db.configure(f"sqlite:///{tmp_path / 'empty.db'}")
    monkeypatch.setenv("WWOS_ENV", "production")
    with pytest.raises(migrate.MigrationError, match="expected"):
        db.init_db()
