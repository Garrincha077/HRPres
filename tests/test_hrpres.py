"""Synthetic offline checks. Tests never contact the ANON portal."""
from __future__ import annotations

import gzip
import json
import sqlite3

import httpx
import pytest

from hrpres.anon import AnonClient, SourceError, parse_decision, parse_results
from hrpres.cli import collect
from hrpres.storage import Corpus

DOC_ID = "11111111-2222-3333-4444-555555555555"
RESULTS = f"""
<div class="number-of-rslts">1 rezultat</div>
<a class="search-result" href="/Document/View?id={DOC_ID}">
  <div class="decision-number">Rev-1/2026-2</div>
  <div class="decision-court">Vrhovni sud Republike Hrvatske</div>
  <div class="decision-date">21.10.2026.</div>
</a>
"""
DECISION = """
<div data-metadata-type="decision-number">
<p class="metadata-title">Broj</p><p class="metadata-content">Rev-1/2026-2</p></div>
<div data-metadata-type="court">
<p class="metadata-content">Vrhovni sud Republike Hrvatske</p></div>
<div data-metadata-type="decision-date">
<p class="metadata-content">21.10.2026.</p></div>
<div class="decision-text">
<p>P R E S U D A</p><p>Odbija se revizija.</p>
<p>Prema Zakonu o obveznim odnosima strah se može nadoknaditi.</p>
</div>
"""


def mocked(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/Document/DisplayList":
        if request.url.params.get("page") == "2":
            return httpx.Response(200, text='<div class="number-of-rslts">0 rezultata</div>')
        assert request.url.params.get("sort") == "dat"
        return httpx.Response(200, text=RESULTS)
    if request.url.path == "/Document/View":
        assert request.url.params.get("id") == DOC_ID
        return httpx.Response(200, text=DECISION)
    return httpx.Response(404)


def test_html_parsers_and_source_url():
    hits = parse_results(RESULTS)
    assert hits[0]["id"] == DOC_ID
    meta, passages = parse_decision(DECISION, DOC_ID)
    assert meta["number"] == "Rev-1/2026-2"
    assert any("strah" in passage for passage in passages)
    with pytest.raises(SourceError):
        parse_results("<html>Portal changed</html>")


def test_collect_resume_export_and_fts(tmp_path):
    corpus = Corpus(tmp_path / "work" / "checkpoint.sqlite3")
    with AnonClient(transport=httpx.MockTransport(mocked)) as anon:
        first = collect(corpus, anon, max_new=1)
        assert first["new"] == 1
        assert first["stopped"] == "pilot_limit"
        second = collect(corpus, anon, max_new=1, max_pages=2)
        assert second["new"] == 0
        assert second["already_saved"] == 1
    with pytest.raises(ValueError):
        with AnonClient(transport=httpx.MockTransport(mocked)) as anon:
            collect(corpus, anon, max_new=1001)
    manifest = corpus.export(tmp_path / "releases")
    corpus.close()
    directory = tmp_path / "releases" / manifest["release"]
    assert manifest["decisions"] == 1
    assert manifest["passages"] >= 1
    with gzip.open(directory / "documents.jsonl.gz", "rt", encoding="utf-8") as stream:
        doc = json.loads(stream.readline())
    assert doc["id"] == DOC_ID
    assert doc["source_url"].endswith(DOC_ID)
    with sqlite3.connect(directory / "search.sqlite3") as db:
        hits = db.execute("SELECT rowid FROM passages_fts WHERE passages_fts MATCH 'strah'").fetchall()
        assert hits
    assert (directory / "manifest.json").exists()
