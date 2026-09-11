"""Child process helper for independent replay verification."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from opportunities.lifecycle import LifecyclePolicy
from opportunities.replay import canonical_hash, replay_fixture


def main() -> None:
    policy = LifecyclePolicy(gap_limit_ms=3_000, target_delay_ms=250)
    result, metadata = replay_fixture(Path(sys.argv[1]), policy, int(sys.argv[2]))
    print(json.dumps({"hash": canonical_hash(result), "metadata": metadata}, sort_keys=True))


if __name__ == "__main__":
    main()
