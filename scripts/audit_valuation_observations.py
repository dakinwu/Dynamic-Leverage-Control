"""Read stored source observations without fetching data or granting research eligibility."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dynamic_leverage.config import get_settings  # noqa: E402
from dynamic_leverage.infrastructure.database import make_engine, make_session_factory  # noqa: E402
from dynamic_leverage.services.valuation_evidence import observation_inventory  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutoff", type=datetime.fromisoformat, default=None)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("choose a new output; do not overwrite evidence")
    engine = make_engine(get_settings().database_url)
    try:
        with make_session_factory(engine)() as session:
            result = observation_inventory(session, cutoff=args.cutoff or datetime.now(UTC))
        with args.output.open("x", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(
            json.dumps(
                {"observations": result["observation_count"], "sources": len(result["sources"])}
            )
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
