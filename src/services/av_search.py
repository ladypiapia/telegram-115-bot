from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup, Tag
from requests import Response


BASE_URL = "https://tiantianxiangshang.btchichi.hair"
SEARCH_URL_TEMPLATE = BASE_URL + "/search/{query}/page-1.html"


@dataclass(slots=True)
class SearchResult:
    title: str
    magnet: str
    size: str
    hotness: str
    created_at: str
    detail_url: str


class AVSearchService:
    def __init__(self, timeout: int = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/133.0.0.0 Safari/537.36"
                )
            }
        )

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        response = self.session.get(self._build_search_url(query), timeout=self.timeout)
        response.raise_for_status()

        results: list[SearchResult] = []
        for item in self._parse_results(response):
            magnet = self._fetch_magnet(item.detail_url)
            if not magnet:
                continue
            results.append(
                SearchResult(
                    title=item.title,
                    magnet=magnet,
                    size=item.size,
                    hotness=item.hotness,
                    created_at=item.created_at,
                    detail_url=item.detail_url,
                )
            )
            if len(results) >= limit:
                break
        return results

    def _build_search_url(self, query: str) -> str:
        return SEARCH_URL_TEMPLATE.format(query=quote(query.strip(), safe=""))

    def _parse_results(self, response: Response) -> Iterable[SearchResult]:
        response.encoding = response.encoding or response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        for article in soup.select("article.item"):
            link = article.select_one('a[href^="/hash/"]')
            title_tag = article.select_one("h4")
            if not isinstance(link, Tag) or not isinstance(title_tag, Tag):
                continue

            meta_tag = article.select_one("div > p")
            meta_text = meta_tag.get_text(" ", strip=True) if isinstance(meta_tag, Tag) else ""
            detail_url = urljoin(BASE_URL, link.get("href", ""))
            yield SearchResult(
                title=self._extract_title(title_tag),
                magnet="",
                size=self._extract_meta(meta_text, "文件大小", "創建時間"),
                hotness=self._extract_meta(meta_text, "熱度", "文件大小"),
                created_at=self._extract_meta(meta_text, "創建時間", "文件數量"),
                detail_url=detail_url,
            )

    def _fetch_magnet(self, detail_url: str) -> str | None:
        response = self.session.get(detail_url, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = response.encoding or response.apparent_encoding or "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        magnet_tag = soup.select_one('a[href^="magnet:?xt=urn:btih:"]')
        if not isinstance(magnet_tag, Tag):
            return None
        magnet = magnet_tag.get("href", "").strip()
        return magnet or None

    def _extract_title(self, title_tag: Tag) -> str:
        copied = BeautifulSoup(str(title_tag), "html.parser")
        badge = copied.select_one("span")
        if isinstance(badge, Tag):
            badge.decompose()
        return copied.get_text(" ", strip=True)

    def _extract_meta(self, meta_text: str, label: str, next_label: str) -> str:
        prefix = f"{label}："
        start = meta_text.find(prefix)
        if start < 0:
            return ""
        start += len(prefix)

        suffix = f"{next_label}："
        end = meta_text.find(suffix, start)
        if end < 0:
            end = len(meta_text)
        return meta_text[start:end].strip(" \u00a0")
