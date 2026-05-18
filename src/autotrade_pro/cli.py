"""Command-line entrypoint for AutoTrade Pro."""

from __future__ import annotations

import argparse
import os

from . import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the AutoTrade Pro Flask server.")
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "0.0.0.0"),
        help="Host interface to bind. Defaults to HOST or 0.0.0.0.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "5000")),
        help="Port to bind. Defaults to PORT or 5000.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=os.getenv("FLASK_DEBUG", "0") == "1",
        help="Enable Flask debug mode.",
    )
    args = parser.parse_args()

    app = create_app()
    app.run(host=args.host, port=args.port, debug=args.debug, use_reloader=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
