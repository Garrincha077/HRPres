"""Read only public decisions from https://odluke.sudovi.hr.

The portal permits automated retrieval at at most three calls per second
from one IP. All calls in this process are sequential and spaced >= 0.7 s.
Never run concurrent collectors or distribute requests across IP addresses.
"""
from __future__ import annotations

import re
import time
import html as html_lib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup
from . import __version__

BASE = "https://odluke.sudovi.hr"
SOURCE = "Tražilica odluka sudova Republike Hrvatske"
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
FIELDS = {
    "decision-number": "number",
    "court": "court",
    "decision-date": "date",
    "decision-finality": "finality",
    "ecli-number": "ecli",
    "decision-type": "kind",
    "court-registry-type": "registry",
}


class SourceError(RuntimeError):
    """The portal response cannot safely be used."""


class SourceCancelled(SourceError):
    """A local pause was requested; the pending work remains in SQLite."""


@dataclass(frozen=True)
class ResultPage:
    hits: list[dict]
    total: int


def parse_result_page(html: str, *, start: date, end: date, page: int) -> ResultPage:
    """Require both a result count and confirmation of the requested date filters."""
    soup = BeautifulSoup(html, "html.parser")
    counter = soup.select_one(".number-of-rslts")
    match = re.search(r"var jsonFilters\s*=\s*'([^']*)'", html)
    if counter is None or match is None:
        raise SourceError("Nema broja rezultata ili potvrde vremenskog filtra ANON-a.")
    try:
        filters = json.loads(html_lib.unescape(match[1]).replace('\\"', '"'))
        actual_start = date.fromisoformat(filters["decisionDateFrom"][:10])
        actual_end = date.fromisoformat(filters["decisionDateTo"][:10])
    except (ValueError, TypeError, KeyError) as exc:
        raise SourceError("ANON nije potvrdio tražene datume.") from exc
    if (actual_start, actual_end) != (start, end) or filters.get("sortBy") != "dat":
        raise SourceError("ANON je vratio drugi raspon ili poredak rezultata.")
    digits = re.sub(r"\D", "", counter.get_text(" ", strip=True))
    if not digits:
        raise SourceError("Nečitljiv broj rezultata ANON-a.")
    total = int(digits)
    hits = parse_results(html)
    if page == 1 and total > 0 and not hits:
        raise SourceError("ANON prijavljuje odluke, ali parser ne nalazi rezultate.")
    for hit in hits:
        try:
            observed = datetime.strptime(hit["date"], "%d.%m.%Y").date()
        except ValueError as exc:
            raise SourceError("Rezultat nema čitljiv datum odluke.") from exc
        if not start <= observed <= end:
            raise SourceError("Odluka je izvan potvrđenog vremenskog raspona.")
    return ResultPage(hits=hits, total=total)


def url_for(decision_id: str) -> str:
    if not UUID.fullmatch(decision_id):
        raise ValueError("Neispravan identifikator ANON odluke.")
    return f"{BASE}/Document/View?id={decision_id}"


def parse_results(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a.search-result"):
        ids = parse_qs(urlparse(a.get("href", "")).query).get("id", [])
        if not ids or not UUID.fullmatch(ids[0]):
            continue
        def field(css: str) -> str:
            node = a.select_one(css)
            return " ".join(node.get_text(" ", strip=True).split()) if node else ""
        out.append({
            "id": ids[0],
            "number": field(".decision-number"),
            "court": field(".decision-court"),
            "date": field(".decision-date").rstrip("."),
        })
    if not out and not soup.select_one(".number-of-rslts"):
        raise SourceError("ANON je promijenio izgled rezultata ili nije vratio popis.")
    return out


def _chunks(paragraphs: list[str], size: int = 1500) -> list[str]:
    pieces: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        pending = " ".join(paragraph.replace("\u00ad", "").split())
        while len(pending) > size:
            cut = pending.rfind(" ", 0, size + 1)
            if cut < size // 2:
                cut = size
            part, pending = pending[:cut].strip(), pending[cut:].strip()
            if buffer:
                pieces.append(buffer)
                buffer = ""
            if part:
                pieces.append(part)
        if not pending:
            continue
        if buffer and len(buffer) + len(pending) + 2 > size:
            pieces.append(buffer)
            buffer = ""
        buffer = f"{buffer}\n\n{pending}" if buffer else pending
    if buffer:
        pieces.append(buffer)
    return pieces


def parse_decision(html: str, decision_id: str) -> tuple[dict, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    meta = {"id": decision_id, **{value: "" for value in FIELDS.values()}}
    for node in soup.select("[data-metadata-type]"):
        name = FIELDS.get(node.get("data-metadata-type", ""))
        content = node.select_one(".metadata-content")
        if name and content and not meta[name]:
            meta[name] = " ".join(content.get_text(" ", strip=True).split())
    meta["date"] = meta["date"].rstrip(".")
    section = soup.select_one("div.decision-text")
    if section is None:
        raise SourceError(f"Nema teksta odluke {decision_id}.")
    for ignored in section.select("script, style, head"):
        ignored.decompose()
    paragraphs = [
        node.get_text(" ", strip=True)
        for node in section.select("p, li, h1, h2, h3, h4, h5, h6")
        if node.get_text(" ", strip=True)
    ]
    if not paragraphs:
        paragraphs = [line.strip() for line in section.get_text("\n").splitlines() if line.strip()]
    chunks = _chunks(paragraphs)
    if not meta["number"] or not chunks:
        raise SourceError(f"Nepotpuni metapodaci ili prazan tekst odluke {decision_id}.")
    return meta, chunks


class AnonClient:
    def __init__(self, *, interval: float = 0.7, transport=None,
                 cancelled: Callable[[], bool] | None = None):
        if interval < 0.34:
            raise ValueError("Razmak između zahtjeva mora biti barem 0,34 s.")
        self.interval = interval
        self.last = 0.0
        self.cancelled = cancelled
        self.client = httpx.Client(
            transport=transport, base_url=BASE, follow_redirects=True,
            timeout=httpx.Timeout(25.0, read=90.0),
            headers={"User-Agent": f"HRPres/{__version__} (public court decisions; github.com/Garrincha077/HRPres)"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def _check_pause(self):
        if self.cancelled is not None and self.cancelled():
            raise SourceCancelled("Zatražena je pauza.")

    def _wait(self, seconds: float):
        deadline = time.monotonic() + max(0, seconds)
        while True:
            self._check_pause()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.25) if self.cancelled else remaining)

    def get(self, path: str, **params) -> str:
        for attempt in range(4):
            self._check_pause()
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                self._wait(wait)
            self.last = time.monotonic()
            try:
                result = self.client.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise SourceError(f"Mrežna pogreška ANON-a: {exc}") from exc
                self._wait(min(2 ** attempt, 8))
                continue
            if result.status_code == 200:
                return result.text
            if result.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                retry_after = result.headers.get("Retry-After", "")
                delay = 2 ** attempt
                if retry_after.isdecimal():
                    delay = int(retry_after)
                elif retry_after:
                    try:
                        retry_at = parsedate_to_datetime(retry_after)
                        delay = max(0, (retry_at - datetime.now(timezone.utc)).total_seconds())
                    except (ValueError, TypeError):
                        pass
                self._wait(max(delay, 1))
                continue
            raise SourceError(f"ANON je vratio HTTP {result.status_code} za {path}.")
        raise SourceError("ANON nije dostupan nakon ponovljenih pokušaja.")

    def page(self, page: int) -> list[dict]:
        return parse_results(self.get("/Document/DisplayList", q="", sort="dat", page=page))

    def window_page(self, page: int, start: date, end: date) -> ResultPage:
        html = self.get(
            "/Document/DisplayList", q="", sort="dat", page=page,
            dd_from=start.strftime("%d.%m.%Y."), dd_to=end.strftime("%d.%m.%Y."),
        )
        return parse_result_page(html, start=start, end=end, page=page)

    def decision(self, decision_id: str) -> tuple[dict, list[str]]:
        if not UUID.fullmatch(decision_id):
            raise ValueError("Neispravan identifikator.")
        return parse_decision(self.get("/Document/View", id=decision_id), decision_id)
