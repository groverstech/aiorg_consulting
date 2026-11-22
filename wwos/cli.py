"""Command-line entry point: ``wwos <command>``."""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wwos", description="White Waters AI Business Operating System")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="development: bring the database to the latest schema")
    dbp = sub.add_parser("db", help="schema migrations (production-safe)")
    dbsub = dbp.add_subparsers(dest="db_cmd", required=True)
    dbsub.add_parser("status", help="current/head revision, pending revisions and drift")
    up = dbsub.add_parser("upgrade", help="apply pending migrations")
    up.add_argument("--to", default="head")
    up.add_argument("--backup-confirmed", action="store_true",
                    help="PostgreSQL: confirm a backup (Cloud SQL on-demand backup or pg_dump) was taken")
    up.add_argument("--allow-destructive", action="store_true", help="permit revisions that can lose data")
    down = dbsub.add_parser("downgrade", help="roll back schema (destructive)")
    down.add_argument("to")
    down.add_argument("--backup-confirmed", action="store_true")
    down.add_argument("--allow-destructive", action="store_true")
    dbsub.add_parser("check", help="exit 1 unless the schema is at head with no drift")
    t = sub.add_parser("create-tenant", help="create a tenant with departments and founding agents")
    t.add_argument("name")
    t.add_argument("--budget", default=None, help="monthly AI budget in USD")
    k = sub.add_parser("create-key", help="issue an API key for a tenant")
    k.add_argument("tenant_id")
    k.add_argument("--name", default="key")
    k.add_argument("--role", choices=["owner", "operator", "viewer"], default="operator")
    w = sub.add_parser("worker", help="run the background worker")
    w.add_argument("--once", action="store_true", help="drain due tasks and exit")
    w.add_argument("--poll", type=float, default=2.0)
    w.add_argument("--no-schedule", action="store_true", help="do not run recurring jobs (daily reviews, QA scans)")
    w.add_argument("--health-port", type=int, default=0,
                   help="serve GET /healthz on this port (Cloud Run requires a listening port)")
    srv = sub.add_parser("serve", help="run the API and dashboard")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)
    sub.add_parser("qa", help="run independent QA invariant checks (exit 1 on failure)")
    b = sub.add_parser("backup", help="back up the database (SQLite)")
    b.add_argument("--dest", default="backups")
    r = sub.add_parser("restore", help="restore a backup over the database (irreversible)")
    r.add_argument("path")
    r.add_argument("--yes", action="store_true", help="confirm the overwrite")
    args = parser.parse_args(argv)

    from .logging_setup import configure as configure_logging

    configure_logging()
    from . import db

    if args.cmd == "init-db":
        db.init_db()
        print("database ready")
    elif args.cmd == "db":
        from . import migrate

        if args.db_cmd == "status":
            print(f"current={migrate.current()} head={migrate.head()} pending={migrate.pending()}")
            if migrate.current() is not None:
                d = migrate.drift()
                print("drift: none" if not d else f"drift: {d}")
        elif args.db_cmd == "upgrade":
            try:
                r = migrate.upgrade(args.to, backup_confirmed=args.backup_confirmed,
                                    allow_destructive=args.allow_destructive)
            except migrate.MigrationError as e:
                print(f"refused: {e}", file=sys.stderr)
                return 2
            print(f"{r.before} -> {r.after}; applied={r.applied}; backup={r.backup}")
        elif args.db_cmd == "downgrade":
            try:
                print(migrate.downgrade(args.to, allow_destructive=args.allow_destructive,
                                        backup_confirmed=args.backup_confirmed))
            except migrate.MigrationError as e:
                print(f"refused: {e}", file=sys.stderr)
                return 2
        elif args.db_cmd == "check":
            ok = migrate.current() == migrate.head() and not migrate.drift()
            print("schema OK" if ok else f"schema NOT OK: current={migrate.current()} head={migrate.head()}")
            return 0 if ok else 1
    elif args.cmd == "create-tenant":
        from .bootstrap import create_tenant

        db.init_db()
        tenant_id, key = create_tenant(args.name, args.budget)
        print(f"tenant_id={tenant_id}\nowner_api_key={key}\n(store this key now; it is not shown again)")
    elif args.cmd == "create-key":
        from .bootstrap import create_api_key

        print(create_api_key(args.tenant_id, args.name, args.role))
    elif args.cmd == "worker":
        from .config import assert_safe_for_environment
        from .workflows import drain, run_worker

        assert_safe_for_environment()
        db.init_db()
        if args.health_port:
            from .workflows import start_health_server

            start_health_server(args.health_port)
        n = drain() if args.once else run_worker(poll_seconds=args.poll, scheduled=not args.no_schedule)
        print(f"processed {n} task(s)")
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("wwos.api:app", host=args.host, port=args.port)
    elif args.cmd == "qa":
        from . import qa

        db.init_db()
        report = qa.run()
        print(report.render())
        return 0 if report.passed else 1
    elif args.cmd == "backup":
        from .backup import backup

        print(backup(args.dest))
    elif args.cmd == "restore":
        if not args.yes:
            print("restore overwrites the live database; re-run with --yes to confirm", file=sys.stderr)
            return 2
        from .backup import restore

        print(restore(args.path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
