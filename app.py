import argparse
import signal
import sys
from pathlib import Path

from src.batch_service import BatchService
from src.field_store import FieldStore
from src.http_api import create_server
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def main(argv=None):
    parser = argparse.ArgumentParser(description="实验室仪器校准与方法验证")
    parser.add_argument("--db", default="./data.db", help="Central SQLite database path")
    parser.add_argument("--field-db", default="./field.db", help="Offline field SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8309)
    parser.add_argument(
        "--today",
        default=None,
        help="Override the current date (YYYY-MM-DD), mainly for demos/tests",
    )
    args = parser.parse_args(argv)

    clock = (lambda: args.today) if args.today else None
    repository = SQLiteRepository(args.db)
    rules = RuleEngine(clock=clock)
    service = DomainService(repository, rules)
    field_store = FieldStore(args.field_db)
    batch_service = BatchService(repository, rules, field_store)
    static_dir = Path(__file__).resolve().parent / "static"
    server = create_server(
        args.host, args.port, service, rules, str(static_dir), batch_service
    )

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        print("实验室仪器校准与方法验证 listening on http://%s:%s" % (args.host, args.port), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
