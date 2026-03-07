from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.av_search import AVSearchService


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch AV search results and magnet links.")
    parser.add_argument("query", help="AV code or search keyword")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of results to fetch")
    args = parser.parse_args()

    service = AVSearchService()
    results = service.search(args.query, limit=args.limit)
    print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
