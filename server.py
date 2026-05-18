"""Local entrypoint for AutoTrade Pro."""

from __future__ import annotations

import os
from pathlib import Path
import sys


def _load_dotenv() -> None:
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from autotrade_pro import create_app  # noqa: E402


app = create_app()


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "0") == "1"
    port = int(os.getenv("PORT", "5000"))
    app.logger.info("AutoTrade Pro starting on port %s (debug=%s)", port, debug_mode)
    app.run(host="0.0.0.0", port=port, debug=debug_mode, use_reloader=debug_mode)
