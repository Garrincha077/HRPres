"""Bounded public pilot with a pinned ONNX encoder and offline local queries.

The same quantized graph, tokenizer, mean pooling and normalization are used
for both document segments and queries. No Ollama or generative model.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import time
from pathlib import Path

MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
MODEL_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
MODEL_FILE = "onnx/model_quint8_avx2.onnx"
MAX_TOKENS = 128
OVERLAP = 24
PROBE_QUERIES = [
    "Koji je rok za žalbu protiv rješenja?",
    "Naknada troškova ovršnog postupka i platni nalog",
    "Obveza plaćanja duga i zakonske zatezne kamate",
]


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_source(release: Path) -> tuple[dict, list[dict]]:
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    source = release / "documents.jsonl.gz"
    if digest(source) != manifest["files"][source.name]["sha256"]:
        raise ValueError("Neispravan kontrolni zbroj izvornog paketa.")
    docs = []
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        for line in stream:
            doc = json.loads(line)
            if doc["source_url"] != "https://odluke.sudovi.hr/Document/View?id=" + doc["id"]:
                raise ValueError("Pilot dopušta samo odluke iz javnog izvora ANON.")
            for passage in doc["passages"]:
                if hashlib.sha256(passage["text"].encode()).hexdigest() != passage["sha256"]:
                    raise ValueError("Izmijenjen izvorni odlomak.")
            docs.append(doc)
            if len(docs) > 25:
                raise ValueError("Ovaj workflow je ograničen na 25 odluka.")
    if len(docs) != 25 or len({doc["id"] for doc in docs}) != 25:
        raise ValueError("Potreban je točno ovaj pilot od 25 jedinstvenih odluka.")
    return manifest, docs


class Encoder:
    def __init__(self, model_dir: Path, *, threads=2):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.no_padding()
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS, stride=OVERLAP)
        config = json.loads((model_dir / "tokenizer_config.json").read_text(encoding="utf-8"))
        pad = config["pad_token"]
        self.pad_id = self.tokenizer.token_to_id(pad if isinstance(pad, str) else pad["content"])
        if self.pad_id is None:
            raise ValueError("Tokenizer nema očekivani padding token.")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(model_dir / "model.onnx"), options,
                                           providers=["CPUExecutionProvider"])

    def encode_tokens(self, encodings):
        import numpy as np

        length = max(len(e.ids) for e in encodings)
        ids = np.full((len(encodings), length), self.pad_id, dtype=np.int64)
        mask = np.zeros_like(ids)
        types = np.zeros_like(ids)
        for i, encoding in enumerate(encodings):
            ids[i, :len(encoding.ids)] = encoding.ids
            mask[i, :len(encoding.ids)] = encoding.attention_mask
            types[i, :len(encoding.ids)] = encoding.type_ids
        values = {"input_ids": ids, "attention_mask": mask, "token_type_ids": types}
        output = self.session.run(None, {i.name: values[i.name] for i in self.session.get_inputs()})[0]
        if output.ndim != 3 or output.shape[-1] != 384:
            raise ValueError("Neočekivan ONNX izlaz; potreban je model s 384 dimenzije po tokenu.")
        weighted = output * mask[:, :, None].astype(np.float32)
        pooled = weighted.sum(axis=1) / mask.sum(axis=1, keepdims=True)
        pooled = np.asarray(pooled, dtype=np.float32)
        return pooled / np.maximum(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12)

    def query(self, text: str):
        encoded = self.tokenizer.encode(text)
        if encoded.overflowing:
            raise ValueError("Upit je dulji od 128 tokena; skrati pitanje za ovaj pilot.")
        return self.encode_tokens([encoded])[0]


def split_passage(encoder: Encoder, text: str):
    encoding = encoder.tokenizer.encode(text)
    for number, part in enumerate([encoding, *encoding.overflowing], 1):
        ranges = [(a, b) for (a, b), special in zip(part.offsets, part.special_tokens_mask) if not special and b > a]
        if not ranges:
            continue
        if len(part.ids) > MAX_TOKENS:
            raise ValueError("Segment premašuje token granicu.")
        yield number, min(a for a, _ in ranges), max(b for _, b in ranges), part


def build(release: Path, out: Path) -> dict:
    import faiss
    import numpy as np
    from huggingface_hub import hf_hub_download

    source_manifest, docs = load_source(release)
    if out.exists():
        raise ValueError("Odredište već postoji; koristi novu mapu za novu izradu.")
    out.mkdir(parents=True)
    model_dir = out / "model"
    model_dir.mkdir()
    for source, target in [(MODEL_FILE, "model.onnx"), ("tokenizer.json", "tokenizer.json"),
                           ("tokenizer_config.json", "tokenizer_config.json"),
                           ("sentence_bert_config.json", "sentence_bert_config.json")]:
        downloaded = hf_hub_download(repo_id=MODEL_ID, filename=source,
                                     revision=MODEL_REVISION, token=False)
        shutil.copyfile(downloaded, model_dir / target)
    model_config = json.loads((model_dir / "sentence_bert_config.json").read_text(encoding="utf-8"))
    if model_config["max_seq_length"] != MAX_TOKENS:
        raise ValueError("Promijenjena token granica modela.")
    started = time.monotonic()
    encoder = Encoder(model_dir)
    records, vectors, batch = [], [], []
    for doc in docs:
        for passage in doc["passages"]:
            for segment, begin, end, encoding in split_passage(encoder, passage["text"]):
                text = passage["text"][begin:end]
                records.append({"decision_id": doc["id"], "number": doc["number"], "court": doc["court"],
                                "date": doc["date"], "source_url": doc["source_url"],
                                "passage_seq": passage["seq"], "segment_seq": segment,
                                "source_sha256": passage["sha256"], "start_char": begin, "end_char": end,
                                "text": text, "tokens": len(encoding.ids)})
                batch.append(encoding)
                if len(batch) == 16:
                    vectors.append(encoder.encode_tokens(batch))
                    batch = []
    if batch:
        vectors.append(encoder.encode_tokens(batch))
    matrix = np.ascontiguousarray(np.vstack(vectors), dtype=np.float32)
    if matrix.shape != (len(records), 384) or not np.isfinite(matrix).all():
        raise ValueError("Neispravan rezultat vektorizacije.")
    np.save(out / "vectors.npy", matrix, allow_pickle=False)
    index = faiss.IndexFlatIP(384)
    index.add(matrix)
    faiss.write_index(index, str(out / "vectors.faiss"))
    (out / "segments.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    shutil.copyfile(release / "documents.jsonl.gz", out / "documents.jsonl.gz")
    probe_vectors = np.vstack([encoder.query(query) for query in PROBE_QUERIES])
    np.save(out / "probe-queries.npy", probe_vectors, allow_pickle=False)
    probes = [{"query": query, "top_rows": np.argsort(-(matrix @ vec))[:5].tolist()}
              for query, vec in zip(PROBE_QUERIES, probe_vectors)]
    (out / "probe-results.json").write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "schema": "hrpres-onnx-pilot/1", "source_release": source_manifest["release"],
        "source_sha256": digest(release / "documents.jsonl.gz"), "decisions": len(docs),
        "original_passages": sum(len(d["passages"]) for d in docs), "vectors": len(records),
        "dimension": 384, "model": MODEL_ID, "model_revision": MODEL_REVISION,
        "model_file": MODEL_FILE, "max_tokens": MAX_TOKENS, "overlap_tokens": OVERLAP,
        "pooling": "attention-mask-mean", "normalized": True, "backend": "onnxruntime-cpu-uint8-avx2",
        "embedding_seconds": round(time.monotonic() - started, 3),
        "attribution": "Izvor: Tražilica odluka sudova Republike Hrvatske (https://odluke.sudovi.hr)",
        "warning": "Funkcionalni pilot na 25 odluka; pravna relevantnost nije validirana.",
        "files": {p.relative_to(out).as_posix(): {"bytes": p.stat().st_size, "sha256": digest(p)}
                  for p in sorted(out.rglob("*")) if p.is_file()},
    }
    (out / "vectors-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def verify(bundle: Path) -> dict:
    import numpy as np

    manifest = json.loads((bundle / "vectors-manifest.json").read_text(encoding="utf-8"))
    if manifest["schema"] != "hrpres-onnx-pilot/1" or manifest["model_revision"] != MODEL_REVISION:
        raise ValueError("Nepodržana verzija modela ili paketa.")
    for name, info in manifest["files"].items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle.resolve()) or digest(path) != info["sha256"]:
            raise ValueError("Kontrolni zbroj datoteke nije valjan: " + name)
    matrix = np.load(bundle / "vectors.npy", allow_pickle=False)
    records = [json.loads(line) for line in (bundle / "segments.jsonl").read_text(encoding="utf-8").splitlines()]
    if matrix.shape != (len(records), 384) or len(records) != manifest["vectors"]:
        raise ValueError("Neusklađen broj vektora i odlomaka.")
    if not np.isfinite(matrix).all() or not np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-4):
        raise ValueError("Vektori nisu konačni i normalizirani.")
    with gzip.open(bundle / "documents.jsonl.gz", "rt", encoding="utf-8") as stream:
        docs = [json.loads(line) for line in stream]
    sources = {(d["id"], p["seq"]): p for d in docs for p in d["passages"]}
    if len(docs) != 25 or len({r["decision_id"] for r in records}) != 25:
        raise ValueError("Paket ne obuhvaća točno 25 odluka.")
    covered = {}
    for record in records:
        key = (record["decision_id"], record["passage_seq"])
        passage = sources[key]
        a, b = record["start_char"], record["end_char"]
        if not 0 <= a < b <= len(passage["text"]) or passage["text"][a:b] != record["text"]:
            raise ValueError("Segment se ne podudara s izvornim tekstom.")
        if passage["sha256"] != record["source_sha256"] or not 1 <= record["tokens"] <= MAX_TOKENS:
            raise ValueError("Segment ima pogrešnu oznaku izvora ili duljinu.")
        covered.setdefault(key, []).append((a, b))
    for key, passage in sources.items():
        cursor = 0
        for a, b in sorted(covered.get(key, [])):
            if passage["text"][cursor:a].strip():
                raise ValueError("Dio izvornog teksta nije pokriven segmentima.")
            cursor = max(cursor, b)
        if passage["text"][cursor:].strip():
            raise ValueError("Završetak odlomka nije vektoriziran.")
    return {"decisions": len(docs), "original_passages": len(sources), "vectors": len(records),
            "dimension": 384, "hashes_verified": True, "source_text_coverage_verified": True}


def search(bundle: Path, query: str, *, encoder=None, limit=5) -> list[dict]:
    import numpy as np

    encoder = encoder or Encoder(bundle / "model")
    matrix = np.load(bundle / "vectors.npy", allow_pickle=False)
    records = [json.loads(line) for line in (bundle / "segments.jsonl").read_text(encoding="utf-8").splitlines()]
    scores = matrix @ encoder.query(query)
    found, seen = [], set()
    for row in np.argsort(-scores):
        record = records[int(row)]
        if record["decision_id"] in seen:
            continue
        seen.add(record["decision_id"])
        found.append({**record, "score": float(scores[row])})
        if len(found) == limit:
            break
    return found


def main():
    import sys
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="HRPres vektorski pilot: 25 odluka")
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("build")
    create.add_argument("--release", type=Path, required=True)
    create.add_argument("--out", type=Path, required=True)
    for action in ("verify", "search", "interactive"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--bundle", type=Path, required=True)
        if action == "search":
            cmd.add_argument("--query", required=True)
    args = parser.parse_args()
    if args.action == "build":
        print(json.dumps(build(args.release, args.out), ensure_ascii=False, indent=2))
        return
    result = verify(args.bundle)
    if args.action == "verify":
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    encoder = Encoder(args.bundle / "model")
    while True:
        if args.action == "search":
            query = args.query
        else:
            try:
                query = input("\nPitanje za pretragu (Enter za izlaz): ").strip()
            except (EOFError, KeyboardInterrupt):
                return
        if not query:
            return
        started = time.monotonic()
        try:
            hits = search(args.bundle, query, encoder=encoder)
            print(f"\nPretraga među 25 odluka: {time.monotonic() - started:.3f} s")
            for hit in hits:
                print(f"\n{hit['number']} | {hit['court']} | {hit['date']}\n{hit['text']}\n{hit['source_url']}")
        except ValueError as exc:
            print(str(exc))
        if args.action == "search":
            return


if __name__ == "__main__":
    main()
