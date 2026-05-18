from __future__ import annotations

import json
import os
import subprocess
import sys


def test_init_db_script_creates_and_seeds_database(tmp_path):
    env = {
        **os.environ,
        "AUTOTRADE_DATA_DIR": str(tmp_path / "data"),
        "AUTOTRADE_DB_FILE": "autotrade-test.db",
    }

    result = subprocess.run(
        [sys.executable, "scripts/init_db.py", "--json"],
        check=True,
        cwd=os.getcwd(),
        env=env,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["database_path"].endswith("autotrade-test.db")
    assert payload["dealers"] == 1
    assert payload["incentives"] == 4
    assert payload["market_snapshots"] == 16
    assert payload["valuations"] == 0
