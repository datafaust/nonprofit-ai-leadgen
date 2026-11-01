"""Run the local lead viewer: uv run python leads_web.py"""
import argparse
import logging

from nonprofit_leadgen.web import create_app


def main():
    parser = argparse.ArgumentParser(description="Read-only local nonprofit lead viewer")
    parser.add_argument("--db", default="data/nonprofits.db", help="Path to the local SQLite database")
    parser.add_argument("--port", type=int, default=5055, help="Local port (default: 5055)")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    app = create_app(args.db)
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
