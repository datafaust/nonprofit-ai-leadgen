"""Command line entry point."""
import argparse
import logging
import re

from .export import export_csv
from .ingest import ingest_all
from .storage import connect, stats
from .website_check import run_all, run_batch

def main(argv=None):
    parser = argparse.ArgumentParser(prog="nonprofit-leadgen")
    parser.add_argument("--db", default="data/nonprofits.db", help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest")
    sub.add_parser("stats")
    websites = sub.add_parser("validate-websites", help="Manually check a small batch of listed websites")
    scope = websites.add_mutually_exclusive_group()
    scope.add_argument("--limit", type=int, default=None, help="Number of sites to check (1-25; default: 5)")
    scope.add_argument("--all", action="store_true", help="Check every eligible website once, saving progress after each site")
    history = sub.add_parser("website-checks", help="Inspect recent stored checks by organization ID prefix")
    history.add_argument("--id", required=True, help="Organization ID or its first eight characters")
    exp = sub.add_parser("export")
    exp.add_argument("--borough")
    exp.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    with connect(args.db) as db:
        if args.command == "ingest":
            results = ingest_all(db)
            for source, count, skipped, error in results:
                print(f"{source}: {count} source records imported, {skipped} skipped" + (f", error: {error}" if error else ""))
            return 1 if any(error for _, _, _, error in results) else 0
        if args.command == "stats":
            report = stats(db)
            print(f"Organizations: {report['organizations']}")
            print("Sources: " + ", ".join(f"{k}={n}" for k, n in report["sources"]))
            print("Boroughs: " + ", ".join(f"{k}={n}" for k, n in report["boroughs"]))
            print(f"Contact completeness: websites={report['websites']}, emails={report['emails']}, phones={report['phones']}")
            return 0
        if args.command == "validate-websites":
            if args.all:
                def progress(done, total, org_id, result):
                    detail = f"HTTP {result.http_status}" if result.http_status else result.error_type
                    print(f"{done}/{total} {org_id[:8]}: {result.status}" + (f" ({detail})" if detail else ""), flush=True)
                try:
                    done, total, summary = run_all(db, on_progress=progress)
                except KeyboardInterrupt:
                    print("Interrupted. Completed checks were saved; rerun --all to resume.")
                    return 130
                print(f"Checked {done}/{total} eligible websites. " +
                      (", ".join(f"{key}={value}" for key, value in sorted(summary.items())) if summary else "No sites pending."))
                return 0
            try:
                results = run_batch(db, limit=args.limit if args.limit is not None else 5)
            except ValueError as exc:
                parser.error(str(exc))
            if not results:
                print("No unchecked websites in this batch window.")
            for org_id, result in results:
                detail = f"HTTP {result.http_status}" if result.http_status else result.error_type
                print(f"{org_id[:8]}: {result.status}" + (f" ({detail})" if detail else ""))
            print(f"Checked {len(results)} websites; results describe reachability, not organization identity.")
            return 0
        if args.command == "website-checks":
            if not re.fullmatch(r"[0-9a-f-]{8,36}", args.id):
                parser.error("id must be an organization ID or an eight-character hex prefix")
            rows = db.execute("""SELECT substr(organization_id, 1, 8), checked_at, status, http_status, error_type
                                 FROM website_checks WHERE organization_id LIKE ?
                                 ORDER BY check_id DESC LIMIT 10""", (args.id + "%",)).fetchall()
            if not rows:
                print("No stored website checks match that ID.")
            for row in rows:
                detail = f"HTTP {row['http_status']}" if row["http_status"] else row["error_type"]
                print(f"{row[0]} {row['checked_at'][:19]}: {row['status']}" + (f" ({detail})" if detail else ""))
            return 0
        try:
            count = export_csv(db, args.output, args.borough)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"Exported {count} organizations to {args.output}")
        return 0

if __name__ == "__main__":
    raise SystemExit(main())
