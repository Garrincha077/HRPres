"""Resumable, bounded local downloads; no embedding model and no cloud upload."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .anon import AnonClient, SourceCancelled, SourceError
from .runtime import require_free_space
from .storage import Corpus

RESULT_CAP = 10_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pause_path(db: Path) -> Path:
    return db.with_name(db.name + ".pause")


def atomic_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def prepare(corpus: Corpus, start: date | None, end: date | None) -> dict:
    db = corpus.db
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS crawl_plan (
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          start_date TEXT NOT NULL, end_date TEXT NOT NULL, created_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS crawl_windows (
          id INTEGER PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', next_page INTEGER NOT NULL DEFAULT 1,
          reported_count INTEGER, scan INTEGER NOT NULL DEFAULT 0,
          last_error TEXT, finished_utc TEXT,
          UNIQUE(start_date,end_date)
        );
        CREATE TABLE IF NOT EXISTS crawl_queue (
          ordinal INTEGER PRIMARY KEY, window_id INTEGER NOT NULL,
          decision_id TEXT NOT NULL, listing_json TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',
          UNIQUE(window_id,decision_id),
          FOREIGN KEY(window_id) REFERENCES crawl_windows(id)
        );
        CREATE INDEX IF NOT EXISTS crawl_pending ON crawl_queue(window_id,state,ordinal);
        CREATE TABLE IF NOT EXISTS crawl_pages (
          window_id INTEGER NOT NULL, scan INTEGER NOT NULL, page INTEGER NOT NULL,
          fingerprint TEXT NOT NULL,
          PRIMARY KEY(window_id,scan,page),
          UNIQUE(window_id,scan,fingerprint)
        );
    """)
    existing = db.execute("SELECT * FROM crawl_plan WHERE singleton=1").fetchone()
    if existing:
        if (start and start.isoformat() != existing["start_date"]) or (
                end and end.isoformat() != existing["end_date"]):
            raise ValueError("Ova baza već ima spremljen vremenski raspon. "
                             "Za nastavak izostavi --date-from/--date-to; za drugi raspon koristi drugu bazu.")
        return dict(existing)
    start, end = start or date(1900, 1, 1), end or date.today()
    if start > end or end > date.today():
        raise ValueError("Neispravan vremenski raspon (završetak ne smije biti u budućnosti).")
    windows = []
    cursor = start
    while cursor <= end:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = min(end, next_month - timedelta(days=1))
        windows.append((cursor.isoformat(), last.isoformat()))
        if last == date.max:
            break
        cursor = last + timedelta(days=1)
    with db:
        db.execute("INSERT INTO crawl_plan VALUES (1,?,?,?)", (start.isoformat(), end.isoformat(), utc_now()))
        db.executemany("INSERT INTO crawl_windows(start_date,end_date) VALUES (?,?)", windows)
    return dict(db.execute("SELECT * FROM crawl_plan WHERE singleton=1").fetchone())


def _block(db: sqlite3.Connection, window_id: int, message: str):
    with db:
        db.execute("UPDATE crawl_windows SET state='blocked',last_error=? WHERE id=?", (message, window_id))
    raise SourceError(message)


def _split(db: sqlite3.Connection, window: sqlite3.Row):
    start, end = date.fromisoformat(window["start_date"]), date.fromisoformat(window["end_date"])
    if start == end:
        _block(db, window["id"], "Jedan dan doseže ograničenje od 10.000 rezultata. "
               "Raspon nije dovršen: potrebno je dodatno dijeljenje po sudovima.")
    middle = start + (end - start) // 2
    with db:
        db.execute("UPDATE crawl_windows SET state='split' WHERE id=?", (window["id"],))
        db.executemany("INSERT OR IGNORE INTO crawl_windows(start_date,end_date) VALUES (?,?)", [
            (start.isoformat(), middle.isoformat()),
            ((middle + timedelta(days=1)).isoformat(), end.isoformat()),
        ])


def collect_local(corpus: Corpus, client: AnonClient, *, max_new: int = 25,
                  max_pages: int = 200, start: date | None = None, end: date | None = None,
                  min_free_gib: float = 100, progress: Callable[[dict], None] | None = None) -> dict:
    """Commit each observed page before any of its decisions are requested.

    Restart drains the saved page queue before visiting later pages. A bounded
    reconciliation scan detects mismatched unique counts. The source is mutable;
    matching counts are not a guarantee against later publications/corrections.
    """
    if not 1 <= max_new <= 1000:
        raise ValueError("Jedno lokalno pokretanje dopušta 1 do 1.000 novih odluka.")
    if not 1 <= max_pages <= 2000:
        raise ValueError("Broj stranica mora biti između 1 i 2.000.")
    require_free_space(corpus.path.parent, min_free_gib)
    plan = prepare(corpus, start, end)
    db = corpus.db
    pause = pause_path(corpus.path)
    initial = corpus.counts()
    added_passages = 0
    started = time.monotonic()
    report = {
        "schema": "hrpres-local-run/1", "started_utc": utc_now(), "db": str(corpus.path),
        "date_from": plan["start_date"], "date_to": plan["end_date"],
        "new": 0, "already_saved": 0, "pages": 0, "stopped": "running",
        "coverage_independently_verified": False,
        "note": "Raspon je po datumu odluke. Naknadne objave i ispravci traže novu provjeru.",
    }
    report_path = corpus.path.parent / "last-run.json"

    def notify(kind: str, **details):
        if progress:
            progress({"event": kind, **report, "total": initial["decisions"] + report["new"], **details})

    def interrupted() -> bool:
        if pause.exists():
            report["stopped"] = "paused"
            return True
        return False

    def checkpoint(*, final=False):
        if final:
            report.update(corpus.counts())
            report["new"] = report["decisions"] - initial["decisions"]
        else:
            report["decisions"] = initial["decisions"] + report["new"]
            report["passages"] = initial["passages"] + added_passages
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        report["updated_utc"] = utc_now()
        atomic_json(report_path, report)

    # Do not override a caller's cancellation source; the CLI also watches this file.
    notify("started")
    try:
        while report["new"] < max_new:
            if interrupted():
                break
            require_free_space(corpus.path.parent, min_free_gib)
            blocked = db.execute("SELECT last_error FROM crawl_windows WHERE state='blocked' LIMIT 1").fetchone()
            if blocked:
                raise SourceError(blocked["last_error"])
            window = db.execute("SELECT * FROM crawl_windows WHERE state IN ('pending','active') "
                                "ORDER BY end_date DESC,start_date DESC LIMIT 1").fetchone()
            if window is None:
                report["stopped"] = "scope_scanned"
                break
            wid = window["id"]
            report["window"] = {"from": window["start_date"], "to": window["end_date"]}
            with db:
                db.execute("UPDATE crawl_windows SET state='active' WHERE id=?", (wid,))
            pending = db.execute("SELECT * FROM crawl_queue WHERE window_id=? AND state='pending' "
                                 "ORDER BY ordinal LIMIT 1", (wid,)).fetchone()
            if pending:
                did = pending["decision_id"]
                if corpus.has(did):
                    report["already_saved"] += 1
                else:
                    meta, passages = client.decision(did)
                    observed = datetime.strptime(meta["date"], "%d.%m.%Y").date()
                    if not date.fromisoformat(window["start_date"]) <= observed <= date.fromisoformat(window["end_date"]):
                        raise SourceError("Datum otvorene odluke ne odgovara spremljenom rasponu.")
                    if corpus.put(meta, passages):
                        report["new"] += 1
                        added_passages += len(passages)
                # If interrupted after put(), the UUID is still recognized on restart.
                with db:
                    db.execute("UPDATE crawl_queue SET state='saved' WHERE ordinal=?", (pending["ordinal"],))
                checkpoint()
                notify("decision_saved", decision_id=did)
                continue
            if report["pages"] >= max_pages:
                report["stopped"] = "page_limit"
                break
            page_number = window["next_page"]
            page = client.window_page(page_number, date.fromisoformat(window["start_date"]),
                                      date.fromisoformat(window["end_date"]))
            report["pages"] += 1
            with db:
                db.execute("UPDATE crawl_windows SET reported_count=?,last_error=NULL WHERE id=?", (page.total, wid))
            if page.total >= RESULT_CAP:
                _split(db, window)
                notify("window_split")
                checkpoint()
                continue
            if not page.hits:
                observed_count = db.execute("SELECT COUNT(*) FROM crawl_queue WHERE window_id=?", (wid,)).fetchone()[0]
                if observed_count != page.total:
                    if window["scan"] >= 2:
                        _block(db, wid, f"Broj jedinstvenih odluka ({observed_count}) ne odgovara "
                               f"broju izvora ({page.total}); raspon nije označen dovršenim.")
                    with db:
                        db.execute("UPDATE crawl_windows SET next_page=1,scan=scan+1 WHERE id=?", (wid,))
                    notify("window_reconcile")
                else:
                    with db:
                        db.execute("UPDATE crawl_windows SET state='complete',finished_utc=? WHERE id=?", (utc_now(), wid))
                    notify("window_scanned", observed_count=observed_count)
                checkpoint()
                continue
            fingerprint = hashlib.sha256("|".join(h["id"] for h in page.hits).encode()).hexdigest()
            repeated = db.execute("SELECT 1 FROM crawl_pages WHERE window_id=? AND scan=? AND fingerprint=?",
                                  (wid, window["scan"], fingerprint)).fetchone()
            if repeated:
                _block(db, wid, "ANON ponavlja istu stranicu rezultata; raspon nije označen dovršenim.")
            with db:
                db.execute("INSERT INTO crawl_pages VALUES (?,?,?,?)", (wid, window["scan"], page_number, fingerprint))
                db.executemany("INSERT OR IGNORE INTO crawl_queue(window_id,decision_id,listing_json) VALUES (?,?,?)", [
                    (wid, h["id"], json.dumps(h, ensure_ascii=False)) for h in page.hits
                ])
                db.execute("UPDATE crawl_windows SET next_page=? WHERE id=?", (page_number + 1, wid))
            checkpoint()
            notify("page_saved", page=page_number)
        if report["stopped"] == "running":
            report["stopped"] = "decision_limit"
    except (KeyboardInterrupt, SourceCancelled):
        report["stopped"] = "paused"
    except Exception as exc:
        report["stopped"] = "error"
        report["error"] = str(exc)
        if "window" in report:
            with db:
                db.execute("UPDATE crawl_windows SET last_error=? WHERE start_date=? AND end_date=?",
                           (str(exc), report["window"]["from"], report["window"]["to"]))
        raise
    finally:
        checkpoint(final=True)
    notify("finished")
    return report


def local_status(path: Path, *, verify=False) -> dict:
    if not path.exists():
        return {"db": str(path), "decisions": 0, "passages": 0, "state": "not_started"}
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result = {"db": str(path), "bytes": path.stat().st_size, "decisions": 0, "passages": 0}
        for table in ("decisions", "passages"):
            if table in tables:
                result[table] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if "crawl_windows" in tables:
            result["windows"] = {r[0]: r[1] for r in db.execute("SELECT state,COUNT(*) FROM crawl_windows GROUP BY state")}
            row = db.execute("SELECT start_date,end_date,next_page,reported_count,last_error FROM crawl_windows "
                             "WHERE state IN ('active','pending','blocked') ORDER BY end_date DESC,start_date DESC LIMIT 1").fetchone()
            result["next_window"] = dict(row) if row else None
            result["pending_decisions"] = db.execute("SELECT COUNT(*) FROM crawl_queue WHERE state='pending'").fetchone()[0]
            row = db.execute("SELECT * FROM crawl_plan WHERE singleton=1").fetchone()
            result["scope"] = dict(row) if row else None
        if verify:
            result["quick_check"] = db.execute("PRAGMA quick_check").fetchone()[0]
        result["pause_requested"] = pause_path(path).exists()
        result["coverage_independently_verified"] = False
    return result


def search_local(path: Path, query: str, limit: int = 10) -> list[dict]:
    import re
    tokens = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    if not tokens or not 1 <= limit <= 50:
        raise ValueError("Upiši riječi za pretragu i broj pogodaka od 1 do 50.")
    expression = " AND ".join('"' + token + '"' for token in tokens)
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("""
            SELECT d.id,d.number,d.court,d.date,d.source_url,p.seq,
              snippet(passages_fts,0,'[',']',' … ',40) AS snippet
            FROM passages_fts JOIN passages p ON p.id=passages_fts.rowid
            JOIN decisions d ON d.id=p.decision_id
            WHERE passages_fts MATCH ? ORDER BY bm25(passages_fts) LIMIT ?
        """, (expression, limit))]
