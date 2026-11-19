"""Оценка Telegram-юзернейма по публичным завершённым продажам Fragment.

Все цены — ориентиры в TON, не оферты и не гарантии продажи.
Никакой авторизации Fragment, кошельков или приватных API.
"""
from __future__ import annotations

import asyncio
import re
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import urlencode

import aiohttp

FRAGMENT = "https://fragment.com"
VALID_NAME = re.compile(r"^[a-z][a-z0-9_]{4,31}$")
ROW_RE = re.compile(r'<tr class="tm-row-selectable"[^>]*>(.*?)</tr>', re.S | re.I)
NAME_RE = re.compile(r'href="/username/([a-zA-Z0-9_]+)"')
PRICE_RE = re.compile(
    r'<div class="table-cell-value tm-value icon-before icon-ton">\s*'
    r'([\d,]+(?:\.\d+)?)\s*</div>', re.I
)
DATE_RE = re.compile(r'<time datetime="([^"]+)"')
TECH_WORDS = (
    "hex", "byte", "code", "root", "pixel", "bot", "cyber",
    "dev", "shell", "proxy", "dark", "nova", "linux", "crypto",
    "hash", "core", "cloud", "git", "data", "bit", "node",
    "kernel", "logic", "web", "net", "matrix", "debug",
)


@dataclass(frozen=True)
class FragmentSale:
    username: str
    price_ton: float
    kind: str  # sold / auction
    sold_at: datetime | None = None

    @property
    def url(self) -> str:
        return f"{FRAGMENT}/username/{self.username}"


@dataclass(frozen=True)
class Valuation:
    username: str
    score: int
    low_ton: int | None
    high_ton: int | None
    sales: tuple[FragmentSale, ...]
    auctions: tuple[FragmentSale, ...]
    checked_at: datetime
    data_ok: bool

    @property
    def has_price(self) -> bool:
        return self.low_ton is not None and self.high_ton is not None


def normalize_username(raw: str) -> str | None:
    value = raw.strip()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):].split("?", 1)[0].split("/", 1)[0]
            break
    value = value.lstrip("@").lower()
    if not VALID_NAME.fullmatch(value) or value.endswith("_") or "__" in value:
        return None
    return value


def username_score(username: str) -> int:
    """Эвристика качества названия 0–100, не цена и не вероятность продажи."""
    n = len(username)
    score = 70 - max(0, n - 4) * 3
    score += min(8, len(set(username))) * 2
    score -= username.count("_") * 10
    score -= sum(c.isdigit() for c in username) * 5
    score += 6 if username.isalpha() else 0
    if any(word in username for word in TECH_WORDS):
        score += 5
    vowel_count = sum(c in "aeiouy" for c in username)
    if vowel_count == 0:
        score -= 11
    elif n >= 7 and vowel_count < 2:
        score -= 6
    if re.search(r"[^aeiouy0-9_]{4}", username):
        score -= 9
    if re.search(r"(.)\1\1", username):
        score -= 12
    return max(1, min(99, score))


def parse_fragment_listings(html_text: str, kind: str) -> list[FragmentSale]:
    """Публичная табличная разметка, строгая проверка статуса и стоимости."""
    if kind not in {"sold", "auction"}:
        raise ValueError("Unsupported Fragment listing type")
    entries: list[FragmentSale] = []
    seen: set[str] = set()
    for row in ROW_RE.findall(html_text):
        match = NAME_RE.search(row)
        price = PRICE_RE.search(row)
        if not match or not price:
            continue
        name = match.group(1).lower()
        if name in seen:
            continue
        if kind == "sold" and not (
            "Sale price" in row and "Sold" in row
        ):
            continue
        if kind == "auction" and ("Sale price" in row or "Sold" in row):
            continue
        try:
            value = float(price.group(1).replace(",", ""))
        except ValueError:
            continue
        if value <= 0 or value > 10**9:
            continue
        date_match = DATE_RE.search(row) if kind == "sold" else None
        date = None
        if date_match:
            try:
                date = datetime.fromisoformat(date_match.group(1))
            except ValueError:
                pass
        entries.append(FragmentSale(name, value, kind, date))
        seen.add(name)
    return entries


def _similarity(target: str, comparable: str) -> float:
    similarity = SequenceMatcher(None, target, comparable).ratio()
    prefix = 0
    for a, b in zip(target, comparable):
        if a != b:
            break
        prefix += 1
    return similarity + min(prefix, 5) * 0.025 - abs(len(target) - len(comparable)) * 0.035


def rank_similar(username: str, sales: list[FragmentSale], limit: int = 6) -> list[FragmentSale]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=3 * 365)
    ranked = []
    for sale in sales:
        if sale.username == username or abs(len(sale.username) - len(username)) > 2:
            continue
        if sale.kind == "sold" and sale.sold_at and sale.sold_at < cutoff:
            continue
        score = _similarity(username, sale.username)
        if score < 0.46:
            continue
        ranked.append((score, sale))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [sale for _, sale in ranked[:limit]]


def value_from_listings(
    username: str,
    sold: list[FragmentSale],
    auctions: list[FragmentSale],
    data_ok: bool = True,
) -> Valuation:
    sales = rank_similar(username, sold, limit=6)
    comparable_auctions = rank_similar(username, auctions, limit=3)
    low, high = None, None
    if len(sales) >= 3:
        # Устойчивый ориентир по медиане 3–6 схожих сделок.
        # Диапазон широкий: сходство имени не делает сделки эквивалентными.
        median = statistics.median(x.price_ton for x in sales)
        low = max(1, round(median * 0.5))
        high = max(low + 1, round(median * 1.5))
    return Valuation(
        username=username,
        score=username_score(username),
        low_ton=low,
        high_ton=high,
        sales=tuple(sales),
        auctions=tuple(comparable_auctions),
        checked_at=datetime.now(timezone.utc),
        data_ok=data_ok,
    )


async def _fetch_fragment(
    session: aiohttp.ClientSession, prefix: str, kind: str
) -> list[FragmentSale]:
    params = {"type": "usernames", "query": prefix, "filter": kind, "sort": "price"}
    url = f"{FRAGMENT}/?{urlencode(params)}"
    async with session.get(url, allow_redirects=True) as response:
        response.raise_for_status()
        # Ограничение отклика, чтобы бот не обрабатывал огромные страницы.
        body = await response.read()
        if len(body) > 1_500_000:
            raise ValueError("Fragment response exceeds size limit")
        return parse_fragment_listings(body.decode("utf-8", errors="replace"), kind)


async def evaluate_username(username: str, session: aiohttp.ClientSession) -> Valuation:
    # Два поиска: сначала узкий префикс, затем более широкий при недостатке продаж.
    prefixes = list(dict.fromkeys([username[:min(5, len(username))], username[:3]]))
    sold: dict[str, FragmentSale] = {}
    auctions: dict[str, FragmentSale] = {}
    data_ok = False
    for prefix in prefixes:
        try:
            sold_rows = await _fetch_fragment(session, prefix, "sold")
            data_ok = True
            sold.update((entry.username, entry) for entry in sold_rows)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            continue
        # Завершённые сделки важнее ставок; не запрашиваем торги,
        # если уже удалось собрать достаточную выборку.
        if len(rank_similar(username, list(sold.values()))) >= 3:
            break
        try:
            auction_rows = await _fetch_fragment(session, prefix, "auction")
            auctions.update((entry.username, entry) for entry in auction_rows)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass
    return value_from_listings(username, list(sold.values()), list(auctions.values()), data_ok)
