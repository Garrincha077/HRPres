"""CLI: independent downloader / search indexer; no Ollama dependency."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .anon import AnonClient, SourceError
from .storage import Corpus

PILOT_LIMIT = 1000


def collect(corpus: Corpus, client: AnonClient, *, max_new: int = 100, max_pages: int = 200) -> dict:
    if not 1 <= max_new <= PILOT_LIMIT:
        raise ValueError(f"Pilot dopušta 1 do {PILOT_LIMIT} novih odluka.")
    if not 1 <= max_pages <= 2000:
        raise ValueError("Neispravan broj stranica.")
    seen_pages: set[tuple[str, ...]] = set()
    added = skipped = pages = 0
    reason = "page_limit"
    for page in range(1, max_pages + 1):
        hits = client.page(page)
        pages += 1
        if not hits:
            reason = "end_of_results"
            break
        fingerprint = tuple(item["id"] for item in hits)
        if fingerprint in seen_pages:
            reason = "repeated_results_page"
            break
        seen_pages.add(fingerprint)
        for hit in hits:
            if corpus.has(hit["id"]):
                skipped += 1
                continue
            meta, passages = client.decision(hit["id"])
            if corpus.put(meta, passages):
                added += 1
            if added >= max_new:
                reason = "pilot_limit"
                break
        if added >= max_new:
            break
    return {"new": added, "already_saved": skipped, "pages": pages,
            "stopped": reason, **corpus.counts()}


def main():
    parser = argparse.ArgumentParser(description="HRPres javni korpus sudskih odluka")
    sub = parser.add_subparsers(dest="action", required=True)
    get = sub.add_parser("collect", help="Preuzmi najnovije odluke u lokalnu SQLite bazu")
    get.add_argument("--db", type=Path, required=True)
    get.add_argument("--max-new", type=int, default=100)
    get.add_argument("--max-pages", type=int, default=200)
    get.add_argument("--interval", type=float, default=0.7)
    save = sub.add_parser("export", help="Izvezi prenosivi tekstualni i FTS indeks")
    save.add_argument("--db", type=Path, required=True)
    save.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    corpus = Corpus(args.db)
    try:
        if args.action == "collect":
            with AnonClient(interval=args.interval) as client:
                result = collect(corpus, client, max_new=args.max_new, max_pages=args.max_pages)
        else:
            result = corpus.export(args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, SourceError) as exc:
        parser.exit(1, f"HRPres: {exc}\n")
    finally:
        corpus.close()


if __name__ == "__main__":
    main()
