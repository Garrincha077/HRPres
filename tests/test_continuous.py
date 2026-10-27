"""Continuous downloader scheduling and lifecycle; no network or model download."""
from __future__ import annotations

import threading
import subprocess
import sys
from datetime import date

import httpx
import pytest

from hrpres.anon import AnonClient, ResultPage, SourceError, TransientSourceError
from hrpres.collector import collect_local, pause_path, prepare, refresh_follow
from hrpres.storage import Corpus
from hrpres.continuous import collector_lease
from hrpres.runtime import CollectorBusy, ProcessLock


class ManyDecisions:
    def __init__(self, count):
        self.count = count

    def window_page(self, page, start, end):
        return ResultPage([{"id": f"00000000-0000-0000-0000-{i:012d}"} for i in range(1, self.count+1)] if page == 1 else [], self.count)

    def decision(self, did):
        return {"id": did, "number": "P-1/2020", "date": "15.01.2020"}, ["Odluka suda."]


def test_unbounded_download_passes_the_old_thousand_limit(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    result = collect_local(corpus, ManyDecisions(1001), max_new=None, max_pages=None,
                           start=date(2020,1,1), end=date(2020,1,31), min_free_gib=0)
    assert result["new"] == result["decisions"] == 1001
    assert result["stopped"] == "scope_scanned"
    corpus.close()


def test_revisit_finds_new_uuid_and_counts_current_generation(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    collect_local(corpus, ManyDecisions(3), max_new=10, start=date(2020,1,1), end=date(2020,1,31), min_free_gib=0)
    refresh_follow(corpus, today=date(2020,1,31))
    result = collect_local(corpus, ManyDecisions(4), max_new=10, min_free_gib=0)
    assert result["new"] == 1 and result["decisions"] == 4
    refresh_follow(corpus, today=date(2020,1,31))
    result = collect_local(corpus, ManyDecisions(2), max_new=10, min_free_gib=0)
    assert result["stopped"] == "scope_scanned"  # removed listings do not cause a false count mismatch
    assert corpus.db.execute("SELECT generation FROM crawl_windows").fetchone()[0] == 2
    corpus.close()


def test_follow_extends_scope_without_overlapping_existing_windows(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    prepare(corpus, date(2020,1,1), date(2020,1,30))
    result = refresh_follow(corpus, today=date(2020,2,2))
    assert result["end_date"] == "2020-02-02"
    ranges = corpus.db.execute("SELECT start_date,end_date FROM crawl_windows ORDER BY start_date").fetchall()
    assert [tuple(r) for r in ranges] == [("2020-01-01","2020-01-30"),("2020-01-31","2020-01-31"),("2020-02-01","2020-02-02")]
    corpus.close()


def test_refresh_reopens_one_window_instead_of_starving_history(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    prepare(corpus, date(2020,1,1), date(2020,3,31))
    with corpus.db:
        corpus.db.execute("UPDATE crawl_windows SET state='complete',finished_utc='2020-04-01'")
    refresh_follow(corpus, today=date(2020,3,31))
    assert corpus.db.execute("SELECT COUNT(*) FROM crawl_windows WHERE state='pending'").fetchone()[0] == 1
    corpus.close()


def test_pause_interrupts_wait_after_catching_up(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    prepare(corpus, date.today(), date.today())
    stop = threading.Event()
    result = collect_local(corpus, ManyDecisions(0), max_new=None, max_pages=None, min_free_gib=0, follow=True,
                           cancelled=stop.is_set, progress=lambda e: stop.set() if e["event"] == "waiting" else None)
    assert result["stopped"] == "paused"
    corpus.close()


def test_final_server_retry_after_is_preserved_for_recovery():
    with AnonClient(transport=httpx.MockTransport(lambda _: httpx.Response(429,headers={"Retry-After":"180"}))) as client:
        client._wait = lambda _: None
        with pytest.raises(TransientSourceError) as error:
            client.get("/Document/DisplayList")
        assert error.value.retry_after == 180


def test_layout_errors_remain_fatal_instead_of_being_retried():
    assert not issubclass(SourceError, TransientSourceError)


def test_mode_switch_pauses_only_its_own_database(tmp_path):
    source=tmp_path/"state.sqlite3"
    lock=tmp_path/"host.lock"
    code=("from pathlib import Path; import sys,time; from hrpres.runtime import ProcessLock; "
          "source=Path(sys.argv[1]); a=ProcessLock(Path(sys.argv[2])); b=ProcessLock(source.with_name(source.name+'.lock')); "
          "a.__enter__(); b.__enter__(); print('ready',flush=True); "
          "\nwhile not source.with_name(source.name+'.pause').exists(): time.sleep(.02)\n")
    child=subprocess.Popen([sys.executable,"-c",code,str(source),str(lock)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=="ready"
        with collector_lease(source,lock,take_over=True,wait_seconds=5):
            assert pause_path(source).exists()
        child.wait(timeout=5)
    finally:
        if child.poll() is None: child.terminate()
        child.communicate(timeout=5)


def test_mode_switch_does_not_pause_an_unrelated_root(tmp_path):
    source=tmp_path/"state.sqlite3"
    lock=tmp_path/"host.lock"
    with ProcessLock(lock):
        with pytest.raises(CollectorBusy,match="drugom bazom"):
            with collector_lease(source,lock,take_over=True): pass
    assert not pause_path(source).exists()
