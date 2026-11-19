from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

import aiohttp

from words import WORDS_5, WORDS_6

LETTERS = "abcdefghijklmnopqrstuvwxyz"
VOWELS = "aeiouy"
CONSONANTS = "bcdfghjklmnpqrstvwxz"
# Узнаваемые IT/RE-основы; популярные целые слова (hacker, coder и т. п.)
# не выдаём как готовые никнеймы: они почти наверняка заняты.
# Только осмысленные названия из dev / RE / системного программирования.
# Никаких выдуманных слогов вроде -ivo / -vora / -drift.
# Каждая пара проверена по смыслу; не перемножаем все слова подряд.
TECH_SHORT_PAIRS = {
    "hex": "map ptr buf asm cmp xor log ref jmp key dll",
    "mem": "map ptr buf hex cmp log ref key dll asm ops",
    "bin": "map ptr buf hex cmp log ref api dll asm",
    "asm": "map ptr buf hex cmp log ref jmp dll",
    "dll": "map ptr buf hex log ref api dbg",
    "sys": "map ptr buf hex log ref api dbg",
    "net": "map ptr buf log api dns tcp udp",
    "bit": "map hex xor log key ops asm",
    "x64": "map ptr buf hex cmp xor log ref api dll asm",
    "x86": "map ptr buf hex cmp xor log ref api dll asm",
    "x32": "map ptr buf hex cmp xor log ref api dll asm",
    "cpu": "ptr hex log reg asm",
    "git": "log ref obj",
    "api": "log key ptr map ref",
    "xor": "ptr key buf hex ref cmp",
    "jmp": "ptr hex ref buf",
    "dbg": "map ptr log api",
    "reg": "map ptr log ref key",
    "ptr": "map buf hex ref asm",
}

TECH_LONG_PAIRS = {
    "hex": "dump scan trace patch hook probe watch view editor parser mask shift bytes stack cache check shell decode encode",
    "mem": "dump scan trace patch hook probe watch map read write alloc leak fault stack cache guard break",
    "dll": "hook patch scan dump trace probe watch proxy load loader entry import export map guard inject",
    "bin": "dump scan trace patch hook probe read write parse parser view cache stack check",
    "asm": "hook patch trace probe watch scan dump view cache shift stack parse jit",
    "sys": "hook patch trace probe watch scan dump guard cache stack loader calls",
    "byte": "dump scan trace patch hook probe read write parse shift mask swap ops map",
    "code": "scan trace patch hook probe read write parse cache stack map dump",
    "net": "scan trace probe watch guard filter packet cache hook dump",
    "bit": "mask shift flip scan trace patch swap write read ops hook",
    "ptr": "scan trace hook dump parse cache watch guard check map",
    "root": "trace patch scan dump probe guard watch shell access",
    "heap": "map scan dump trace patch hook probe watch cache alloc",
    "stack": "map scan dump trace hook probe watch cache frame ptr",
    "hash": "scan dump trace probe cache list map check",
    "cache": "hit miss line map scan trace flush load",
    "reg": "scan dump trace patch watch read write hook key",
    "dbg": "scan trace probe watch patch hook stack dump log",
    "api": "hook probe scan trace patch guard call dump",
    "ida": "hook scan trace patch dump probe view core",
    "x64": "hook patch probe trace debug stack cache shell scan dump loader",
    "x86": "hook patch probe trace debug stack cache shell scan dump loader",
    "x32": "hook patch probe trace debug stack cache shell scan dump loader",
    "git": "fetch patch merge reset diff clone commit branch",
    "xor": "bytes mask patch hook trace scan dump key",
    "jmp": "trace patch hook chain stack addr back",
    "proxy": "scan trace cache probe guard hook check",
    "kernel": "dump map trace hook patch ptr log",
    "opcode": "dump scan map trace hex",
    "packet": "dump scan trace log buf",
    "thread": "map scan dump trace log",
    "debug": "log dump trace hook scan map",
    "shell": "code scan trace hook",
    "tcp": "dump scan trace proxy probe",
    "udp": "dump scan trace proxy probe",
}

TECH_STANDALONE = (
    "bitmask", "bitwise", "bitshift", "bitflip", "hexdump", "memdump",
    "syscall", "keyscan", "stackptr", "voidptr", "nullref", "nullptr",
    "xorbytes", "codepage", "debuglog", "opcache", "jmptrace",
    "cachehit", "cachemiss", "segfault", "stackbuf", "tcpdump",
    "udpscan", "iproute", "winapi", "asmjit", "hexedit",
)

TECH_OBVIOUS_NAMES = {
    "hacker", "cracker", "coder", "developer", "rootkit", "nullbyte",
    "bytecode", "devops", "bitrix", "x64bits", "x32bits", "x32dll",
    "x64dll", "idahacker", "dllcracker", "darkweb", "opensource",
}

def _tech_pool() -> set[str]:
    names = set(TECH_STANDALONE)
    for pairs in (TECH_SHORT_PAIRS, TECH_LONG_PAIRS):
        for prefix, suffixes in pairs.items():
            names.update(prefix + suffix for suffix in suffixes.split())
    return {
        name for name in names
        if 5 <= len(name) <= 10
        and re.fullmatch(r"[a-z][a-z0-9_]+", name)
        and name not in TECH_OBVIOUS_NAMES
    }

@dataclass(slots=True)
class Candidate:
    username: str
    likely_free: bool
    checked: bool = True


def _pseudo_word(length: int) -> str:
    # Несколько схем дают разные формы, а не одно и то же окончание.
    schemes = {
        5: ("cvcvc", "cvccv", "vccvc", "ccvcv"),
        6: ("cvccvc", "cvcvcv", "vccvcv", "ccvcvc", "cvcvcc"),
    }
    scheme = random.choice(schemes[length])
    return "".join(random.choice(CONSONANTS if ch == "c" else VOWELS) for ch in scheme)


def _matches_filter(word: str, raw_filter: str) -> bool:
    raw_filter = raw_filter.strip().lower()
    if not raw_filter or raw_filter == "-":
        return True
    parts = raw_filter.split()
    excluded = ""
    required = ""
    mask = ""
    for part in parts:
        if part.startswith("-"):
            excluded += re.sub("[^a-z]", "", part[1:])
        elif "?" in part or (part.isalpha() and len(part) == len(word)):
            mask = part
        else:
            required += re.sub("[^a-z]", "", part.replace(",", ""))
    if excluded and any(ch in word for ch in excluded):
        return False
    if required and not any(ch in word for ch in required):
        return False
    if mask:
        if len(mask) != len(word):
            return False
        pattern = "^" + re.escape(mask).replace(r"\?", "[a-z]") + "$"
        return bool(re.match(pattern, word))
    return True


def build_candidates(length: int, raw_filter: str, amount: int = 24) -> list[str]:
    source = list(WORDS_5 if length == 5 else WORDS_6)
    random.shuffle(source)
    dictionary_words = [
        word for word in source if len(word) == length and _matches_filter(word, raw_filter)
    ]
    # Обычные словарные слова почти всегда заняты. Сначала генерируем редкие,
    # произносимые формы; словарные варианты добавляем лишь для разнообразия.
    pool: list[str] = []
    seen: set[str] = set()
    attempts = 0
    while len(pool) < amount * 4 and attempts < 5000:
        attempts += 1
        word = _pseudo_word(length)
        if word not in seen and _matches_filter(word, raw_filter):
            seen.add(word)
            pool.append(word)
    # Примерно каждый пятый вариант — настоящее слово, остальные редкие формы.
    result: list[str] = []
    while pool and len(result) < amount:
        if dictionary_words and len(result) % 5 == 4:
            word = dictionary_words.pop()
            if word not in seen:
                result.append(word)
                seen.add(word)
        else:
            result.append(pool.pop())
    return result


def build_tech_candidates(min_length: int, max_length: int, amount: int = 30) -> list[str]:
    """Только вручную отобранные сочетания с реальным IT/RE-смыслом."""
    if amount <= 0 or min_length > max_length:
        return []
    values = [name for name in _tech_pool() if min_length <= len(name) <= max_length]
    random.shuffle(values)
    return values[:amount]


# Публичная проверка не требует телефона, MTProto и авторизации Fragment.
# Отсутствие публичной страницы НЕ гарантирует возможность регистрации:
# Telegram может удерживать имя, резервировать его или продавать на Fragment.
LOGGER = logging.getLogger(__name__)
MAX_PUBLIC_CONCURRENCY = 8
PUBLIC_CACHE_SECONDS = 120
PUBLIC_TIMEOUT_SECONDS = 12


def classify_tme_response(name: str, status: int, url: str, html: str) -> Candidate:
    """Консервативная проверка публичной страницы t.me.

    True означает только "публичный профиль не найден" — не свободен гарантированно.
    При ошибке и нестандартной странице checked=False: не выдаём ложное имя.
    """
    if status != 200:
        return Candidate(name, False, checked=False)
    lower_url = url.lower().rstrip("/")
    if lower_url in {"https://telegram.org", "http://telegram.org"}:
        return Candidate(name, True)
    if lower_url != f"https://t.me/{name.lower()}":
        return Candidate(name, False, checked=False)
    if "tgme_page_title" in html:
        # Есть публичный профиль/канал: имя занято.
        return Candidate(name, False)
    # Telegram часто отдаёт страницу-заглушку «Contact @имя» без профиля.
    # Это лишь потенциальная возможность: не гарантия регистрации.
    if (
        f"<title>Telegram: Contact @{name}</title>" in html
        and "tgme_page_icon" in html
        and f"tg://resolve?domain={name}" in html
        and "tgme_page_title" not in html
    ):
        return Candidate(name, True)
    if "<title>Telegram Messenger</title>" in html and "tgme_page_title" not in html:
        return Candidate(name, True)
    return Candidate(name, False, checked=False)


class PublicUsernameChecker:
    """Один HTTP-клиент на бота, общий сетевой семафор и короткий кэш."""

    def __init__(
        self, session: aiohttp.ClientSession, concurrency: int = MAX_PUBLIC_CONCURRENCY
    ) -> None:
        self.session = session
        self._slots = asyncio.Semaphore(concurrency)
        self._cache: dict[str, tuple[float, Candidate]] = {}

    async def check(self, username: str) -> Candidate:
        name = username.lower().lstrip("@")
        cached = self._cache.get(name)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            async with self._slots:
                timeout = aiohttp.ClientTimeout(total=PUBLIC_TIMEOUT_SECONDS)
                async with self.session.get(
                    f"https://t.me/{name}", timeout=timeout, allow_redirects=True
                ) as response:
                    # Не скачиваем бесконечные страницы.
                    body = await response.read()
                    if len(body) > 100_000:
                        return Candidate(name, False, checked=False)
                    candidate = classify_tme_response(
                        name, response.status, str(response.url),
                        body.decode("utf-8", errors="replace"),
                    )
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as error:
            LOGGER.warning("Public t.me check failed for @%s: %s", name, error)
            return Candidate(name, False, checked=False)
        if candidate.checked:
            if len(self._cache) >= 4000:
                self._cache.clear()
            self._cache[name] = (time.monotonic() + PUBLIC_CACHE_SECONDS, candidate)
        else:
            LOGGER.warning("Public t.me response inconclusive for @%s", name)
        return candidate


async def _check_exact(checker: PublicUsernameChecker, username: str) -> Candidate:
    return await checker.check(username)


async def check_candidates(
    usernames: list[str],
    checker: PublicUsernameChecker,
    limit: int = 5,
    progress: Callable[[int, int, int, str, bool], Awaitable[None]] | None = None,
) -> list[Candidate]:
    results: list[Candidate] = []
    total = len(usernames)
    for checked, username in enumerate(usernames, start=1):
        result = await _check_exact(checker, username)
        if result.checked and result.likely_free:
            results.append(result)
        # Отчитываемся о каждой попытке, включая сетевые ошибки, иначе
        # интерфейс останавливается на 3/12 после фактического завершения.
        if progress is not None:
            await progress(checked, total, len(results), username, result.checked)
        if len(results) >= limit:
            break
        # Бережный темп к публичному t.me. Параллельные пользователи не блокируются.
        await asyncio.sleep(0.2)
    return results
