from __future__ import annotations

import sys

from collaborative_vibe_coder.cli import main as cli_main


def main() -> int:
    return cli_main(["supervise", *sys.argv[1:]])
