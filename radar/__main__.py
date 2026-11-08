"""Run with python -m radar; all commands keep secret values out of logs."""
import argparse
import sys
import threading

import psycopg

from radar.config import Settings, init_config


def main(argv=None):
    parser = argparse.ArgumentParser(description="Guitar Radar Phase 1 owner application")
    parser.add_argument("command", choices=("init-config", "migrate", "bootstrap", "serve", "worker"))
    parser.add_argument("--no-worker", action="store_true", help="Serve API only; use a separate worker process")
    args = parser.parse_args(argv)
    if args.command == "init-config":
        try:
            init_config()
            print("Created private .env.phase1. Open it locally for the owner login; never paste it into chat.")
        except FileExistsError:
            print("Using existing .env.phase1; it was not overwritten.")
        return 0
    try:
        settings = Settings.from_env()
        from radar.db import Database, Conflict
        from radar.worker import Worker
        db = Database(settings.database_url)
        db.migrate()
        if args.command == "bootstrap":
            if not db.list_groups():
                db.create_group("vegmpremium", "https://www.facebook.com/groups/vegmpremium")
            print("Database ready. Pilot group configured; no Facebook request was made.")
        elif args.command == "migrate":
            print("Database schema ready.")
        else:
            worker = Worker(db, interval_seconds=settings.interval_seconds, lookback_hours=settings.lookback_hours)
            if args.command == "worker":
                worker.run_forever()
            else:
                import uvicorn
                from radar.app import create_app
                stopped = threading.Event()
                thread = None
                if not args.no_worker:
                    def run_worker():
                        try:
                            worker.run_forever(stopped)
                        except Exception:
                            print("[radar] Worker stopped. Check database availability and restart the app.", file=sys.stderr)
                    thread = threading.Thread(target=run_worker, name="radar-worker", daemon=False)
                    thread.start()
                try:
                    uvicorn.run(create_app(settings, db), host=settings.host, port=settings.port,
                                access_log=False, log_level="warning", proxy_headers=False)
                finally:
                    stopped.set()
                    if thread:
                        thread.join()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except psycopg.Error:
        print("Database unavailable. Start PostgreSQL and check .env.phase1 locally.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Phase 1 could not start; check setup and local configuration. Secret values were not logged.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
