from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import requests
from bs4 import BeautifulSoup


SEARCH_URL = "https://sukebei.nyaa.si/"


@dataclass(slots=True)
class SearchResult:
    title: str
    magnet: str
    size: str


class AVSearchService:
    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()

    def search(self, query: str) -> list[SearchResult]:
        response = self.session.get(
            SEARCH_URL,
            params={"q": query, "f": 0, "c": "0_0"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return list(self._parse_results(response.text))

    def _parse_results(self, html: str) -> Iterable[SearchResult]:
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.select("table tbody tr"):
            title_tag = row.select_one('a[href^="/view/"]')
            magnet_tag = row.select_one('a[href^="magnet:"]')
            size_cell = row.select("td")
            if not title_tag or not magnet_tag:
                continue
            size = size_cell[3].get_text(" ", strip=True) if len(size_cell) > 3 else ""
            yield SearchResult(
                title=title_tag.get_text(" ", strip=True),
                magnet=magnet_tag["href"],
                size=size,
            )
