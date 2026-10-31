"""Write the malformed input fixtures to data/malformed/."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netcoop.config import data_dir  # noqa: E402
from netcoop.sim.malformed import write_fixtures  # noqa: E402

if __name__ == "__main__":
    paths = write_fixtures(data_dir() / "malformed")
    print(f"wrote {len(paths)} malformed fixtures")
