"""Read only public decisions from https://odluke.sudovi.hr.

The portal permits automated retrieval at at most three calls per second
from one IP. All calls in this process are sequential and spaced >= 0.7 s.
Never run concurrent collectors or distribute requests across IP addresses.
"""
from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

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
    def __init__(self, *, interval: float = 0.7, transport=None):
        if interval < 0.34:
            raise ValueError("Razmak između zahtjeva mora biti barem 0,34 s.")
        self.interval = interval
        self.last = 0.0
        self.client = httpx.Client(
            transport=transport, base_url=BASE, follow_redirects=True,
            timeout=httpx.Timeout(25.0, read=90.0),
            headers={"User-Agent": "HRPres/0.1 (public court decisions; github.com/Garrincha077/HRPres)"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.client.close()

    def get(self, path: str, **params) -> str:
        for attempt in range(4):
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            try:
                result = self.client.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise SourceError(f"Mrežna pogreška ANON-a: {exc}") from exc
                time.sleep(min(2 ** attempt, 8))
                continue
            if result.status_code == 200:
                return result.text
            if result.status_code in (429, 500, 502, 503, 504) and attempt < 3:
                retry_after = result.headers.get("Retry-After", "")
                delay = int(retry_after) if retry_after.isdecimal() else 2 ** attempt
                time.sleep(min(max(delay, 1), 60))
                continue
            raise SourceError(f"ANON je vratio HTTP {result.status_code} za {path}.")
        raise SourceError("ANON nije dostupan nakon ponovljenih pokušaja.")

    def page(self, page: int) -> list[dict]:
        return parse_results(self.get("/Document/DisplayList", q="", sort="dat", page=page))

    def decision(self, decision_id: str) -> tuple[dict, list[str]]:
        if not UUID.fullmatch(decision_id):
            raise ValueError("Neispravan identifikator.")
        return parse_decision(self.get("/Document/View", id=decision_id), decision_id)
