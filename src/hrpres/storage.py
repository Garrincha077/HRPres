"""Local SQLite checkpoint and portable public-source release export."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .anon import BASE, SOURCE, url_for


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class Corpus:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS decisions (
              id TEXT PRIMARY KEY, number TEXT NOT NULL, court TEXT, date TEXT,
              finality TEXT, ecli TEXT, kind TEXT, registry TEXT,
              source_url TEXT NOT NULL, content_sha256 TEXT NOT NULL,
              fetched_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passages (
              id INTEGER PRIMARY KEY, decision_id TEXT NOT NULL,
              seq INTEGER NOT NULL, text TEXT NOT NULL, content_sha256 TEXT NOT NULL,
              UNIQUE(decision_id,seq),
              FOREIGN KEY(decision_id) REFERENCES decisions(id)
            );
            CREATE INDEX IF NOT EXISTS passages_decision ON passages(decision_id,seq);
            CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
              text, content='passages', content_rowid='id',
              tokenize='unicode61 remove_diacritics 2'
            );
        """)

    def close(self):
        self.db.close()

    def has(self, decision_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM decisions WHERE id=?", (decision_id,)
        ).fetchone() is not None

    def put(self, meta: dict, passages: list[str]) -> bool:
        did = meta["id"]
        if self.has(did):
            return False
        full_text = "\n\n".join(passages)
        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.db:
            self.db.execute(
                """INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (did, meta["number"], meta.get("court", ""), meta.get("date", ""),
                 meta.get("finality", ""), meta.get("ecli", ""), meta.get("kind", ""),
                 meta.get("registry", ""), url_for(did),
                 sha256(full_text.encode("utf-8")), fetched),
            )
            for seq, passage in enumerate(passages, 1):
                c = self.db.execute(
                    "INSERT INTO passages(decision_id,seq,text,content_sha256) VALUES (?,?,?,?)",
                    (did, seq, passage, sha256(passage.encode("utf-8"))),
                )
                self.db.execute(
                    "INSERT INTO passages_fts(rowid,text) VALUES (?,?)", (c.lastrowid, passage)
                )
        return True

    def counts(self) -> dict:
        return {
            "decisions": self.db.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
            "passages": self.db.execute("SELECT COUNT(*) FROM passages").fetchone()[0],
        }

    def export(self, output_dir: Path) -> dict:
        counts = self.counts()
        if not counts["decisions"]:
            raise ValueError("Nema odluka za izvoz.")
        output_dir.mkdir(parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime("hrpres-pilot-%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
        staging = output_dir / ("." + name + ".partial")
        final = output_dir / name
        staging.mkdir()
        docs_file = staging / "documents.jsonl.gz"
        with gzip.open(docs_file, "wt", encoding="utf-8", compresslevel=9) as out:
            for decision in self.db.execute(
                "SELECT * FROM decisions ORDER BY fetched_utc, id"
            ):
                entry = dict(decision)
                entry["passages"] = [
                    {"seq": p["seq"], "text": p["text"], "sha256": p["content_sha256"]}
                    for p in self.db.execute(
                        "SELECT seq,text,content_sha256 FROM passages WHERE decision_id=? ORDER BY seq",
                        (decision["id"],),
                    )
                ]
                out.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
        index_path = staging / "search.sqlite3"
        # A self-contained, consistent copy, including FTS; never sync the live database.
        destination = sqlite3.connect(index_path)
        try:
            self.db.backup(destination)
        finally:
            destination.close()
        files = {}
        for path in (docs_file, index_path):
            files[path.name] = {
                "bytes": path.stat().st_size,
                "sha256": file_hash(path),
            }
        manifest = {
            "schema": "hrpres-corpus/1",
            "release": name,
            "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": SOURCE,
            "source_url": BASE,
            "attribution": f"Izvor: {SOURCE} ({BASE})",
            **counts,
            "files": files,
            "embedding_status": "not_computed",
            "warning": "Pilot je datumski sortiran, ali nije provjereno potpuno pokrivanje izvora.",
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(staging, final)
        return {"directory": str(final), **manifest}
