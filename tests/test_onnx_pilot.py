"""Offline source guards and encoder boundary behavior."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hrpres.onnx_pilot import Encoder, digest, load_source, split_passage

PILOT = Path(__file__).resolve().parents[1] / "examples" / "public-pilot-25"


def test_fixed_pilot_is_original_25_with_58_verified_passages():
    manifest, docs = load_source(PILOT)
    assert len(docs) == 25
    assert sum(len(d["passages"]) for d in docs) == 58
    assert manifest["files"]["documents.jsonl.gz"]["sha256"] == "580fa3b6b831e7328b50a9e435d940fe94b3902856240b85f2260b11638d2635"


def write_input(tmp_path, docs):
    with gzip.open(tmp_path / "documents.jsonl.gz", "wt", encoding="utf-8") as stream:
        for doc in docs:
            stream.write(json.dumps(doc) + "\n")
    (tmp_path / "manifest.json").write_text(json.dumps({"files": {"documents.jsonl.gz": {
        "sha256": digest(tmp_path / "documents.jsonl.gz")}}}), encoding="utf-8")


def test_source_guard_rejects_other_origin_and_modified_text(tmp_path):
    _, docs = load_source(PILOT)
    docs[0]["source_url"] = "https://example.org/private"
    write_input(tmp_path, docs)
    with pytest.raises(ValueError, match="javnog izvora"):
        load_source(tmp_path)
    _, docs = load_source(PILOT)
    docs[0]["passages"][0]["text"] += " tampered"
    write_input(tmp_path, docs)
    with pytest.raises(ValueError, match="Izmijenjen"):
        load_source(tmp_path)


def test_source_guard_rejects_extra_and_duplicate_decisions(tmp_path):
    _, docs = load_source(PILOT)
    write_input(tmp_path, docs + [docs[0]])
    with pytest.raises(ValueError, match="25 odluka"):
        load_source(tmp_path)
    docs[-1] = docs[0]
    write_input(tmp_path, docs)
    with pytest.raises(ValueError, match="jedinstvenih"):
        load_source(tmp_path)


def test_token_windows_preserve_the_overflow_tail():
    tail = SimpleNamespace(offsets=[(0, 0), (7, 15), (0, 0)], special_tokens_mask=[1, 0, 1], ids=[1, 7, 2])
    head = SimpleNamespace(offsets=[(0, 0), (0, 10), (0, 0)], special_tokens_mask=[1, 0, 1], ids=[1, 5, 2], overflowing=[tail])
    encoder = SimpleNamespace(tokenizer=SimpleNamespace(encode=lambda _: head))
    parts = list(split_passage(encoder, "Sadržaj odlomka!"))
    assert [(n, a, b) for n, a, b, _ in parts] == [(1, 0, 10), (2, 7, 15)]


def test_query_rejects_truncation():
    encoder = object.__new__(Encoder)
    encoder.tokenizer = SimpleNamespace(encode=lambda _: SimpleNamespace(overflowing=["tail"]))
    with pytest.raises(ValueError, match="dulji"):
        encoder.query("long query")


def test_pooling_ignores_padded_tokens_and_normalizes():
    np = pytest.importorskip("numpy")
    encoder = object.__new__(Encoder)
    encoder.pad_id = 0

    def inference(_, feeds):
        assert feeds["attention_mask"].tolist() == [[1, 1], [1, 0]]
        output = np.zeros((2, 2, 384), dtype=np.float32)
        output[0, :, 0] = [2, 4]
        output[1, :, 1] = [3, 1000]
        return [output]

    encoder.session = SimpleNamespace(get_inputs=lambda: [SimpleNamespace(name=n) for n in ("input_ids", "attention_mask", "token_type_ids")], run=inference)
    encodings = [SimpleNamespace(ids=[4, 5], attention_mask=[1, 1], type_ids=[0, 0]),
                 SimpleNamespace(ids=[6], attention_mask=[1], type_ids=[0])]
    matrix = encoder.encode_tokens(encodings)
    assert matrix.shape == (2, 384)
    assert matrix[0, 0] == pytest.approx(1)
    assert matrix[1, 1] == pytest.approx(1)
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1)
