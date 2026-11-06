"""CLI entry point.

Examples:
    python main.py "ivf cost india"
    python main.py "standing desk" -p google,reddit,youtube -n 20 -c 15 -o results.json
    python main.py "skincare" -p instagram --hashtags skincare,skincareroutine
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from market_scraper import ALL_PLATFORMS, Aggregator, SearchRequest, Settings, use_system_certificates


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate public market-research signals for a query.")
    parser.add_argument("query", help="search query, e.g. \"electric scooter reviews\"")
    parser.add_argument("-p", "--platforms", default="all",
                        help=f"comma-separated subset of: {','.join(ALL_PLATFORMS)} (default: all)")
    parser.add_argument("-n", "--limit", type=int, help="max primary results per platform (default: DEFAULT_LIMIT)")
    parser.add_argument("-c", "--comments", type=int,
                        help="max comments/answers per thread or video; 0 disables (default: DEFAULT_COMMENTS)")
    parser.add_argument("--hashtags", default="", help="Instagram hashtags to query (default: derived from query)")
    parser.add_argument("-o", "--output", help="write JSON here instead of stdout")
    parser.add_argument("--compact", action="store_true", help="minified JSON")
    parser.add_argument("--env-file", default=".env", help="path to .env file (default: .env)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="-v for info, -vv for debug logs")
    return parser.parse_args(argv)


def resolve_platforms(raw: str) -> list[str]:
    if raw.strip().lower() == "all":
        return list(ALL_PLATFORMS)
    chosen = [p.strip().lower() for p in raw.split(",") if p.strip()]
    unknown = [p for p in chosen if p not in ALL_PLATFORMS]
    if unknown:
        raise SystemExit(f"error: unknown platform(s) {unknown}; choose from {list(ALL_PLATFORMS)}")
    return list(dict.fromkeys(chosen))


def print_summary(payload: dict[str, Any]) -> None:
    err = sys.stderr
    print(f"\nQuery: {payload['query']!r}  |  {payload['total_results']} results in "
          f"{payload['duration_seconds']}s", file=err)
    for name, info in payload["platforms"].items():
        detail = info.get("message", "")
        if info.get("warnings"):
            detail = (detail + f" ({len(info['warnings'])} warning(s))").strip()
        print(f"  {name:<10} {info['status']:<8} {info['count']:>4}  {detail}", file=err)
    topics = [t["term"] for t in payload["insights"]["trending_topics"][:10]]
    if topics:
        print(f"Trending: {', '.join(topics)}", file=err)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    use_system_certificates()
    logging.basicConfig(
        level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx INFO logs full URLs incl. API keys

    settings = Settings.from_env(args.env_file)
    request = SearchRequest(
        query=args.query.strip(),
        limit=max(1, args.limit if args.limit is not None else settings.default_limit),
        comments_per_item=max(0, args.comments if args.comments is not None else settings.default_comments),
        hashtags=tuple(h.strip().lstrip("#") for h in args.hashtags.split(",") if h.strip()),
    )
    if not request.query:
        raise SystemExit("error: query must not be empty")

    try:
        payload = asyncio.run(Aggregator(settings).run(request, resolve_platforms(args.platforms)))
    except KeyboardInterrupt:
        return 130

    text = json.dumps(payload, ensure_ascii=False, indent=None if args.compact else 2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
        print(text)

    print_summary(payload)
    return 0 if any(p["status"] == "ok" for p in payload["platforms"].values()) else 1


if __name__ == "__main__":
    sys.exit(main())
