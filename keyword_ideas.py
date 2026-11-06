"""CLI for the keyword idea engine.

Examples:
    python keyword_ideas.py "standing desk"
    python keyword_ideas.py "ivf treatment" --country in --serp -o ideas.json
    python keyword_ideas.py "home workout" --source youtube --csv ideas.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path

from market_scraper import Settings
from market_scraper.keywords import KeywordEngine, KeywordRequest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Expand a seed keyword into questions, prepositions, comparisons and A–Z ideas.")
    parser.add_argument("keyword")
    parser.add_argument("--source", choices=["google", "youtube"], default="google")
    parser.add_argument("--country", default=None, help="2-letter country code (default: SEARCH_COUNTRY)")
    parser.add_argument("--language", default=None, help="language code (default: SEARCH_LANGUAGE)")
    parser.add_argument("--serp", action="store_true", help="add People Also Ask + related searches (1 SERP credit)")
    parser.add_argument("-o", "--output", help="write JSON here instead of stdout")
    parser.add_argument("--csv", help="also write a flat CSV of keywords")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env(args.env_file)
    request = KeywordRequest(keyword=args.keyword, source=args.source,
                             country=(args.country or settings.search_country).lower(),
                             language=(args.language or settings.search_language).lower(),
                             include_serp=args.serp)
    result = asyncio.run(KeywordEngine(settings).run(request))

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=["keyword", "group", "modifier", "position", "intent", "funnel"])
            writer.writeheader()
            writer.writerows(result["keywords"])

    counts = ", ".join(f"{g} {n}" for g, n in result["counts"].items())
    print(f"\n{result['total_keywords']} keywords for {result['keyword']!r} in {result['duration_seconds']}s "
          f"({counts}); {result['requests']['failed']} failed requests", file=sys.stderr)
    return 0 if result["total_keywords"] else 1


if __name__ == "__main__":
    sys.exit(main())
