"""Run from repository root: python3 -m scripts.cluster --help."""

import argparse
import fcntl
import sys
import urllib.error

from .artifacts import prepare
from .operations import cleanup, deploy, setup, smoke
from .runtime import Lab, LabError


class Options(argparse.Namespace):
    command: str
    image: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Project-owned local kind/Knative lab")
    parser.add_argument(
        "command", choices=("prepare", "setup", "deploy", "smoke", "cleanup")
    )
    parser.add_argument("--image", default="python-serverless-lab:0.1.0")
    options = parser.parse_args(namespace=Options())
    lab = Lab()
    lab.local.mkdir(exist_ok=True, mode=0o700)
    try:
        with (lab.local / "cluster.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            match options.command:
                case "prepare":
                    prepare(lab)
                case "setup":
                    setup(lab)
                case "deploy":
                    deploy(lab, options.image)
                case "smoke":
                    smoke(lab)
                case "cleanup":
                    cleanup(lab)
    except (LabError, OSError, urllib.error.URLError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
