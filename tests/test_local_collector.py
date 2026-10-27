"""Durability, source completeness checks and native runtime safeguards."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from hrpres.anon import AnonClient, ResultPage, SourceCancelled, SourceError, parse_result_page
from hrpres.collector import collect_local, local_status, pause_path, search_local
from hrpres.runtime import CollectorBusy, KeepAwake, ProcessLock
from hrpres.storage import Corpus

START = date(2020, 1, 1)
END = date(2020, 1, 31)


def did(n):
    return f"00000000-0000-0000-0000-{n:012d}"


class Client:
    def __init__(self, count=3):
        self.count = count
        self.pages = []
        self.documents = []
        self.after_save = None

    def window_page(self, page, start, end):
        self.pages.append((page, start, end))
        hits = [{"id": did(n), "date": "15.01.2020"} for n in range(1, self.count + 1)] if page == 1 else []
        return ResultPage(hits, self.count)

    def decision(self, decision_id):
        self.documents.append(decision_id)
        return {"id": decision_id, "number": "Rev-1/2020", "date": "15.01.2020"}, ["Zastara tražbine i obvezni odnosi."]


def run(corpus, client, **kw):
    return collect_local(corpus, client, min_free_gib=0, start=START, end=END, **kw)


def test_restart_drains_persisted_page_without_listing_it_again(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = Client()
    corpus = Corpus(path)
    assert run(corpus, first, max_new=1)["new"] == 1
    corpus.close()
    second = Client()
    corpus = Corpus(path)
    assert run(corpus, second, max_new=2)["decisions"] == 3
    assert second.pages == []
    assert second.documents == [did(2), did(3)]
    corpus.close()
    assert local_status(path)["pending_decisions"] == 0


def test_network_failure_keeps_pending_queue_and_resume(tmp_path):
    client = Client()
    original = client.decision
    client.decision = lambda _: (_ for _ in ()).throw(SourceError("unavailable"))
    corpus = Corpus(tmp_path / "state.sqlite3")
    with pytest.raises(SourceError, match="unavailable"):
        run(corpus, client, max_new=1)
    assert local_status(corpus.path)["pending_decisions"] == 3
    client.decision = original
    result = run(corpus, client, max_new=3)
    assert result["new"] == 3
    assert len(client.pages) == 1
    corpus.close()


def test_pause_after_commit_and_continue(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    client = Client()

    def progress(event):
        if event["event"] == "decision_saved":
            pause_path(corpus.path).write_text("pause")

    result = run(corpus, client, max_new=3, progress=progress)
    assert result["stopped"] == "paused"
    assert result["decisions"] == 1
    pause_path(corpus.path).unlink()
    assert run(corpus, client, max_new=2)["decisions"] == 3
    assert len(client.pages) == 1
    corpus.close()


def test_interrupt_between_decision_commit_and_queue_ack_is_idempotent(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    original = corpus.put

    def interrupted_put(meta, passages):
        original(meta, passages)
        raise KeyboardInterrupt

    corpus.put = interrupted_put
    assert run(corpus, Client(), max_new=1)["stopped"] == "paused"
    corpus.put = original
    result = run(corpus, Client(), max_new=2)
    assert result["decisions"] == 3
    assert result["already_saved"] == 1
    corpus.close()


def test_result_cap_splits_and_visits_newer_half_first(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    client = Client(1)
    original = client.window_page

    def pages(page, start, end):
        if (start, end) == (START, END):
            client.pages.append((page, start, end))
            return ResultPage([], 10000)
        return ResultPage([{"id": did(1), "date": "20.01.2020"}], 1)

    client.window_page = pages
    client.decision = lambda _: ({"id": did(1), "number": "P-1", "date": "20.01.2020"}, ["Odluka"])
    result = run(corpus, client, max_new=1)
    assert result["window"] == {"from": "2020-01-17", "to": "2020-01-31"}
    assert corpus.db.execute("SELECT COUNT(*) FROM crawl_windows WHERE state='split'").fetchone()[0] == 1
    corpus.close()


def test_capped_single_day_stops_without_false_completion(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    client = Client()
    client.window_page = lambda *args: ResultPage([], 10000)
    with pytest.raises(SourceError, match="po sudovima"):
        collect_local(corpus, client, start=START, end=START, min_free_gib=0)
    assert corpus.db.execute("SELECT state FROM crawl_windows").fetchone()[0] == "blocked"
    corpus.close()


def test_repeated_page_blocks_window(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    client = Client(1)
    client.window_page = lambda *args: ResultPage([{"id": did(1)}], 3)
    with pytest.raises(SourceError, match="ponavlja"):
        run(corpus, client, max_new=3)
    assert corpus.db.execute("SELECT state FROM crawl_windows").fetchone()[0] == "blocked"
    corpus.close()


def test_count_mismatch_reconciles_but_never_marks_partial_range_complete(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    client = Client(1)
    original = client.window_page
    client.window_page = lambda *args: ResultPage(original(*args).hits, 2)
    with pytest.raises(SourceError, match="ne odgovara"):
        run(corpus, client, max_new=3)
    assert corpus.db.execute("SELECT state FROM crawl_windows").fetchone()[0] == "blocked"
    assert corpus.counts()["decisions"] == 1
    corpus.close()


def test_complete_scope_matches_counts_and_searches_read_only(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    result = run(corpus, Client(1), max_new=2)
    assert result["stopped"] == "scope_scanned"
    assert result["coverage_independently_verified"] is False
    corpus.close()
    assert local_status(tmp_path / "state.sqlite3")["windows"] == {"complete": 1}
    hit = search_local(tmp_path / "state.sqlite3", 'zastara - "tražbine"')[0]
    assert hit["source_url"].endswith(did(1))
    assert "Zastara" in hit["snippet"]


def test_page_budget_resumes_instead_of_restarting_first_page(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    client = Client(1)
    first = run(corpus, client, max_new=5, max_pages=1)
    assert first["stopped"] == "page_limit"
    second = run(corpus, client, max_new=5, max_pages=1)
    assert second["stopped"] == "scope_scanned"
    assert [p[0] for p in client.pages] == [1, 2]
    corpus.close()


def test_existing_plan_cannot_be_silently_replaced(tmp_path):
    corpus = Corpus(tmp_path / "state.sqlite3")
    run(corpus, Client(), max_new=1)
    with pytest.raises(ValueError, match="spremljen vremenski raspon"):
        collect_local(corpus, Client(), start=date(2019, 1, 1), min_free_gib=0)
    corpus.close()


def test_disk_guard_stops_before_network_and_preserves_data(tmp_path, monkeypatch):
    monkeypatch.setattr("hrpres.runtime.shutil.disk_usage", lambda _: SimpleNamespace(free=100))
    corpus = Corpus(tmp_path / "state.sqlite3")
    client = Client()
    with pytest.raises(RuntimeError, match="Premalo prostora"):
        collect_local(corpus, client, min_free_gib=1)
    assert client.pages == [] and client.documents == []
    assert corpus.counts()["decisions"] == 0
    corpus.close()


def page_html(total=0, filtered_start="2020-01-01", filtered_end="2020-01-31", hits=""):
    filters = {"decisionDateFrom": filtered_start, "decisionDateTo": filtered_end, "sortBy": "dat"}
    return f'<div class="number-of-rslts">{total} rezultata</div>{hits}<script>var jsonFilters = \'{json.dumps(filters)}\';</script>'


def test_filters_must_be_confirmed_and_positive_results_cannot_disappear():
    assert parse_result_page(page_html(), start=START, end=END, page=1).total == 0
    with pytest.raises(SourceError, match="drugi raspon"):
        parse_result_page(page_html(filtered_end="2020-02-01"), start=START, end=END, page=1)
    with pytest.raises(SourceError, match="parser"):
        parse_result_page(page_html(total=3), start=START, end=END, page=1)
    with pytest.raises(SourceError):
        parse_result_page("<html>New layout</html>", start=START, end=END, page=1)


def test_retry_after_is_not_shortened_to_sixty_seconds():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "180"}) if len(calls) == 1 else httpx.Response(200, text="ok")

    with AnonClient(transport=httpx.MockTransport(handler)) as client:
        waits = []
        client._wait = waits.append
        assert client.get("/Document/DisplayList") == "ok"
        assert 180 in waits


def test_pause_cancels_retry_wait_before_a_second_request():
    paused = False
    calls = []

    def handler(request):
        nonlocal paused
        calls.append(request)
        paused = True
        return httpx.Response(429, headers={"Retry-After": "180"})

    with AnonClient(transport=httpx.MockTransport(handler), cancelled=lambda: paused) as client:
        with pytest.raises(SourceCancelled):
            client.get("/Document/DisplayList")
    assert len(calls) == 1


def test_os_lock_is_exclusive_and_released_after_process_crash(tmp_path):
    lock = tmp_path / "collector.lock"
    code = ("from pathlib import Path; import sys; from hrpres.runtime import ProcessLock; "
            "lock=ProcessLock(Path(sys.argv[1])); lock.__enter__(); print('ready',flush=True); sys.stdin.readline()")
    child = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(CollectorBusy):
            with ProcessLock(lock):
                pass
    finally:
        child.terminate()
        child.communicate(timeout=10)
    with ProcessLock(lock):
        pass


@pytest.mark.skipif(os.name != "nt", reason="Windows thread execution state")
def test_sleep_prevention_released_on_exception(monkeypatch):
    calls = []

    class Function:
        def __call__(self, flags):
            calls.append(flags)
            return 1

    monkeypatch.setattr("ctypes.windll.kernel32.SetThreadExecutionState", Function())
    with pytest.raises(RuntimeError):
        with KeepAwake() as state:
            assert state.active
            raise RuntimeError("interrupted")
    assert calls == [0x80000001, 0x80000000]
