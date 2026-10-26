"""Optional GPU/Colab embedding of immutable public-corpus releases.

Install 'hrpres[embed]' in a Colab runtime. Do not put private client records
in the public-source input archives. One release = one vector shard.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path


def embed_release(release: Path, out: Path, *, model_id: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                  batch_size: int = 16, device: str | None = None) -> dict:
    import faiss
    import numpy as np
    from sentence_transformers import SentenceTransformer

    source = release / "documents.jsonl.gz"
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    from .storage import file_hash
    source_sha = file_hash(source)
    if manifest["files"]["documents.jsonl.gz"]["sha256"] != source_sha:
        raise ValueError("Neispravan SHA-256 izvornog arhiva.")
    keys: list[str] = []
    texts: list[str] = []
    with gzip.open(source, "rt", encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            for passage in item["passages"]:
                digest = hashlib.sha256(passage["text"].encode("utf-8")).hexdigest()
                if digest != passage["sha256"]:
                    raise ValueError(f"Izmijenjen odlomak odluke {item['id']}.")
                keys.append(f"{item['id']}:{passage['seq']}:{digest[:12]}")
                texts.append(passage["text"])
    if not texts:
        raise ValueError("Arhiv ne sadrži odlomke.")
    model = SentenceTransformer(model_id, device=device)
    vectors = model.encode(
        texts, batch_size=batch_size, show_progress_bar=True,
        normalize_embeddings=True, convert_to_numpy=True,
    )
    matrix = np.ascontiguousarray(np.asarray(vectors, dtype=np.float32))
    if len(matrix) != len(keys) or not np.isfinite(matrix).all():
        raise ValueError("Neispravne dimenzije ili vrijednosti embeddinga.")
    out.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out / "embeddings-f16.npz",
        keys=np.asarray(keys), vectors=matrix.astype(np.float16),
    )
    index = faiss.IndexFlatIP(int(matrix.shape[1]))
    index.add(matrix)
    faiss.write_index(index, str(out / "vectors.faiss"))
    report = {
        "schema": "hrpres-vectors/1",
        "source_release": manifest["release"],
        "source_sha256": source_sha,
        "model": model_id,
        "dimension": int(matrix.shape[1]),
        "passages": len(keys),
        "normalized": True,
        "index": "faiss-IndexFlatIP",
        "note": "Točan indeks samo za ograničene pilotne pakete; produkcija treba kompresiju.",
    }
    (out / "vectors-manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main():
    p = argparse.ArgumentParser(description="HRPres udaljena vektorizacija (Colab / GPU)")
    p.add_argument("--release", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--device", default=None)
    args = p.parse_args()
    if args.batch_size < 1:
        p.error("--batch-size mora biti pozitivan.")
    print(json.dumps(embed_release(
        args.release, args.out, model_id=args.model,
        batch_size=args.batch_size, device=args.device
    ), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
