"""Build the real local station catalogue in Linux Docker, including on Windows.

uv run --frozen python -m scripts.prepare_stations
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare():
    subprocess.run(["docker", "compose", "--profile", "prepare", "run", "--rm", "--build",
                    "--no-deps", "station-prepare"], cwd=ROOT, check=True)


if __name__ == "__main__":
    prepare()
