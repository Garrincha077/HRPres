"""Incremental local vectors: one committed source passage at a time."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .collector import atomic_json
from .onnx_pilot import Encoder, MODEL_REVISION, digest, split_passage, verify
from .runtime import ProcessLock, require_free_space


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VectorStore:
    def __init__(self, path: Path, source: Path, bundle: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.source, self.bundle = path, source.resolve(), bundle
        self.db = sqlite3.connect(path, timeout=15, uri=True)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA cache_size=-4096")
        self.db.execute("ATTACH DATABASE ? AS corpus", (self.source.as_uri() + "?mode=ro",))
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS vector_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS embedded_passages(
              passage_id INTEGER PRIMARY KEY,source_sha256 TEXT NOT NULL,
              segments INTEGER NOT NULL,updated_utc TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS live_vectors(
              passage_id INTEGER NOT NULL,segment_seq INTEGER NOT NULL,
              decision_id TEXT NOT NULL,source_sha256 TEXT NOT NULL,
              start_char INTEGER NOT NULL,end_char INTEGER NOT NULL,tokens INTEGER NOT NULL,
              vector BLOB NOT NULL,PRIMARY KEY(passage_id,segment_seq));
            CREATE TABLE IF NOT EXISTS vector_cursor(singleton INTEGER PRIMARY KEY,cursor INTEGER NOT NULL);
            INSERT OR IGNORE INTO vector_cursor VALUES(1,0);
        """)
        manifest = json.loads((bundle / "vectors-manifest.json").read_text(encoding="utf-8"))
        expected = {"schema": "hrpres-live-vectors/1", "source": str(self.source),
                    "model_revision": MODEL_REVISION,
                    "model_sha256": manifest["files"]["model/model.onnx"]["sha256"],
                    "tokenizer_sha256": manifest["files"]["model/tokenizer.json"]["sha256"]}
        for key, value in expected.items():
            row = self.db.execute("SELECT value FROM vector_meta WHERE key=?", (key,)).fetchone()
            if row is not None and row[0] != value:
                self.db.close()
                raise ValueError("Vektorska baza pripada drugom izvoru ili modelu; ne miješaj indekse.")
        with self.db:
            for key, value in expected.items():
                self.db.execute("INSERT OR IGNORE INTO vector_meta VALUES(?,?)", (key, value))

    def close(self):
        self.db.close()

    def counts(self):
        return {"indexed_passages": self.db.execute("SELECT COUNT(*) FROM embedded_passages").fetchone()[0],
                "vectors": self.db.execute("SELECT COUNT(*) FROM live_vectors").fetchone()[0],
                "source_passages": self.db.execute("SELECT COUNT(*) FROM corpus.passages").fetchone()[0]}

    def seed_pilot(self):
        if self.db.execute("SELECT 1 FROM vector_meta WHERE key='pilot_seeded'").fetchone():
            return 0
        matrix = np.load(self.bundle / "vectors.npy", allow_pickle=False)
        records = [json.loads(line) for line in (self.bundle / "segments.jsonl").read_text(encoding="utf-8").splitlines()]
        groups = {}
        for i, record in enumerate(records):
            groups.setdefault((record["decision_id"], record["passage_seq"], record["source_sha256"]), []).append((record, matrix[i]))
        copied = 0
        for (did, seq, sha), parts in groups.items():
            row = self.db.execute("SELECT p.*,d.source_url FROM corpus.passages p JOIN corpus.decisions d ON d.id=p.decision_id "
                                  "WHERE p.decision_id=? AND p.seq=? AND p.content_sha256=?", (did, seq, sha)).fetchone()
            if row is not None:
                copied += int(self.put(row, parts))
        with self.db:
            self.db.execute("INSERT INTO vector_meta VALUES('pilot_seeded','true')")
        return copied

    def pending(self, *, repair=False, limit=32, ceiling=None):
        cursor = self.db.execute("SELECT cursor FROM vector_cursor WHERE singleton=1").fetchone()[0]
        sql = "SELECT p.*,d.source_url FROM corpus.passages p JOIN corpus.decisions d ON d.id=p.decision_id "
        if repair:
            sql += "LEFT JOIN embedded_passages e ON e.passage_id=p.id WHERE (e.passage_id IS NULL OR e.source_sha256<>p.content_sha256) "
            params = []
        else:
            sql += "WHERE p.id>? "
            params = [cursor]
        if ceiling is not None:
            sql += "AND p.id<=? "
            params.append(ceiling)
        return self.db.execute(sql + "ORDER BY p.id LIMIT ?", [*params, limit]).fetchall()

    def already_indexed(self, row):
        saved = self.db.execute("SELECT source_sha256 FROM embedded_passages WHERE passage_id=?", (row["id"],)).fetchone()
        return saved is not None and saved[0] == row["content_sha256"]

    def put(self, row, parts):
        """Vectors and passage acknowledgement commit together, including on replacement."""
        if row["source_url"] != "https://odluke.sudovi.hr/Document/View?id=" + row["decision_id"]:
            raise ValueError("Vektorizacija očekuje javni ANON izvor.")
        if hashlib.sha256(row["text"].encode()).hexdigest() != row["content_sha256"]:
            raise ValueError("Kontrolni zbroj izvornog odlomka nije valjan.")
        prepared, covered = [], []
        for record, vector in parts:
            a, b = record["start_char"], record["end_char"]
            vector = np.asarray(vector, dtype="<f4")
            if vector.shape != (384,) or not np.isfinite(vector).all() or abs(float(np.linalg.norm(vector)) - 1) > 1e-4:
                raise ValueError("Neispravan vektor odlomka.")
            if not 0 <= a < b <= len(row["text"]) or not 1 <= record["tokens"] <= 128:
                raise ValueError("Neispravne granice segmenta.")
            covered.append((a, b))
            prepared.append((row["id"], record["segment_seq"], row["decision_id"], row["content_sha256"],
                             a, b, record["tokens"], vector.tobytes()))
        position = 0
        for a, b in sorted(covered):
            if row["text"][position:a].strip():
                raise ValueError("Dio odlomka nije vektoriziran.")
            position = max(position, b)
        if row["text"][position:].strip() or not prepared:
            raise ValueError("Završetak odlomka nije vektoriziran.")
        with self.db:
            current = self.db.execute("SELECT content_sha256 FROM corpus.passages WHERE id=?", (row["id"],)).fetchone()
            if current is None or current[0] != row["content_sha256"]:
                return False
            if self.already_indexed(row):
                return False
            self.db.execute("DELETE FROM live_vectors WHERE passage_id=?", (row["id"],))
            self.db.executemany("INSERT INTO live_vectors VALUES(?,?,?,?,?,?,?,?)", prepared)
            self.db.execute("INSERT INTO embedded_passages VALUES(?,?,?,?) ON CONFLICT(passage_id) DO UPDATE SET "
                            "source_sha256=excluded.source_sha256,segments=excluded.segments,updated_utc=excluded.updated_utc",
                            (row["id"], row["content_sha256"], len(prepared), utc_now()))
        return True

    def advance(self, passage_id):
        with self.db:
            self.db.execute("UPDATE vector_cursor SET cursor=MAX(cursor,?) WHERE singleton=1", (passage_id,))


def encode_passage(encoder, row):
    windows = list(split_passage(encoder, row["text"]))
    parts = []
    for offset in range(0, len(windows), 16):
        batch = windows[offset:offset + 16]
        vectors = encoder.encode_tokens([part for _, _, _, part in batch])
        for (seq, begin, end, encoding), vector in zip(batch, vectors):
            parts.append(({"segment_seq": seq, "start_char": begin, "end_char": end, "tokens": len(encoding.ids)}, vector))
    return parts


def watch_vectors(source: Path, target: Path, bundle: Path, *, stop: threading.Event,
                  download_done: threading.Event, min_free_gib=100, progress=None,
                  encoder_factory=Encoder, validate=True, ceiling=None):
    """Drain a committed source snapshot or watch additions without loading the corpus."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if validate:
        verify(bundle)
    with ProcessLock(target.with_name(target.name + ".lock")):
        store = VectorStore(target, source, bundle)
        report_path = target.parent / "vectors-status.json"
        try:
            require_free_space(target.parent, min_free_gib)
            seeded = store.seed_pilot()
            encoder = encoder_factory(bundle / "model")
            next_repair = 0.0
            report = {"schema": "hrpres-live-vector-status/1", "state": "running", "seeded_passages": seeded,
                      "newly_encoded_passages": 0, "model_revision": MODEL_REVISION}

            def checkpoint(state):
                report.update(store.counts())
                report.update(state=state, updated_utc=utc_now())
                atomic_json(report_path, report)
                if progress:
                    progress(dict(report))

            while not stop.is_set():
                require_free_space(target.parent, min_free_gib)
                rows = store.pending(ceiling=ceiling)
                repairing = False
                if not rows and time.monotonic() >= next_repair:
                    rows = store.pending(repair=True, ceiling=ceiling)
                    repairing = True
                    if not rows:
                        next_repair = time.monotonic() + 3600
                for row in rows:
                    if stop.is_set():
                        break
                    if not store.already_indexed(row):
                        report["newly_encoded_passages"] += int(store.put(row, encode_passage(encoder, row)))
                    if not repairing:
                        store.advance(row["id"])
                if rows:
                    checkpoint("running")
                    continue
                checkpoint("snapshot_indexed" if ceiling is not None else "caught_up")
                if download_done.is_set():
                    return report
                stop.wait(1)
            checkpoint("paused")
            return report
        except Exception as exc:
            atomic_json(report_path, {"state": "error", "error": str(exc), "updated_utc": utc_now()})
            raise
        finally:
            store.close()


def search_live(source: Path, target: Path, bundle: Path, query: str, *, encoder=None, limit=5):
    """Exact CPU scan with bounded memory; latency grows with the index size."""
    if not 1 <= limit <= 50:
        raise ValueError("Odaberi od 1 do 50 odluka.")
    encoder = encoder or Encoder(bundle / "model")
    q = encoder.query(query)
    store = VectorStore(target, source, bundle)
    best = {}
    try:
        cursor = store.db.execute("SELECT v.* FROM live_vectors v JOIN corpus.passages p ON p.id=v.passage_id "
                                 "AND p.content_sha256=v.source_sha256")
        while True:
            rows = cursor.fetchmany(256)
            if not rows:
                break
            matrix = np.vstack([np.frombuffer(row["vector"], dtype="<f4") for row in rows])
            scores = matrix @ q
            for row, score in zip(rows, scores):
                did = row["decision_id"]
                if did in best:
                    if score > best[did][0]:
                        best[did] = (float(score), dict(row))
                elif len(best) < limit:
                    best[did] = (float(score), dict(row))
                else:
                    lowest = min(best, key=lambda k: best[k][0])
                    if score > best[lowest][0]:
                        del best[lowest]
                        best[did] = (float(score), dict(row))
        results = []
        for score, record in sorted(best.values(), key=lambda item: -item[0]):
            row = store.db.execute("SELECT d.number,d.court,d.date,d.source_url,p.text,p.content_sha256 FROM corpus.passages p "
                                   "JOIN corpus.decisions d ON d.id=p.decision_id WHERE p.id=?", (record["passage_id"],)).fetchone()
            if row is not None and row["content_sha256"] == record["source_sha256"]:
                results.append({"score": score, "decision_id": record["decision_id"], "number": row["number"],
                                "court": row["court"], "date": row["date"], "source_url": row["source_url"],
                                "text": row["text"][record["start_char"]:record["end_char"]]})
        return results
    finally:
        store.close()
