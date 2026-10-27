"""Incremental vector durability, deduplication and coupled worker lifecycle."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from datetime import date
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")

from hrpres.anon import ResultPage
from hrpres.continuous import run_pipeline
from hrpres.live_vectors import VectorStore, search_live, watch_vectors
from hrpres.storage import Corpus


def did(n):
    return f"00000000-0000-0000-0000-{n:012d}"


def unit():
    vector = np.zeros(384, dtype=np.float32)
    vector[0] = 1
    return vector


class FakeEncoder:
    def __init__(self, _):
        pass

    def query(self, _):
        return unit()


def fake_parts(_, row):
    return [({"segment_seq":1,"start_char":0,"end_char":len(row["text"]),"tokens":3}, unit())]


def setup(tmp_path, count=3):
    source = tmp_path / "data" / "state.sqlite3"
    corpus = Corpus(source)
    for n in range(1, count+1):
        corpus.put({"id":did(n),"number":f"P-{n}/2020","date":"15.01.2020"}, [f"Odluka suda {n}."])
    corpus.close()
    bundle = tmp_path / "vectors" / "pilot-25-minilm-onnx"
    bundle.mkdir(parents=True)
    (bundle / "vectors-manifest.json").write_text(json.dumps({"files":{
        "model/model.onnx":{"sha256":"model-test"},"model/tokenizer.json":{"sha256":"tokenizer-test"}}}))
    text = "Odluka suda 1."
    record = {"decision_id":did(1),"passage_seq":1,"segment_seq":1,"start_char":0,"end_char":len(text),
              "tokens":3,"source_sha256":hashlib.sha256(text.encode()).hexdigest()}
    (bundle / "segments.jsonl").write_text(json.dumps(record)+"\n")
    np.save(bundle / "vectors.npy", np.array([unit()]), allow_pickle=False)
    return source, tmp_path / "data" / "vectors.sqlite3", bundle


def drain(source, target, bundle, **kw):
    done = threading.Event(); done.set()
    return watch_vectors(source,target,bundle,stop=threading.Event(),download_done=done,
                         min_free_gib=0,encoder_factory=FakeEncoder,validate=False,**kw)


def test_seed_reuses_pilot_and_restart_does_not_reencode(tmp_path,monkeypatch):
    source,target,bundle=setup(tmp_path)
    calls=[]
    def encode(encoder,row):
        calls.append(row["id"])
        return fake_parts(encoder,row)
    monkeypatch.setattr("hrpres.live_vectors.encode_passage",encode)
    first=drain(source,target,bundle)
    assert first["seeded_passages"]==1 and first["newly_encoded_passages"]==2
    assert calls==[2,3]
    second=drain(source,target,bundle)
    assert second["newly_encoded_passages"]==0 and second["vectors"]==3
    assert calls==[2,3]


def test_new_commits_are_indexed_after_restart(tmp_path,monkeypatch):
    source,target,bundle=setup(tmp_path,1)
    monkeypatch.setattr("hrpres.live_vectors.encode_passage",fake_parts)
    drain(source,target,bundle)
    corpus=Corpus(source)
    corpus.put({"id":did(2),"number":"P-2","date":"15.01.2020"},["Novi odlomak."])
    corpus.close()
    result=drain(source,target,bundle)
    assert result["indexed_passages"]==2 and result["newly_encoded_passages"]==1


def test_atomic_commit_does_not_acknowledge_failed_vector_write(tmp_path):
    source,target,bundle=setup(tmp_path,1)
    store=VectorStore(target,source,bundle)
    row=store.pending()[0]
    store.db.execute("CREATE TRIGGER reject_vector BEFORE INSERT ON live_vectors BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.put(row,fake_parts(None,row))
    assert store.counts()["vectors"]==0
    assert not store.already_indexed(row)
    store.db.execute("DROP TRIGGER reject_vector")
    assert store.put(row,fake_parts(None,row))
    assert store.already_indexed(row)
    store.close()


def test_changed_source_is_not_returned_with_stale_vectors(tmp_path,monkeypatch):
    source,target,bundle=setup(tmp_path,1)
    monkeypatch.setattr("hrpres.live_vectors.encode_passage",fake_parts)
    drain(source,target,bundle)
    text="Ispravljeni odlomak."
    with sqlite3.connect(source) as db:
        db.execute("UPDATE passages SET text=?,content_sha256=? WHERE id=1",(text,hashlib.sha256(text.encode()).hexdigest()))
    assert search_live(source,target,bundle,"upit",encoder=FakeEncoder(None))==[]
    result=drain(source,target,bundle)
    assert result["vectors"]==1 and result["newly_encoded_passages"]==1
    assert search_live(source,target,bundle,"upit",encoder=FakeEncoder(None))[0]["text"]==text


def test_model_or_source_identity_cannot_be_silently_changed(tmp_path):
    source,target,bundle=setup(tmp_path,1)
    store=VectorStore(target,source,bundle);store.close()
    manifest=json.loads((bundle/"vectors-manifest.json").read_text())
    manifest["files"]["model/model.onnx"]["sha256"]="other-model"
    (bundle/"vectors-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match="drugom izvoru ili modelu"):
        VectorStore(target,source,bundle)


def test_search_keeps_unique_decisions_and_bounded_result_count(tmp_path,monkeypatch):
    source,target,bundle=setup(tmp_path,3)
    monkeypatch.setattr("hrpres.live_vectors.encode_passage",fake_parts)
    drain(source,target,bundle)
    hits=search_live(source,target,bundle,"upit",encoder=FakeEncoder(None),limit=2)
    assert len(hits)==len({r["decision_id"] for r in hits})==2


def test_bounded_drain_leaves_newer_source_for_the_next_run(tmp_path,monkeypatch):
    source,target,bundle=setup(tmp_path,3)
    monkeypatch.setattr("hrpres.live_vectors.encode_passage",fake_parts)
    assert drain(source,target,bundle,ceiling=2)["indexed_passages"]==2
    assert drain(source,target,bundle)["indexed_passages"]==3


def test_pipeline_couples_collection_and_vector_drain(tmp_path,monkeypatch):
    source,target,bundle=setup(tmp_path,0)
    monkeypatch.setattr("hrpres.live_vectors.encode_passage",fake_parts)

    class Client:
        def __init__(self,**kw): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def window_page(self,page,start,end): return ResultPage([{"id":did(1)},{"id":did(2)}],2)
        def decision(self,key): return {"id":key,"number":"P-1","date":date.today().strftime("%d.%m.%Y")},["Odluka suda."]

    def worker(*args,**kw):
        return watch_vectors(*args,**kw,encoder_factory=FakeEncoder,validate=False)

    result=run_pipeline(tmp_path,max_new=2,min_free_gib=0,client_factory=Client,
                        vector_worker=worker,host_lock=tmp_path/"host.lock")
    assert result["download"]["decisions"]==2
    assert result["vectors"]["indexed_passages"]==2
    assert result["vectors"]["state"]=="caught_up"


def test_vector_failure_stops_the_pipeline_and_preserves_database(tmp_path):
    setup(tmp_path,0)
    def fail(*args,**kw): raise RuntimeError("model unavailable")
    class NoNetwork:
        def __init__(self,cancelled,**kw):
            deadline=time.monotonic()+2
            while not cancelled() and time.monotonic()<deadline: time.sleep(.01)
            assert cancelled()
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def window_page(self,*args): raise AssertionError("No network in tests")
    with pytest.raises(RuntimeError,match="model unavailable"):
        run_pipeline(tmp_path,max_new=1,min_free_gib=0,vector_worker=fail,client_factory=NoNetwork,host_lock=tmp_path/"host.lock")
    with sqlite3.connect(tmp_path/"data"/"state.sqlite3") as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0]=="ok"
