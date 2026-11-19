from __future__ import annotations

import asyncio
import logging
import os
import re
from logging.handlers import RotatingFileHandler
from decimal import Decimal, InvalidOperation
from pathlib import Path

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    CopyTextButton,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from cards import render_username_card, render_valuation_card, username_value
from crypto_pay import CryptoPayError, create_invoice, get_invoice, test_token
from database import (
    activate_crypto_invoice,
    ensure_user,
    get_crypto_invoice,
    get_seen_usernames,
    mark_username_seen,
    FREE_DAILY_SEARCHES,
    SEARCH_COOLDOWN_SECONDS,
    get_search_access,
    reserve_search,
    finish_search,
    release_search_on_failure,
    grant_subscription,
    create_promo_code,
    redeem_promo_code,
    list_promo_codes,
    has_subscription,
    has_ultra_subscription,
    ultra_until,
    init_db,
    is_admin,
    grant_admin,
    revoke_admin,
    list_admins,
    save_crypto_invoice,
    subscription_until,
)
from search import PublicUsernameChecker, build_candidates, build_tech_candidates, check_candidates
from valuation import evaluate_username, normalize_username, value_from_listings

ROOT = Path(__file__).resolve().parent
SUPPORT_USERNAME = "idapatch"
CHANNEL_USERNAME = "@UzSearcher"
CHANNEL_URL = "https://t.me/UzSearcher"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = 8260244722
SUBSCRIPTION_PLANS = {
    3: 19,
    7: 38,
    14: 75,
}
CRYPTO_PRICES = {3: "0.25", 7: "0.50", 14: "1.00"}
ULTRA_CRYPTO_PRICES = {3: "4.00", 7: "7.00", 14: "15.00"}
ADMIN_SUB_DAYS = (3, 7, 14, 30, 90, 365)
DEFAULT_SEARCH_COUNT = 2
FREE_MAX_SEARCH_COUNT = 2
MAX_SEARCH_COUNT = 15
router = Router()
public_checker: PublicUsernameChecker | None = None
crypto_pay_token = ""
active_searches: set[int] = set()
LOGGER = logging.getLogger(__name__)


def subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subscription")],
    ])


async def is_channel_member(bot: Bot, user_id: int) -> bool:
    member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
    status = getattr(member.status, "value", member.status)
    return status not in {"left", "kicked"}


async def send_subscription_gate(event: Message | CallbackQuery) -> None:
    text = (
        "🔐 <b>ДОСТУП К UZSEARCH</b>\n\n"
        "Чтобы войти в бота и пользоваться поиском, подпишитесь на наш канал "
        "<b>@UzSearcher</b>. Там публикуются обновления, новые фильтры и подборки юзернеймов.\n\n"
        "После подписки вернитесь сюда и нажмите «Проверить подписку»."
    )
    try:
        if isinstance(event, CallbackQuery):
            await event.answer("Сначала подпишитесь на канал", show_alert=True)
            await event.message.answer(text, reply_markup=subscription_keyboard())
        else:
            await event.answer(text, reply_markup=subscription_keyboard())
    except TelegramForbiddenError:
        LOGGER.info("Cannot show subscription gate: user blocked bot")


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if isinstance(event, CallbackQuery) and event.data == "check_subscription":
            return await handler(event, data)
        if isinstance(event, Message):
            # Платёж должен примениться, даже если пользователь покинул канал после выставления счёта.
            if event.successful_payment or (event.text or "").startswith("/grant"):
                return await handler(event, data)
        try:
            subscribed = await is_channel_member(data["bot"], user.id)
        except Exception:
            LOGGER.exception("Subscription check failed user_id=%s", user.id)
            if isinstance(event, CallbackQuery):
                await event.answer("Проверка подписки временно недоступна", show_alert=True)
            else:
                await event.answer("Проверка подписки временно недоступна. Напишите в поддержку.")
            return None
        if not subscribed:
            await send_subscription_gate(event)
            return None
        return await handler(event, data)


def configure_logging() -> None:
    """Логи на диск с ротацией; никаких токенов и сырых HTTP-заголовков."""
    directory = ROOT / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    handlers: list[logging.Handler] = [
        RotatingFileHandler(directory / "bot.log", maxBytes=2_000_000,
                            backupCount=3, encoding="utf-8"),
        RotatingFileHandler(directory / "errors.log", maxBytes=2_000_000,
                            backupCount=3, encoding="utf-8"),
        logging.StreamHandler(),
    ]
    handlers[1].setLevel(logging.ERROR)
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


async def safe_edit(message: Message, text: str, **kwargs) -> None:
    """TelegramBadRequest для неизменённого текста — нормальный no-op."""
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as error:
        if "message is not modified" in str(error).lower():
            LOGGER.debug("Skipping unchanged message")
            return
        raise


@router.errors()
async def log_unhandled_error(event: ErrorEvent) -> bool:
    """Сохраняем полный traceback, пользователю показываем только короткое сообщение."""
    error = event.exception
    LOGGER.error(
        "Unhandled update id=%s: %s", event.update.update_id, type(error).__name__,
        exc_info=(type(error), error, error.__traceback__),
    )
    try:
        if event.update.callback_query is not None:
            await event.update.callback_query.answer(
                "⚠️ Ошибка бота. Она записана в журнал. Попробуйте позже.",
                show_alert=True,
            )
        elif event.update.message is not None:
            await event.update.message.answer(
                "⚠️ Произошла ошибка. Подробности записаны в журнал."
            )
    except Exception:
        LOGGER.exception("Failed to notify user about unexpected bot error")
    return True


class SearchState(StatesGroup):
    choosing_length = State()
    entering_filter = State()
    ready = State()


class AdminState(StatesGroup):
    entering_subscription_user = State()
    choosing_subscription_days = State()
    entering_admin_user = State()
    removing_admin_user = State()
    entering_promo_code = State()
    choosing_promo_tier = State()
    entering_promo_days = State()
    entering_promo_uses = State()


class ValuationState(StatesGroup):
    entering_username = State()


class PromoState(StatesGroup):
    entering_code = State()


def has_ultra_access(user_id: int) -> bool:
    return is_admin(user_id, ADMIN_ID) or has_ultra_subscription(user_id)


def has_premium_access(user_id: int) -> bool:
    return has_ultra_access(user_id) or has_subscription(user_id)


def parse_user_id(value: str | None) -> int | None:
    value = (value or "").strip()
    if not value.isascii() or not value.isdigit():
        return None
    user_id = int(value)
    return user_id if 0 < user_id < 10**16 else None


def main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 Поиск", callback_data="search")
    builder.button(text="👤 Профиль", callback_data="profile")
    builder.button(text="💰 Оценить юзернейм", callback_data="evaluate")
    builder.button(text="🎟 Промокод", callback_data="promo")
    builder.button(text="💎 Купить подписку", callback_data="buy")
    builder.button(text="🛟 Техподдержка", url=f"https://t.me/{SUPPORT_USERNAME}")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()



def profile_keyboard(user_id: int, active: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_admin(user_id, ADMIN_ID):
        builder.button(text="🎁 Выдать подписку", callback_data="admin:sub")
        builder.button(text="🎟 Создать промокод", callback_data="admin:promo")
        builder.button(text="📋 Промокоды", callback_data="admin:promos")
        if user_id == ADMIN_ID:
            builder.button(text="🛡 Выдать админа", callback_data="admin:add")
            builder.button(text="🚫 Забрать админа", callback_data="admin:remove")
            builder.button(text="👥 Список админов", callback_data="admin:list")
    if not has_ultra_access(user_id):
        builder.button(text="👑 Купить Ultra", callback_data="buy")
    if not active and not has_ultra_access(user_id):
        builder.button(text="💎 Купить Premium", callback_data="buy")
    builder.button(text="↩️ Главное меню", callback_data="home")
    builder.adjust(1)
    return builder.as_markup()


def admin_days_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for days in ADMIN_SUB_DAYS:
        builder.button(text=f"💎 {days} дн.", callback_data=f"admin:days:{days}")
    builder.button(text="❌ Отмена", callback_data="admin:cancel")
    builder.adjust(3, 3, 1)
    return builder.as_markup()


def admin_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")
    ]])


def promo_tier_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Premium", callback_data="admin:promo:tier:premium")],
        [InlineKeyboardButton(text="👑 Ultra", callback_data="admin:promo:tier:ultra")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin:cancel")],
    ])


def buy_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ 3 дня — 19 Stars", callback_data="plan:3")
    builder.button(text="⭐ 7 дней — 38 Stars", callback_data="plan:7")
    builder.button(text="⭐ 14 дней — 75 Stars", callback_data="plan:14")
    builder.button(text="₿ 3 дня — $0.25", callback_data="crypto:3")
    builder.button(text="₿ 7 дней — $0.50", callback_data="crypto:7")
    builder.button(text="₿ 14 дней — $1", callback_data="crypto:14")
    builder.button(text="👑 Ultra · 3 дня — $4", callback_data="ultra:3")
    builder.button(text="👑 Ultra · 7 дней — $7", callback_data="ultra:7")
    builder.button(text="👑 Ultra · 14 дней — $15", callback_data="ultra:14")
    builder.button(text="↩️ Назад", callback_data="home")
    builder.adjust(1)
    return builder.as_markup()


def selected_search_count(data: dict) -> int:
    """Количество свободных имён, которые пользователь хочет получить (1–15)."""
    try:
        count = int(data.get("count", DEFAULT_SEARCH_COUNT))
    except (TypeError, ValueError):
        return DEFAULT_SEARCH_COUNT
    return count if 1 <= count <= MAX_SEARCH_COUNT else DEFAULT_SEARCH_COUNT


def search_keyboard(count: int = DEFAULT_SEARCH_COUNT) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="5 букв", callback_data="length:5")
    builder.button(text="6 букв", callback_data="length:6")
    builder.button(text="🎛 Фильтр", callback_data="filter")
    builder.button(text="💻 Tech / Hacker", callback_data="tech_menu")
    builder.button(text=f"🔢 Количество: {count}", callback_data="count_menu")
    builder.button(text="🚀 Начать поиск", callback_data="run")
    builder.button(text="↩️ Назад", callback_data="home")
    builder.adjust(2, 2, 1, 1, 1)
    return builder.as_markup()


def count_keyboard(selected: int, premium: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    maximum = MAX_SEARCH_COUNT if premium else FREE_MAX_SEARCH_COUNT
    for count in range(1, maximum + 1):
        mark = " ✅" if count == selected else ""
        builder.button(text=f"{count}{mark}", callback_data=f"count:{count}")
    builder.button(text="↩️ Назад к поиску", callback_data="count_back")
    builder.adjust(*(5 for _ in range((maximum + 4) // 5)), 1)
    return builder.as_markup()


def tech_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💻 Короткие · 6 букв · бесплатно", callback_data="tech:short")],
        [InlineKeyboardButton(text="💎 Расширенные · 7–10 · Premium", callback_data="tech:wide")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="search")],
    ])


def fresh_search_candidates(
    user_id: int,
    mode: str,
    length: int,
    raw_filter: str,
    tech_min: int = 6,
    tech_max: int = 6,
    limit: int = 12,
) -> list[str]:
    """Не повторять проверенные кандидаты для одного Telegram ID."""
    already_seen = get_seen_usernames(user_id)
    if mode == "tech":
        # Tech-словарь конечный: перебираем весь, чтобы при остатке
        # из нескольких новых имён не выбирать старые случайно.
        values = build_tech_candidates(tech_min, tech_max, amount=10000)
        return [name for name in values if name not in already_seen][:limit]

    chosen: list[str] = []
    reserved = set(already_seen)
    # Обычные псевдослова генерируются практически без ограничения,
    # однако при узком фильтре новые комбинации могут закончиться.
    for _ in range(8):
        for name in build_candidates(length, raw_filter, amount=max(48, limit * 4)):
            if name not in reserved:
                reserved.add(name)
                chosen.append(name)
                if len(chosen) >= limit:
                    return chosen
    return chosen


async def show_home(message: Message, state: FSMContext) -> None:
    await state.clear()
    caption = (
        "🔎 <b>USERNAME SEARCH — главное меню</b>\n\n"
        "6 букв — бесплатно. Поиск 5-буквенных имён доступен с подпиской.\n"
        "Выберите раздел ↓"
    )
    # Keep the deployment asset beside the entry point so web upload hosts
    # do not need to preserve a nested directory structure.
    banner = ROOT / "menu.png"
    if banner.exists():
        await message.answer_photo(FSInputFile(banner), caption=caption, reply_markup=main_keyboard())
    else:
        await message.answer(caption, reply_markup=main_keyboard())


@router.message(CommandStart())
async def start(message: Message, state: FSMContext) -> None:
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await show_home(message, state)


@router.callback_query(F.data == "check_subscription")
async def check_subscription(callback: CallbackQuery, state: FSMContext) -> None:
    try:
        subscribed = await is_channel_member(callback.bot, callback.from_user.id)
    except Exception:
        LOGGER.exception("Manual subscription check failed user_id=%s", callback.from_user.id)
        await callback.answer(
            "Не удалось проверить подписку. Убедитесь, что бот добавлен администратором канала.",
            show_alert=True,
        )
        return
    if not subscribed:
        await callback.answer("Подписка пока не найдена. Подпишитесь и попробуйте снова.", show_alert=True)
        return
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    await callback.answer("✅ Подписка подтверждена")
    await show_home(callback.message, state)


@router.callback_query(F.data == "promo")
async def promo_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(PromoState.entering_code)
    await callback.answer()
    await callback.message.answer(
        "🎟 <b>АКТИВАЦИЯ ПРОМОКОДА</b>\n\n"
        "Отправьте промокод одним сообщением. Регистр букв не важен.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Отмена", callback_data="home")
        ]]),
    )


@router.message(PromoState.entering_code)
async def promo_redeem(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer("Промокод должен содержать 3–32 символа: латинские буквы, цифры, <code>_</code> или <code>-</code>.")
        return
    ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    result = redeem_promo_code(message.from_user.id, code)
    if not result["ok"]:
        texts = {
            "not_found": "❌ Промокод не найден.",
            "inactive": "❌ Этот промокод отключён.",
            "exhausted": "❌ У промокода закончились активации.",
            "already_used": "ℹ️ Вы уже активировали этот промокод.",
            "unknown_user": "❌ Не удалось найти ваш профиль. Отправьте /start и повторите.",
        }
        await message.answer(texts.get(result["reason"], "❌ Не удалось активировать промокод."))
        return
    await state.clear()
    tier_name = "Ultra" if result["tier"] == "ultra" else "Premium"
    await message.answer(
        "✅ <b>ПРОМОКОД АКТИВИРОВАН</b>\n\n"
        f"Тариф: <b>{tier_name}</b>\n"
        f"Срок: <b>{result['days']} дней</b>\n"
        f"Действует до: <b>{result['until'].astimezone().strftime('%d.%m.%Y %H:%M')}</b>",
        reply_markup=main_keyboard(),
    )


@router.callback_query(F.data == "evaluate")
async def evaluate_start(callback: CallbackQuery, state: FSMContext) -> None:
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    await state.clear()
    await state.set_state(ValuationState.entering_username)
    await callback.answer()
    await callback.message.answer(
        "💰 <b>ОЦЕНКА ЮЗЕРНЕЙМА</b>\n\n"
        "Напишите свой Telegram-юзернейм, например <code>@hexora</code>.\n\n"
        "Сравню с похожими завершёнными продажами на Fragment, "
        "оценю качество имени и пришлю PNG-карточку.\n\n"
        "Цена ориентировочная, не гарантия продажи. "
        "Для оценки права продажи нужно проверить сам юзернейм на Fragment.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Отмена", callback_data="home")
        ]]),
    )


@router.message(ValuationState.entering_username)
async def evaluate_input(message: Message, state: FSMContext) -> None:
    if (message.text or "").strip().lower() == "/cancel":
        await show_home(message, state)
        return
    username = normalize_username(message.text or "")
    if not username:
        await message.answer(
            "❌ Некорректный юзернейм. Отправьте <code>@username</code> "
            "(от 5 до 32 символов: латиница, цифры и _; начинается с буквы). "
            "Или нажмите /cancel."
        )
        return

    await state.clear()
    status = await message.answer("⏳ Сравниваю похожие продажи на Fragment и готовлю карточку…")
    try:
        async with aiohttp.ClientSession(
            headers={"User-Agent": "Mozilla/5.0 (compatible; UsernameSearchBot/1.0)"},
            timeout=aiohttp.ClientTimeout(total=15, connect=5),
        ) as session:
            valuation = await evaluate_username(username, session)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
        logging.warning("Fragment недоступен для @%s: %s", username, error)
        valuation = value_from_listings(username, [], [], data_ok=False)

    try:
        png = await asyncio.to_thread(render_valuation_card, valuation)
    except Exception as error:
        logging.exception("Ошибка генерации карточки оценки: %s", error)
        await safe_edit(status, "Не удалось создать карточку. Попробуйте позже.")
        return

    if valuation.has_price:
        price_text = (
            f"💰 Ориентир: <b>{valuation.low_ton:,}–{valuation.high_ton:,} TON</b>\n"
            f"По <b>{len(valuation.sales)}</b> похожим завершённым продажам.\n"
        ).replace(",", " ")
    elif valuation.data_ok:
        price_text = (
            "💰 Ориентир по стоимости: <b>недостаточно данных</b>\n"
            f"Похожих продаж: <b>{len(valuation.sales)}</b> (нужно не менее 3).\n"
        )
    else:
        price_text = "💰 Цена: <b>не рассчитана — Fragment недоступен</b>\n"

    caption = (
        f"🔎 <b>ОЦЕНКА @{username}</b>\n\n"
        f"{price_text}"
        f"✨ Алгоритмическая ценность: <b>{valuation.score}/100</b>\n\n"
        "Рыночный ориентир основан на исторических ценах других имён, "
        "не является оценкой текущей заявки и не гарантирует продажу."
    )
    keyboard = [
        [InlineKeyboardButton(text="🌐 Посмотреть на Fragment",
                              url=f"https://fragment.com/username/{username}")],
    ]
    for sale in valuation.sales[:3]:
        keyboard.append([
            InlineKeyboardButton(
                text=f"📊 @{sale.username} · {sale.price_ton:g} TON (продано)",
                url=sale.url,
            )
        ])
    keyboard.append([InlineKeyboardButton(text="🔄 Оценить другой", callback_data="evaluate")])
    await message.answer_photo(
        BufferedInputFile(png, filename=f"value_{username}.png"),
        caption=caption,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
    )
    try:
        await status.delete()
    except Exception:
        pass


@router.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery) -> None:
    user = callback.from_user
    ensure_user(user.id, user.username, user.first_name)
    until = subscription_until(user.id)
    active = has_subscription(user.id)
    admin = is_admin(user.id, ADMIN_ID)
    role = "👑 Владелец" if user.id == ADMIN_ID else ("🛡 Администратор" if admin else "Пользователь")
    status = f"активна до {until.astimezone().strftime('%d.%m.%Y %H:%M')}" if active and until else "не активна"
    premium_access = has_premium_access(user.id)
    ultra_access = has_ultra_access(user.id)
    ultra_expiry = ultra_until(user.id)
    ultra_status = (
        f"до {ultra_expiry.astimezone().strftime('%d.%m.%Y %H:%M')}"
        if has_ultra_subscription(user.id) and ultra_expiry else "не активна"
    )
    premium = "✅ Доступен" if premium_access else "🔒 Нет"
    quota = get_search_access(user.id, premium=premium_access, ultra=ultra_access)
    if ultra_access:
        search_info = "∞ (Ultra)"
    elif premium_access:
        search_info = "∞ (Premium)"
    else:
        search_info = (
            f"{max(0, FREE_DAILY_SEARCHES - quota.used_today)}/{FREE_DAILY_SEARCHES} "
            "на сегодня"
        )
    cooldown_info = (
        f"⏳ Следующий поиск через {quota.wait_seconds} сек.\n"
        if quota.reason == "cooldown" else ""
    )
    await callback.answer()
    await callback.message.answer(
        "👤 <b>ВАШ ПРОФИЛЬ</b>\n\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Роль: <b>{role}</b>\n"
        f"💎 Premium: <b>{status}</b>\n"
        f"👑 Ultra: <b>{ultra_status}</b>\n"
        f"🔑 Premium-доступ: <b>{premium}</b>\n"
        f"🔎 Осталось поисков: <b>{search_info}</b>\n"
        f"{cooldown_info}\n"
        "🆓 Бесплатно: 1–2 имени за запуск, 3 поиска в сутки.\n"
        "💎 Premium: 1–15 имён, без суточного лимита.\n"
        "👑 Ultra: без лимита запусков и без задержки.\n"
        f"⏳ Free/Premium: пауза {SEARCH_COOLDOWN_SECONDS} сек.",
        reply_markup=profile_keyboard(user.id, active),
    )


@router.callback_query(F.data == "buy")
async def buy(callback: CallbackQuery) -> None:
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    await callback.answer()
    await callback.message.answer(
        "💎 <b>ВЫБЕРИТЕ ПОДПИСКУ</b>\n\n"
        "• 3 дня — 19 Stars\n"
        "• 7 дней — 38 Stars\n"
        "💎 <b>PREMIUM</b>: 3/7/14 дней — 19/38/75 Stars "
        "или $0.25/$0.50/$1 через Crypto Pay.\n"
        "1–15 имён за поиск, без суточной квоты; пауза 60 сек.\n\n"
        "👑 <b>ULTRA — БЕЗ ЛИМИТОВ ПОИСКА</b>\n"
        "• 3 дня — $4\n"
        "• 7 дней — $7\n"
        "• 14 дней — $15\n"
        "Неограниченные запуски без минутной задержки, "
        "1–15 имён за один поиск.\n\n"
        "Ultra оплачивается через Crypto Pay; доступ включается "
        "только после подтверждения платежа.",
        reply_markup=buy_keyboard(),
    )


@router.callback_query(F.data.startswith("plan:"))
async def buy_stars(callback: CallbackQuery) -> None:
    days = int(callback.data.split(":", 1)[1])
    stars = SUBSCRIPTION_PLANS.get(days)
    if stars is None:
        await callback.answer("Неизвестный тариф", show_alert=True)
        return
    await callback.answer()
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Premium Username Search — {days} дней",
        description="Доступ к поиску 5- и 6-буквенных Telegram-юзернеймов.",
        payload=f"premium_{days}_days",
        currency="XTR",
        prices=[LabeledPrice(label=f"Подписка на {days} дней", amount=stars)],
    )


@router.pre_checkout_query()
async def approve_checkout(query: PreCheckoutQuery) -> None:
    valid_payloads = {f"premium_{days}_days" for days in SUBSCRIPTION_PLANS}
    await query.answer(ok=query.invoice_payload in valid_payloads, error_message="Неизвестный платёж")


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if payment and payment.invoice_payload.startswith("premium_"):
        try:
            days = int(payment.invoice_payload.split("_")[1])
        except (ValueError, IndexError):
            return
        if days not in SUBSCRIPTION_PLANS:
            return
        ensure_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        until = grant_subscription(message.from_user.id, days)
        await message.answer(
            "✅ <b>Подписка активирована автоматически!</b>\n\n"
            f"Тариф: <b>{days} дней</b>\n"
            f"Действует до: <b>{until.astimezone().strftime('%d.%m.%Y %H:%M')}</b>"
        )


@router.callback_query(F.data.startswith("crypto:"))
@router.callback_query(F.data.startswith("ultra:"))
async def buy_crypto(callback: CallbackQuery) -> None:
    callback_kind, days_raw = callback.data.split(":", 1)
    tier = "premium" if callback_kind == "crypto" else "ultra"
    days = int(days_raw) if days_raw.isascii() and days_raw.isdigit() else 0
    amount = (ULTRA_CRYPTO_PRICES if tier == "ultra" else CRYPTO_PRICES).get(days)
    if amount is None or not crypto_pay_token:
        await callback.answer("Crypto Pay временно недоступен", show_alert=True)
        return

    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    await callback.answer()
    waiting = await callback.message.answer("⏳ Создаю персональный счёт Crypto Pay…")
    try:
        invoice = await asyncio.wait_for(
            create_invoice(
                crypto_pay_token, callback.from_user.id, days, amount, tier=tier
            ),
            timeout=25,
        )
        if not isinstance(invoice, dict):
            raise ValueError("Crypto Pay returned invalid invoice")
        invoice_id = int(invoice["invoice_id"])
        if invoice_id <= 0:
            raise ValueError("Crypto Pay returned invalid invoice ID")
        pay_url = (
            invoice.get("bot_invoice_url")
            or invoice.get("mini_app_invoice_url")
            or invoice.get("pay_url")
        )
        if not isinstance(pay_url, str) or not pay_url.startswith("https://"):
            raise ValueError("Crypto Pay returned no valid payment link")
        save_crypto_invoice(invoice_id, callback.from_user.id, days, amount, tier=tier)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💵 Оплатить ${amount}", url=pay_url)],
            [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_crypto:{invoice_id}")],
            [InlineKeyboardButton(text="↩️ К тарифам", callback_data="buy")],
        ])
        await safe_edit(
            waiting,
            "₿ <b>ОПЛАТА ЧЕРЕЗ CRYPTO PAY</b>\n\n"
            f"Тариф: <b>{tier.title()}, {days} дней</b>\n"
            f"Сумма: <b>${amount}</b>\n\n"
            "1. Нажмите «Оплатить» и завершите платёж.\n"
            "2. Вернитесь сюда и нажмите «Я оплатил».\n"
            "3. Подписка активируется после подтверждения оплаты.",
            reply_markup=keyboard,
        )
        LOGGER.info(
            "Crypto invoice created user_id=%s tier=%s days=%d invoice_id=%d",
            callback.from_user.id, tier, days, invoice_id,
        )
    except Exception as error:
        # Любая ошибка создания/отображения счёта должна убрать индикатор ожидания.
        LOGGER.exception(
            "Crypto invoice creation failed user_id=%s tier=%s days=%d error=%s",
            callback.from_user.id, tier, days, type(error).__name__,
        )
        try:
            await safe_edit(
                waiting,
                "❌ Не удалось создать счёт Crypto Pay.\n"
                "Проверьте подключение и попробуйте ещё раз.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="↩️ К тарифам", callback_data="buy"),
                ]]),
            )
        except Exception:
            LOGGER.exception("Failed to display Crypto invoice error to user_id=%s",
                             callback.from_user.id)


@router.callback_query(F.data.startswith("check_crypto:"))
async def check_crypto_payment(callback: CallbackQuery) -> None:
    invoice_id = int(callback.data.split(":", 1)[1])
    local_invoice = get_crypto_invoice(invoice_id, callback.from_user.id)
    if not local_invoice:
        await callback.answer("Этот счёт принадлежит другому пользователю", show_alert=True)
        return
    if local_invoice["status"] == "paid":
        await callback.answer("Этот платёж уже был применён", show_alert=True)
        return
    await callback.answer("Проверяю оплату…")
    try:
        invoice = await get_invoice(crypto_pay_token, invoice_id)
    except (CryptoPayError, aiohttp.ClientError, asyncio.TimeoutError):
        await callback.message.answer("Не удалось проверить платёж. Попробуйте через несколько секунд.")
        return
    if not invoice or invoice.get("status") != "paid":
        await callback.message.answer("⏳ Платёж пока не найден. Завершите оплату и нажмите кнопку ещё раз.")
        return
    tier = local_invoice["tier"]
    # Для Ultra обязательна сверка счета с его ID, суммой и тарифом.
    # Статуса paid самого по себе недостаточно для выдачи дорогой подписки.
    if tier == "ultra":
        try:
            valid = (
                int(invoice.get("invoice_id", 0)) == invoice_id
                and invoice.get("payload") == f"ultra:{callback.from_user.id}:{local_invoice['days']}"
                and invoice.get("currency_type") == "fiat"
                and invoice.get("fiat") == "USD"
                and Decimal(str(invoice.get("amount"))) == Decimal(str(local_invoice["amount"]))
            )
        except (ValueError, InvalidOperation, TypeError):
            valid = False
        if not valid:
            logging.warning("Crypto Pay Ultra invoice mismatch, invoice_id=%s", invoice_id)
            await callback.message.answer(
                "⚠️ Данные оплаты не совпадают со счётом. Напишите в техподдержку."
            )
            return
    until = activate_crypto_invoice(invoice_id, callback.from_user.id)
    if until is None:
        await callback.message.answer("Этот платёж уже был применён.")
        return
    await callback.message.answer(
        "✅ <b>Оплата подтверждена!</b>\n\n"
        f"{tier.title()} активирована до <b>{until.astimezone().strftime('%d.%m.%Y %H:%M')}</b>.",
        reply_markup=main_keyboard(),
    )


@router.callback_query(F.data == "admin:sub")
async def admin_subscription_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id, ADMIN_ID):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminState.entering_subscription_user)
    await callback.answer()
    await callback.message.answer(
        "🎁 <b>ВЫДАТЬ ПОДПИСКУ</b>\n\nОтправьте числовой Telegram ID пользователя.",
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminState.entering_subscription_user)
async def admin_subscription_user(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id, ADMIN_ID):
        await state.clear()
        await message.answer("Недостаточно прав.")
        return
    target_id = parse_user_id(message.text)
    if target_id is None:
        await message.answer("Нужен числовой Telegram ID. Попробуйте ещё раз.")
        return
    await state.update_data(admin_target_id=target_id)
    await state.set_state(AdminState.choosing_subscription_days)
    await message.answer(
        f"🎁 Пользователь: <code>{target_id}</code>\nВыберите срок подписки:",
        reply_markup=admin_days_keyboard(),
    )


@router.callback_query(F.data.startswith("admin:days:"))
async def admin_subscription_issue(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id, ADMIN_ID):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if await state.get_state() != AdminState.choosing_subscription_days.state:
        await callback.answer("Выберите пользователя заново", show_alert=True)
        return
    days_str = callback.data.rsplit(":", 1)[-1]
    days = int(days_str) if days_str.isdigit() else 0
    target_id = (await state.get_data()).get("admin_target_id")
    if days not in ADMIN_SUB_DAYS or not isinstance(target_id, int):
        await callback.answer("Некорректные данные", show_alert=True)
        return
    ensure_user(target_id, None, "")
    until = grant_subscription(target_id, days)
    await state.clear()
    await callback.answer("Подписка выдана")
    await callback.message.answer(
        f"✅ Выдана подписка на <b>{days} дней</b> для <code>{target_id}</code>.\n"
        f"Действует до <b>{until.astimezone().strftime('%d.%m.%Y %H:%M')}</b>.",
        reply_markup=profile_keyboard(callback.from_user.id, has_subscription(callback.from_user.id)),
    )
    try:
        await callback.bot.send_message(target_id, f"✅ Вам выдали Premium-подписку на {days} дней.")
    except Exception as error:
        logging.info("Не удалось уведомить %s о подписке: %s", target_id, error)


@router.callback_query(F.data == "admin:promo")
async def admin_promo_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id, ADMIN_ID):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminState.entering_promo_code)
    await callback.answer()
    await callback.message.answer(
        "🎟 <b>НОВЫЙ ПРОМОКОД</b>\n\n"
        "Введите название промокода: 3–32 латинских символа, цифры, <code>_</code> или <code>-</code>.",
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminState.entering_promo_code)
async def admin_promo_code(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id, ADMIN_ID):
        await state.clear()
        return
    code = (message.text or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer("Некорректный код. Пример: <code>WELCOME7</code>")
        return
    await state.update_data(promo_code=code)
    await state.set_state(AdminState.choosing_promo_tier)
    await message.answer("Выберите тариф промокода:", reply_markup=promo_tier_keyboard())


@router.callback_query(F.data.startswith("admin:promo:tier:"))
async def admin_promo_tier(callback: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(callback.from_user.id, ADMIN_ID):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    if await state.get_state() != AdminState.choosing_promo_tier.state:
        await callback.answer("Начните создание заново", show_alert=True)
        return
    tier = callback.data.rsplit(":", 1)[-1]
    if tier not in {"premium", "ultra"}:
        await callback.answer("Некорректный тариф", show_alert=True)
        return
    await state.update_data(promo_tier=tier)
    await state.set_state(AdminState.entering_promo_days)
    await callback.answer()
    await callback.message.answer(
        "Введите срок действия подписки в днях — от <b>1</b> до <b>3650</b>:",
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminState.entering_promo_days)
async def admin_promo_days(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id, ADMIN_ID):
        await state.clear()
        return
    raw = (message.text or "").strip()
    days = int(raw) if raw.isascii() and raw.isdigit() else 0
    if not 1 <= days <= 3650:
        await message.answer("Введите целое число от 1 до 3650.")
        return
    await state.update_data(promo_days=days)
    await state.set_state(AdminState.entering_promo_uses)
    await message.answer(
        "Сколько раз можно активировать промокод? Введите число от <b>1</b> до <b>100000</b>:",
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminState.entering_promo_uses)
async def admin_promo_uses(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id, ADMIN_ID):
        await state.clear()
        return
    raw = (message.text or "").strip()
    max_uses = int(raw) if raw.isascii() and raw.isdigit() else 0
    if not 1 <= max_uses <= 100000:
        await message.answer("Введите целое число от 1 до 100000.")
        return
    data = await state.get_data()
    code = data.get("promo_code")
    tier = data.get("promo_tier")
    days = data.get("promo_days")
    if not isinstance(code, str) or tier not in {"premium", "ultra"} or not isinstance(days, int):
        await state.clear()
        await message.answer("Данные потеряны. Создайте промокод заново.")
        return
    created = create_promo_code(code, tier, days, max_uses, message.from_user.id)
    await state.clear()
    if not created:
        await message.answer("❌ Промокод с таким названием уже существует.")
        return
    tier_name = "Ultra" if tier == "ultra" else "Premium"
    await message.answer(
        "✅ <b>ПРОМОКОД СОЗДАН</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Тариф: <b>{tier_name}</b>\n"
        f"Срок: <b>{days} дней</b>\n"
        f"Активаций: <b>{max_uses}</b>",
        reply_markup=profile_keyboard(message.from_user.id, has_subscription(message.from_user.id)),
    )


@router.callback_query(F.data == "admin:promos")
async def admin_promo_list(callback: CallbackQuery) -> None:
    if not is_admin(callback.from_user.id, ADMIN_ID):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    promos = list_promo_codes()
    await callback.answer()
    if not promos:
        await callback.message.answer("Промокодов пока нет.")
        return
    lines = ["📋 <b>ПОСЛЕДНИЕ ПРОМОКОДЫ</b>", ""]
    for promo in promos:
        tier = "Ultra" if promo["tier"] == "ultra" else "Premium"
        status = "✅" if promo["active"] else "❌"
        lines.append(
            f"{status} <code>{promo['code']}</code> · {tier} {promo['days']} дн. · "
            f"{promo['uses_count']}/{promo['max_uses']}"
        )
    await callback.message.answer(
        "\n".join(lines),
        reply_markup=profile_keyboard(callback.from_user.id, has_subscription(callback.from_user.id)),
    )


@router.callback_query(F.data == "admin:add")
async def admin_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только владелец может назначать админов", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminState.entering_admin_user)
    await callback.answer()
    await callback.message.answer(
        "🛡 <b>ВЫДАТЬ АДМИНА</b>\n\nОтправьте числовой Telegram ID пользователя.",
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminState.entering_admin_user)
async def admin_add_user(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        await message.answer("Недостаточно прав.")
        return
    target_id = parse_user_id(message.text)
    if target_id is None:
        await message.answer("Нужен числовой Telegram ID.")
        return
    ensure_user(target_id, None, "")
    added = grant_admin(target_id, message.from_user.id) if target_id != ADMIN_ID else False
    await state.clear()
    await message.answer(
        f"{'✅ Администратор назначен' if added else 'ℹ️ Уже администратор'}: <code>{target_id}</code>",
        reply_markup=profile_keyboard(message.from_user.id, has_subscription(message.from_user.id)),
    )
    if added:
        try:
            await message.bot.send_message(target_id, "🛡 Вам выданы права администратора бота.")
        except Exception as error:
            logging.info("Не удалось уведомить %s о роли: %s", target_id, error)


@router.callback_query(F.data == "admin:remove")
async def admin_remove_start(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только владелец может снимать админов", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminState.removing_admin_user)
    await callback.answer()
    await callback.message.answer(
        "🚫 <b>ЗАБРАТЬ АДМИНА</b>\n\nОтправьте Telegram ID администратора.",
        reply_markup=admin_cancel_keyboard(),
    )


@router.message(AdminState.removing_admin_user)
async def admin_remove_user(message: Message, state: FSMContext) -> None:
    if message.from_user.id != ADMIN_ID:
        await state.clear()
        await message.answer("Недостаточно прав.")
        return
    target_id = parse_user_id(message.text)
    if target_id is None:
        await message.answer("Нужен числовой Telegram ID.")
        return
    if target_id == ADMIN_ID:
        await message.answer("Нельзя снять права владельца.")
        return
    removed = revoke_admin(target_id)
    await state.clear()
    await message.answer(
        f"{'✅ Права сняты' if removed else 'ℹ️ Не найден среди админов'}: <code>{target_id}</code>",
        reply_markup=profile_keyboard(message.from_user.id, has_subscription(message.from_user.id)),
    )
    if removed:
        try:
            await message.bot.send_message(target_id, "🚫 Права администратора бота отозваны.")
        except Exception as error:
            logging.info("Не удалось уведомить %s о снятии роли: %s", target_id, error)


@router.callback_query(F.data == "admin:list")
async def admin_list(callback: CallbackQuery) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    ids = list_admins()
    lines = [f"👑 Владелец: <code>{ADMIN_ID}</code>"]
    lines.extend(f"🛡 <code>{user_id}</code>" for user_id in ids[:100] if user_id != ADMIN_ID)
    if len(ids) > 100:
        lines.append(f"… и ещё {len(ids) - 100}")
    await callback.answer()
    await callback.message.answer(
        "👥 <b>АДМИНИСТРАТОРЫ</b>\n\n" + "\n".join(lines),
        reply_markup=profile_keyboard(callback.from_user.id, has_subscription(callback.from_user.id)),
    )


@router.callback_query(F.data == "admin:cancel")
async def admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.answer("Отменено")
    await callback.message.answer(
        "Действие отменено.",
        reply_markup=profile_keyboard(callback.from_user.id, has_subscription(callback.from_user.id)),
    )


@router.message(Command("grant"))
async def grant_command(message: Message, command: CommandObject) -> None:
    if not is_admin(message.from_user.id, ADMIN_ID):
        return
    parts = (command.args or "").split()
    user_id = parse_user_id(parts[0]) if parts else None
    if user_id is None:
        await message.answer("Формат: <code>/grant USER_ID 14</code>")
        return
    days = int(parts[1]) if len(parts) > 1 and parts[1].isascii() and parts[1].isdigit() else 14
    if not 1 <= days <= 365:
        await message.answer("Срок подписки должен быть от 1 до 365 дней.")
        return
    ensure_user(user_id, None, "")
    until = grant_subscription(user_id, days)
    await message.answer(f"✅ Подписка для <code>{user_id}</code> активна до {until.astimezone().strftime('%d.%m.%Y %H:%M')}")
    try:
        await message.bot.send_message(user_id, f"✅ Подписка активирована на {days} дней.")
    except Exception as error:
        logging.info("Не удалось уведомить %s о подписке: %s", user_id, error)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await message.answer(
        "Выберите длину, при желании задайте фильтр и нажмите «Начать поиск».\n\n"
        "Примеры фильтров:\n"
        "• <code>?a?er</code> — ? заменяет любую букву\n"
        "• <code>-xq</code> — исключить x и q\n"
        "• <code>a,e -x</code> — нужна a или e, без x"
    )


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await show_home(message, state)


@router.callback_query(F.data == "home")
async def home(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await show_home(callback.message, state)


@router.callback_query(F.data == "search")
async def search_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchState.choosing_length)
    premium = has_premium_access(callback.from_user.id)
    count = 5 if premium else DEFAULT_SEARCH_COUNT
    await state.update_data(length=6, filter="", mode="normal", count=count)
    ultra = has_ultra_access(callback.from_user.id)
    access = get_search_access(callback.from_user.id, premium=premium, ultra=ultra)
    available = max(0, FREE_DAILY_SEARCHES - access.used_today)
    rules = (
        "👑 Ultra: 1–15 имён за поиск, нет суточной квоты и минутной задержки."
        if ultra else
        "💎 Premium: 1–15 имён, без суточного ограничения."
        if premium else
        f"🆓 Бесплатно: 1–2 имени за раз, осталось поисков сегодня: {available}/{FREE_DAILY_SEARCHES}."
    )
    await callback.answer()
    await callback.message.answer(
        "💎 <b>ПОИСК ЮЗЕРНЕЙМА</b>\n\n"
        "Найденные варианты проверяются через Telegram. "
        "Финально проверьте имя перед регистрацией.\n\n"
        f"Сейчас выбрано: <b>6 букв</b>, количество: <b>{count}</b>.\n"
        f"{rules}\n"
        + ("" if ultra else f"⏳ Между поисками — пауза {SEARCH_COOLDOWN_SECONDS} секунд."),
        reply_markup=search_keyboard(count),
    )


@router.callback_query(F.data == "count_menu")
async def choose_count_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    premium = has_premium_access(callback.from_user.id)
    maximum = MAX_SEARCH_COUNT if premium else FREE_MAX_SEARCH_COUNT
    await callback.answer()
    await callback.message.answer(
        "🔢 <b>СКОЛЬКО ЮЗЕРНЕЙМОВ НАЙТИ?</b>\n\n"
        f"Выберите от <b>1 до {maximum}</b> свободных имён за поиск. "
        + ("" if has_ultra_access(callback.from_user.id) else
           f"⏳ Пауза между поисками — {SEARCH_COOLDOWN_SECONDS} секунд.\n")
        + ("" if premium else "🆓 Бесплатно: не более 3 поисков за сутки."),
        reply_markup=count_keyboard(selected_search_count(data), premium=premium),
    )


@router.callback_query(F.data.startswith("count:"))
async def select_search_count(callback: CallbackQuery, state: FSMContext) -> None:
    raw = callback.data.split(":", 1)[1]
    count = int(raw) if raw.isascii() and raw.isdigit() else 0
    premium = has_premium_access(callback.from_user.id)
    maximum = MAX_SEARCH_COUNT if premium else FREE_MAX_SEARCH_COUNT
    if not 1 <= count <= maximum:
        await callback.answer(
            f"Бесплатно доступно 1–2 имени. Premium — до {MAX_SEARCH_COUNT}.",
            show_alert=True,
        )
        return
    await state.update_data(count=count)
    await state.set_state(SearchState.ready)
    await callback.answer(f"Выбрано: {count}")
    await safe_edit(callback.message, 
        f"✅ <b>Количество результатов: {count}</b>\n\n"
        "Можно продолжить настройку или начать поиск.",
        reply_markup=search_keyboard(count),
    )


@router.callback_query(F.data == "count_back")
async def back_to_search_from_count(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await callback.answer()
    await safe_edit(callback.message, 
        "🔎 <b>НАСТРОЙКИ ПОИСКА</b>\n\nВыберите параметры и нажмите «Начать поиск».",
        reply_markup=search_keyboard(selected_search_count(data)),
    )


@router.callback_query(F.data.startswith("length:"))
async def choose_length(callback: CallbackQuery, state: FSMContext) -> None:
    length = int(callback.data.split(":", 1)[1])
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    if length == 5 and not has_premium_access(callback.from_user.id):
        await callback.answer("Для 5 букв нужна подписка", show_alert=True)
        await callback.message.answer(
            "🔒 Поиск пятибуквенных имён доступен по подписке.",
            reply_markup=buy_keyboard(),
        )
        return
    await state.update_data(length=length, mode="normal")
    await state.set_state(SearchState.ready)
    await callback.answer(f"Выбрано: {length} букв")
    await safe_edit(callback.message, 
        f"💎 <b>ПОИСК ЮЗЕРНЕЙМА</b>\n\nВыбрано: <b>{length} букв</b>. "
        "Можно настроить фильтр или сразу запустить поиск.",
        reply_markup=search_keyboard(selected_search_count(await state.get_data())),
    )


@router.callback_query(F.data == "filter")
async def ask_filter(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SearchState.entering_filter)
    await callback.answer()
    await callback.message.answer(
        "Отправьте фильтр одним сообщением.\n\n"
        "<code>?a?er</code> — маска, <code>-xq</code> — исключить буквы, "
        "<code>a,e -x</code> — нужна a/e и не должно быть x.\n"
        "Отправьте <code>-</code>, чтобы убрать фильтр."
    )


@router.callback_query(F.data == "tech_menu")
async def tech_menu_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    await callback.message.answer(
        "💻 <b>TECH / HACKER ФИЛЬТР</b>\n\n"
        "Генерирует технические юзернеймы в стиле:\n"
        "<code>@memdump</code> · <code>@dllhook</code> · <code>@hexscan</code> · <code>@x64patch</code>\n\n"
        "Выберите диапазон:",
        reply_markup=tech_keyboard(),
    )


@router.callback_query(F.data.startswith("tech:"))
async def choose_tech_mode(callback: CallbackQuery, state: FSMContext) -> None:
    variant = callback.data.split(":", 1)[1]
    ensure_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
    if variant == "wide" and not has_premium_access(callback.from_user.id):
        await callback.answer("Расширенный Tech-фильтр доступен по подписке", show_alert=True)
        await callback.message.answer("🔒 Оформите подписку для поиска Tech-юзернеймов длиной 7–10.", reply_markup=buy_keyboard())
        return
    if variant == "wide":
        await state.update_data(mode="tech", tech_min=7, tech_max=10, length=10, filter="")
        label = "7–10 символов"
    else:
        await state.update_data(mode="tech", tech_min=6, tech_max=6, length=6, filter="")
        label = "6 символов"
    await state.set_state(SearchState.ready)
    await callback.answer("Tech-фильтр выбран")
    await callback.message.answer(
        f"✅ Выбран <b>Tech / Hacker</b>: {label}.\nНажмите «Начать поиск».",
        reply_markup=search_keyboard(selected_search_count(await state.get_data())),
    )


@router.message(SearchState.entering_filter)
async def save_filter(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip().lower()[:32]
    if not all(ch in "abcdefghijklmnopqrstuvwxyz?,- " for ch in value):
        await message.answer("Используйте только латинские буквы, <code>?</code>, запятую, минус и пробел.")
        return
    await state.update_data(filter="" if value == "-" else value)
    await state.set_state(SearchState.ready)
    await message.answer(
        f"✅ Фильтр: <code>{value if value != '-' else 'выключен'}</code>",
        reply_markup=search_keyboard(selected_search_count(await state.get_data())),
    )


@router.callback_query(F.data == "run")
async def run_search(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    length = int(data.get("length", 6))
    raw_filter = str(data.get("filter", ""))
    mode = str(data.get("mode", "normal"))
    count = selected_search_count(data)
    check_limit = max(12, count * 4)
    user_id = callback.from_user.id
    ensure_user(user_id, callback.from_user.username, callback.from_user.first_name)
    premium = has_premium_access(user_id)
    ultra = has_ultra_access(user_id)

    if not premium and count > FREE_MAX_SEARCH_COUNT:
        await callback.answer("Бесплатно можно выбрать 1–2 имени.", show_alert=True)
        return
    if mode == "tech" and int(data.get("tech_max", 6)) > 6 and not premium:
        await callback.answer("Расширенный Tech-фильтр доступен по подписке", show_alert=True)
        await callback.message.answer(
            "🔒 Для Tech 7–10 символов оформите Premium.",
            reply_markup=buy_keyboard(),
        )
        return
    if mode == "normal" and length == 5 and not premium:
        await callback.answer("Для 5 букв нужна подписка", show_alert=True)
        await callback.message.answer(
            "🔒 Поиск 5-буквенных имён доступен с Premium.",
            reply_markup=buy_keyboard(),
        )
        return

    await callback.answer()
    if public_checker is None:
        await callback.message.answer("⚠️ Проверка Telegram временно недоступна. Попробуйте позже.")
        return
    if user_id in active_searches:
        await callback.message.answer("⏳ Ваш предыдущий поиск ещё идёт. Дождитесь окончания.")
        return

    # На одном event loop проверка и добавление без await атомарны.
    # Другие пользователи не блокируются; HTTP ограничивает общий семафор.
    active_searches.add(user_id)
    reserved = False
    status = None
    checked_count = 0
    failed_count = 0
    try:
        candidates = fresh_search_candidates(
            user_id, mode, length, raw_filter,
            int(data.get("tech_min", 6)), int(data.get("tech_max", 6)),
            limit=check_limit,
        )
        if not candidates:
            await callback.message.answer(
                "🔄 Все подходящие варианты для вас уже проверены. "
                "Измените длину или фильтр.",
                reply_markup=search_keyboard(count),
            )
            return

        access = reserve_search(user_id, premium=premium, ultra=ultra)
        if not access.allowed:
            if access.reason == "daily":
                message = (
                    "🚫 Бесплатный лимит: 3/3 поисков на сегодня исчерпан. "
                    "Следующие будут доступны после 00:00 по Киеву."
                )
            else:
                message = f"⏳ Следующий поиск через {access.wait_seconds} сек."
            await callback.message.answer(message, reply_markup=search_keyboard(count))
            return
        reserved = True
        LOGGER.info("Search started user_id=%s mode=%s count=%d candidates=%d",
                    user_id, mode, count, len(candidates))
        status = await callback.message.answer(
            "⏳ Проверяю публичные страницы Telegram. "
            "Регистрация имени не гарантируется."
        )

        async def update_progress(
            checked: int, total: int, found_count: int,
            username: str, verified: bool
        ) -> None:
            nonlocal checked_count, failed_count
            if verified:
                checked_count += 1
                mark_username_seen(user_id, username)
            else:
                failed_count += 1
                LOGGER.warning("t.me check skipped user_id=%s username=%s attempt=%s/%s",
                               user_id, username, checked, total)
            if checked == 1 or checked % 3 == 0 or checked == total:
                await safe_edit(
                    status,
                    "🔎 <b>Публичная проверка t.me</b>\n\n"
                    f"Последний: <code>@{username}</code>\n"
                    f"Попыток: <b>{checked}/{total}</b>\n"
                    f"Проверено: <b>{checked_count}</b> · ошибок: <b>{failed_count}</b>\n"
                    f"Возможно доступных: <b>{found_count}/{count}</b>",
                )

        found = await check_candidates(
            candidates, public_checker, limit=count, progress=update_progress
        )
        if checked_count == 0:
            LOGGER.warning("Search could not check any username user_id=%s", user_id)
            await safe_edit(
                status,
                "⚠️ Telegram не дал проверить эти имена. Попробуйте позже. "
                "Ни одно имя не объявлено свободным.",
                reply_markup=search_keyboard(count),
            )
            return
        if not found:
            await safe_edit(
                status,
                f"Готово. Попыток: {checked_count + failed_count}, "
                f"проверено: {checked_count}, ошибок: {failed_count}. "
                "Возможно доступных вариантов не найдено. Попробуйте другой фильтр.",
                reply_markup=search_keyboard(count),
            )
            LOGGER.info("Search done user_id=%s checked=%d found=0", user_id, checked_count)
            return

        await safe_edit(
            status,
            f"✅ Найдено кандидатов: <b>{len(found)} из {count}</b>. "
            f"Попыток: {checked_count + failed_count}, ошибок: {failed_count}. "
            "Готовлю карточки…",
        )
        for item in found:
            png = await asyncio.to_thread(render_username_card, item.username)
            card = BufferedInputFile(png, filename=f"{item.username}.png")
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text=f"📋 @{item.username}", copy_text=CopyTextButton(text=item.username),
                ),
                InlineKeyboardButton(
                    text="Проверить в Telegram", url=f"https://t.me/{item.username}",
                ),
            ]])
            await callback.message.answer_photo(
                card,
                caption=(
                    "🔎 <b>ВОЗМОЖНО ДОСТУПНЫЙ ЮЗЕРНЕЙМ</b>\n\n"
                    f"Юзернейм: <code>@{item.username}</code>\n"
                    f"Ценность: <b>{username_value(item.username)}/100</b>\n\n"
                    "Публичная страница не найдена. Имя может быть зарезервировано, "
                    "занято или недоступно к регистрации. Проверьте в Telegram вручную."
                ),
                reply_markup=keyboard,
            )
        await callback.message.answer(
            f"Поиск завершён: {len(found)} из {count}. "
            + ("" if ultra else
               f"Следующий поиск через {SEARCH_COOLDOWN_SECONDS} секунд. ")
            + "Это предварительные результаты, не гарантия регистрации.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Ещё варианты", callback_data="run")],
                [InlineKeyboardButton(text="↩️ Главное меню", callback_data="home")],
            ]),
        )
        LOGGER.info("Search done user_id=%s checked=%d failed=%d candidates=%d",
                    user_id, checked_count, failed_count, len(found))
    except (TelegramRetryAfter, TelegramNetworkError, TelegramServerError) as error:
        LOGGER.warning("Telegram send error user_id=%s: %s", user_id, error)
        if status is not None:
            try:
                await safe_edit(status, "⚠️ Telegram временно ограничил запросы или отправку. Повторите позже.")
            except Exception:
                LOGGER.exception("Failed to send search error user_id=%s", user_id)
    except Exception:
        LOGGER.exception("Search failed user_id=%s mode=%s", user_id, mode)
        try:
            if status is not None:
                await safe_edit(status, "⚠️ Ошибка поиска. Подробности записаны в logs/errors.log.")
            else:
                await callback.message.answer(
                    "⚠️ Ошибка поиска. Подробности записаны в logs/errors.log."
                )
        except Exception:
            LOGGER.exception("Failed to report search error user_id=%s", user_id)
    finally:
        try:
            if reserved:
                if checked_count > 0:
                    finish_search(user_id)
                else:
                    release_search_on_failure(user_id)
        except Exception:
            LOGGER.exception("Failed to finalize search quota user_id=%s", user_id)
        finally:
            active_searches.discard(user_id)


async def main() -> None:
    global public_checker, crypto_pay_token
    load_dotenv(ROOT / ".env")
    configure_logging()
    init_db()
    crypto_pay_token = os.getenv("CRYPTO_PAY_TOKEN", "").strip()
    if crypto_pay_token:
        try:
            app = await test_token(crypto_pay_token)
            LOGGER.info("Crypto Pay подключён: %s", app.get("name", "app"))
        except (CryptoPayError, aiohttp.ClientError, asyncio.TimeoutError):
            LOGGER.exception("Crypto Pay недоступен при запуске. Поиск продолжит работать.")
    else:
        LOGGER.warning("CRYPTO_PAY_TOKEN не задан — криптоплатежи отключены")

    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    session = aiohttp.ClientSession(
        headers={"User-Agent": "Mozilla/5.0 (compatible; PublicUsernameChecker/1.0)"},
        timeout=aiohttp.ClientTimeout(total=15),
    )
    public_checker = PublicUsernameChecker(session)
    dp = Dispatcher(storage=MemoryStorage())
    subscription_middleware = SubscriptionMiddleware()
    dp.message.outer_middleware(subscription_middleware)
    dp.callback_query.outer_middleware(subscription_middleware)
    dp.include_router(router)
    LOGGER.info(
        "Bot started: public t.me checks, HTTP concurrency=%d, phone login not required",
        8,
    )
    try:
        while True:
            try:
                await dp.start_polling(bot, handle_as_tasks=True)
                break
            except (TelegramNetworkError, TelegramServerError) as error:
                LOGGER.warning("Telegram polling error: %s. Retry in 5 seconds.", error)
                await asyncio.sleep(5)
    finally:
        public_checker = None
        await session.close()
        await bot.session.close()
        LOGGER.info("Bot stopped")


if __name__ == "__main__":
    asyncio.run(main())
