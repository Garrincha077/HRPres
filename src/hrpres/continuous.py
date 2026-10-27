"""Continuous local collection and incremental vectors with one shared pause."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .anon import AnonClient, TransientSourceError
from .collector import atomic_json, collect_local, local_status, pause_path, prepare
from .live_vectors import search_live, watch_vectors
from .onnx_pilot import Encoder, verify
from .runtime import CollectorBusy, KeepAwake, ProcessLock
from .storage import Corpus


def locations(root: Path):
    return root / "data" / "state.sqlite3", root / "data" / "vectors.sqlite3", root / "vectors" / "pilot-25-minilm-onnx"


@contextmanager
def collector_lease(source: Path, lock_path: Path, *, take_over=False, wait_seconds=120):
    """A user-selected mode switch pauses this database's collector, never another root."""
    lease = ProcessLock(lock_path)
    try:
        lease.__enter__()
    except CollectorBusy:
        if not take_over:
            raise
        own_database_busy = False
        try:
            with ProcessLock(source.with_name(source.name + ".lock")):
                pass
        except CollectorBusy:
            own_database_busy = True
        if not own_database_busy:
            raise CollectorBusy("Collector radi nad drugom bazom. Najprije ga pauziraj.")
        pause_path(source).write_text("pause\n", encoding="utf-8")
        print("Pauziram postojeće preuzimanje ove baze i čekam spremanje napretka…", flush=True)
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                lease.__enter__()
                break
            except CollectorBusy:
                if time.monotonic() >= deadline:
                    raise CollectorBusy("Postojeći posao još završava. Napredak je sačuvan; ponovno pokreni kontinuirani način.")
                time.sleep(.25)
    try:
        yield
    finally:
        lease.__exit__(None, None, None)


def run_pipeline(root: Path, *, max_new=None, min_free_gib=100, interval=.7,
                 refresh_seconds=1800, client_factory=AnonClient, vector_worker=watch_vectors,
                 host_lock: Path | None = None, take_over=False, model_bundle: Path | None = None):
    source, vectors, bundle = locations(root)
    bundle = model_bundle or bundle
    source.parent.mkdir(parents=True, exist_ok=True)
    stop, download_done = threading.Event(), threading.Event()
    errors, latest_vectors = [], {}
    report_path = source.parent / "continuous-status.json"
    last_print = [0.0]

    def vector_progress(event):
        nonlocal latest_vectors
        latest_vectors = dict(event)

    def vector_job():
        try:
            vector_worker(source, vectors, bundle, stop=stop, download_done=download_done,
                          min_free_gib=min_free_gib, progress=vector_progress)
        except Exception as exc:
            errors.append(str(exc))
            stop.set()

    def progress(event):
        if time.monotonic() - last_print[0] >= 5 or event["event"] in ("waiting", "finished"):
            status = {"state": "waiting" if event["event"] == "waiting" else "running",
                      "decisions": event["total"], "new": event["new"], "vectors": dict(latest_vectors),
                      "updated_utc": event.get("updated_utc"), "mode": "local_continuous"}
            atomic_json(report_path, status)
            print(f"Odluke: {event['total']} (+{event['new']}); vektori: {latest_vectors.get('vectors', 0)}", flush=True)
            if event["event"] == "waiting":
                print("Spremljeni rasponi su pregledani. Čekam novu provjeru objava.", flush=True)
            last_print[0] = time.monotonic()

    lock = host_lock or Path(tempfile.gettempdir()) / "hrpres" / "anon-collector.lock"
    with collector_lease(source, lock, take_over=take_over), ProcessLock(source.with_name(source.name + ".lock")), KeepAwake():
        pause_path(source).unlink(missing_ok=True)
        corpus = Corpus(source)
        thread = None
        result = None
        try:
            prepare(corpus, None, None)
            thread = threading.Thread(target=vector_job, name="hrpres-vectors", daemon=False)
            thread.start()
            initial_count = corpus.counts()["decisions"]
            attempts = 0
            while True:
                remaining = None if max_new is None else max_new - (corpus.counts()["decisions"] - initial_count)
                if remaining is not None and remaining <= 0:
                    result = {"stopped": "decision_limit", **corpus.counts()}
                    break
                try:
                    with client_factory(interval=interval, cancelled=lambda: stop.is_set() or pause_path(source).exists()) as client:
                        result = collect_local(corpus, client, max_new=remaining, max_pages=None,
                                               min_free_gib=min_free_gib, follow=max_new is None,
                                               refresh_seconds=refresh_seconds, progress=progress,
                                               cancelled=stop.is_set)
                    break
                except TransientSourceError as exc:
                    attempts += 1
                    delay = max(min(30 * 2**min(attempts-1, 4), 300), exc.retry_after)
                    atomic_json(report_path, {"state": "retrying_network", "retry_in_seconds": delay, "error": str(exc)})
                    print(f"Privremeni prekid veze. Nastavljam za najmanje {delay:g} s; napredak je sačuvan.", flush=True)
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline and not stop.is_set() and not pause_path(source).exists():
                        stop.wait(min(.25, max(0, deadline-time.monotonic())))
                    if stop.is_set() or pause_path(source).exists():
                        result = {"stopped": "paused", **corpus.counts()}
                        break
            if result["stopped"] == "paused":
                stop.set()
            else:
                download_done.set()
        except BaseException:
            stop.set()
            raise
        finally:
            if thread is not None:
                try:
                    while thread.is_alive():
                        thread.join(.25)
                except KeyboardInterrupt:
                    stop.set()
                    thread.join()
            corpus.close()
            state = "error" if errors or result is None else ("paused" if result["stopped"] == "paused" else "stopped")
            atomic_json(report_path, {"state": state, "download": result, "vectors": dict(latest_vectors), "errors": errors})
        if errors:
            raise RuntimeError("Vektorizacija je zaustavljena: " + errors[0])
        return {"download": result, "vectors": dict(latest_vectors)}


def status(root: Path):
    source, vectors, _ = locations(root)
    result = {"download": local_status(source), "vectors": {"indexed_passages": 0, "vectors": 0}}
    if vectors.exists():
        with sqlite3.connect(vectors.resolve().as_uri() + "?mode=ro", uri=True) as db:
            result["vectors"] = {"indexed_passages": db.execute("SELECT COUNT(*) FROM embedded_passages").fetchone()[0],
                                 "vectors": db.execute("SELECT COUNT(*) FROM live_vectors").fetchone()[0]}
    file = root / "data" / "continuous-status.json"
    if file.exists():
        result["pipeline"] = json.loads(file.read_text(encoding="utf-8"))
    return result


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="HRPres kontinuirano lokalno preuzimanje i vektori")
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("run", "drain", "search", "status"):
        command = sub.add_parser(name)
        command.add_argument("--root", type=Path, default=Path("D:/HRPres"))
        if name in ("run", "drain"):
            command.add_argument("--min-free-gib", type=float, default=100)
        if name == "run":
            command.add_argument("--max-new", type=int, help="Ograničena provjera umjesto trajnog rada")
            command.add_argument("--refresh-seconds", type=float, default=1800)
            command.add_argument("--take-over", action="store_true", help="Sigurno pauziraj postojeći posao ove baze i preuzmi nastavak")
        if name == "search":
            command.add_argument("--query")
    args = parser.parse_args()
    try:
        if args.action == "run":
            run_pipeline(args.root, max_new=args.max_new, min_free_gib=args.min_free_gib,
                         refresh_seconds=args.refresh_seconds, take_over=args.take_over)
        elif args.action == "drain":
            source, vectors, bundle = locations(args.root)
            with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as db:
                ceiling = db.execute("SELECT COALESCE(MAX(id),0) FROM passages").fetchone()[0]
            done = threading.Event()
            done.set()
            print(json.dumps(watch_vectors(source, vectors, bundle, stop=threading.Event(), download_done=done,
                                           min_free_gib=args.min_free_gib, ceiling=ceiling), ensure_ascii=False, indent=2))
        elif args.action == "status":
            result = status(args.root)
            print(f"Preuzeto: {result['download']['decisions']} odluka / {result['download']['passages']} odlomaka.")
            print(f"Vektorizirano: {result['vectors']['indexed_passages']} odlomaka / {result['vectors']['vectors']} vektora.")
            print(f"Kontinuirani način: {result.get('pipeline', {}).get('state', 'još nije pokrenut')}.")
        else:
            source, vectors, bundle = locations(args.root)
            if not vectors.exists():
                raise ValueError("Vektorska baza još nije pripremljena. Pokreni kontinuirani način.")
            verify(bundle)
            encoder = Encoder(bundle / "model")
            while True:
                try:
                    query = args.query or input("\nPitanje (Enter za izlaz): ").strip()
                except (EOFError, KeyboardInterrupt):
                    return
                if not query:
                    return
                started = time.monotonic()
                for hit in search_live(source, vectors, bundle, query, encoder=encoder):
                    print(f"\n{hit['number']} | {hit['court']} | {hit['date']}\n{hit['text']}\n{hit['source_url']}")
                print(f"\nTrajanje pretrage: {time.monotonic()-started:.3f} s")
                if args.query:
                    return
    except KeyboardInterrupt:
        print("\nPauzirano; spremljeni napredak ostaje dostupan.")
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"HRPres: {exc}\n")


if __name__ == "__main__":
    main()
