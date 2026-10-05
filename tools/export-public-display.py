"""Export only the static, read-only display for a dedicated webspace root."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.public_assets import public_assets


def export(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for name, data in public_assets().items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", help="New staging directory, not the local frontend or Joomla directory")
    export(parser.parse_args().destination)
