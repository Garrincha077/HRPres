"""CLI: independent downloader / search indexer; no Ollama dependency."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from datetime import date
from pathlib import Path

from .anon import AnonClient, SourceError
from .storage import Corpus
from .collector import atomic_json, collect_local, local_status, pause_path, search_local
from .runtime import CollectorBusy, KeepAwake, ProcessLock

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
    download = sub.add_parser("download", help="Lokalno preuzimanje s trajnim nastavkom")
    download.add_argument("--db", type=Path, required=True)
    download.add_argument("--max-new", type=int, default=25)
    download.add_argument("--max-pages", type=int, default=200)
    download.add_argument("--interval", type=float, default=0.7)
    download.add_argument("--date-from", type=date.fromisoformat)
    download.add_argument("--date-to", type=date.fromisoformat)
    download.add_argument("--min-free-gib", type=float, default=100)
    download.add_argument("--json", action="store_true")
    for action, description in (("status", "Prikaži spremljeni napredak"),
                                ("pause", "Zatraži pauzu aktivnog preuzimanja"),
                                ("search", "Pretraži lokalne odluke po riječima")):
        command = sub.add_parser(action, help=description)
        command.add_argument("--db", type=Path, required=True)
        command.add_argument("--json", action="store_true")
        if action == "status":
            command.add_argument("--verify", action="store_true", help="Provjeri integritet baze (može potrajati)")
        if action == "search":
            command.add_argument("--query", required=True)
            command.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        if args.action == "status":
            result = local_status(args.db, verify=args.verify)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"Spremljeno: {result['decisions']} odluka / {result['passages']} odlomaka.")
                print(f"Baza: {args.db}")
                print(f"Čeka preuzimanje: {result.get('pending_decisions', 0)} odluka s već spremljenih stranica.")
                if result.get("scope"):
                    scope = result["scope"]
                    print(f"Planirani datumi odluka: {scope['start_date']} do {scope['end_date']}.")
                    print(f"Stanje raspona: {result['windows']}")
                print("Potpunost cijelog izvora još nije neovisno potvrđena.")
            return
        if args.action == "pause":
            args.db.parent.mkdir(parents=True, exist_ok=True)
            pause_path(args.db).write_text("pause\n", encoding="utf-8")
            print("Pauza je zatražena. Program završava trenutni zahtjev i čuva napredak.")
            return
        if args.action == "search":
            result = search_local(args.db, args.query, args.limit)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"Pronađeno odlomaka: {len(result)}")
                for hit in result:
                    print(f"\n{hit['number']} | {hit['court']} | {hit['date']}\n{hit['snippet']}\n{hit['source_url']}")
            return
        args.db.parent.mkdir(parents=True, exist_ok=True)
        # This per-user host lock also protects different database paths.
        with ProcessLock(Path(tempfile.gettempdir()) / "hrpres" / "anon-collector.lock"), \
                ProcessLock(args.db.with_name(args.db.name + ".lock")), KeepAwake() as awake:
            if args.action == "download":
                pause_path(args.db).unlink(missing_ok=True)
            corpus = Corpus(args.db)
            try:
                if args.action == "download":
                    if sys.platform == "win32" and not awake.active:
                        print("Upozorenje: Windows nije potvrdio sprečavanje uspavljivanja.", file=sys.stderr)

                    def show_progress(event):
                        if not args.json and event["event"] == "decision_saved" and (
                                event["new"] <= 3 or event["new"] % 10 == 0):
                            print(f"Novo: {event['new']}/{args.max_new}; ukupno: {event['total']} odluka.", flush=True)
                        elif not args.json and event["event"] == "window_split":
                            print("Raspon ima previše rezultata; dijelim ga na manje raspone.", flush=True)

                    with AnonClient(interval=args.interval, cancelled=lambda: pause_path(args.db).exists()) as client:
                        result = collect_local(corpus, client, max_new=args.max_new, max_pages=args.max_pages,
                                               start=args.date_from, end=args.date_to,
                                               min_free_gib=args.min_free_gib, progress=show_progress)
                    result["sleep_prevention_active_during_run"] = awake.active
                    atomic_json(args.db.parent / "last-run.json", result)
                    if not args.json:
                        reasons = {"decision_limit": "dosegnuta je zadana količina",
                                   "paused": "pauzirano; napredak je sačuvan",
                                   "page_limit": "dosegnut je broj stranica; nastavak je spremljen",
                                   "scope_scanned": "planirani rasponi su pregledani"}
                        print(f"\nZavršeno: {reasons.get(result['stopped'], result['stopped'])}.")
                        print(f"Novo: {result['new']}; ukupno: {result['decisions']} odluka / {result['passages']} odlomaka.")
                        print(f"Izvještaj: {args.db.parent / 'last-run.json'}")
                        return
                elif args.action == "collect":
                    with AnonClient(interval=args.interval) as client:
                        result = collect(corpus, client, max_new=args.max_new, max_pages=args.max_pages)
                else:
                    result = corpus.export(args.out)
                print(json.dumps(result, ensure_ascii=False, indent=2))
            finally:
                corpus.close()
    except (ValueError, SourceError, CollectorBusy, OSError, sqlite3.Error, RuntimeError) as exc:
        parser.exit(1, f"HRPres: {exc}\n")


if __name__ == "__main__":
    main()
