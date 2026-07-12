from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        raise RuntimeError("probe output path is required")
    if sys.argv[1] == "--fail":
        sys.stderr = None  # Simulate a PyInstaller Windows noconsole worker.
        raise RuntimeError("packaged-worker-probe-failure")
    if sys.argv[1] == "--exit-seven":
        return 7

    output_path = Path(sys.argv[1]).resolve()
    output_path.write_text(
        json.dumps(
            {
                "argv": sys.argv,
                "cwd": str(Path.cwd()),
                "file": str(Path(__file__).resolve()),
            }
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
