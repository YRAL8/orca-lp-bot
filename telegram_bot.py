import asyncio
import logging
from collections import deque
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes, filters
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    DRY_RUN,
    AUTO_REBALANCE,
    DEMO_DEPOSIT_USD,
    REBALANCE_DELAY_MIN,
    is_placeholder,
)
from solana_client import get_sol_balance

log = logging.getLogger(__name__)

# Глобальная ссылка на текущую позицию (обновляется из main.py)
current_position = None

# История цен для sparkline (обновляется из main.py / monitor_position)
price_history: deque[float] = deque(maxlen=12)

_SPARK_CHARS = "▁▂▃▄▅▆▇█"

# Ленивый singleton Bot — создаётся при первом реальном send_message().
_bot: Bot | None = None

# Команды, доступные только владельцу (TELEGRAM_CHAT_ID).
_OWNER_COMMANDS = ("status", "setrange")


def _owner_chat_id() -> int | None:
    """TELEGRAM_CHAT_ID как int, или None если пусто/плейсхолдер/нечисловое."""
    if not TELEGRAM_CHAT_ID or is_placeholder(TELEGRAM_CHAT_ID):
        return None
    try:
        return int(TELEGRAM_CHAT_ID)
    except ValueError:
        log.error(
            "TELEGRAM_CHAT_ID=%r не число — команды /status и /setrange не зарегистрированы",
            TELEGRAM_CHAT_ID,
        )
        return None


async def unauthorized_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Логирует попытку вызвать команду из чужого чата — без ответа в Telegram."""
    chat = update.effective_chat
    chat_id = chat.id if chat is not None else None
    text = update.effective_message.text if update.effective_message else None
    command = (text.split()[0] if text else "?")
    log.warning(
        "Ignored Telegram command %s from unauthorized chat_id=%s",
        command,
        chat_id,
    )


def _get_bot() -> Bot:
    """Возвращает общий Bot, создавая его при первом вызове."""
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


async def send_message(text: str) -> None:
    """Отправляет сообщение в Telegram."""
    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHAT_ID
        or is_placeholder(TELEGRAM_BOT_TOKEN)
        or is_placeholder(TELEGRAM_CHAT_ID)
    ):
        print(f"📱 TELEGRAM (заглушка): {text}")
        return

    bot = _get_bot()
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="HTML"
    )


async def notify_startup() -> None:
    """Уведомление о запуске бота — явно показывает DRY_RUN / AUTO_REBALANCE."""
    from config import POLL_INTERVAL_SEC, RANGE_WIDTH_PCT, POSITION_MINT

    if DRY_RUN:
        dry = "DRY RUN"
    else:
        dry = "БОЕВОЙ"
    if AUTO_REBALANCE:
        mode = f"{dry} · авто-ребаланс (AUTO_REBALANCE=on)"
        range_line = f"Диапазон при ребалансе: ±{RANGE_WIDTH_PCT:g}%"
    else:
        mode = f"{dry} · только наблюдение (AUTO_REBALANCE=off)"
        range_line = f"Диапазон при будущем ребалансе: ±{RANGE_WIDTH_PCT:g}%"

    poll_min = POLL_INTERVAL_SEC // 60
    if is_placeholder(POSITION_MINT):
        position_line = "Позиция: демо (задай POSITION_MINT)"
    else:
        position_line = "Позиция: on-chain"

    await send_message(
        f"🤖 <b>Бот запущен</b>\n"
        f"Режим: {mode}\n"
        f"Пара: SOL/USDC · опрос {poll_min} мин · задержка {REBALANCE_DELAY_MIN} мин\n"
        f"{range_line}\n"
        f"{position_line}"
    )


async def notify_out_of_range(position) -> None:
    """Уведомление когда цена вышла за границу."""
    await send_message(
        f"⚠️ <b>Цена вышла за границу!</b>\n"
        f"Текущая цена: ${position.current_price:.2f}\n"
        f"Диапазон: ${position.lower_price:.2f} — ${position.upper_price:.2f}\n"
        f"⏳ Жду {REBALANCE_DELAY_MIN} минут перед ребалансом..."
    )


async def notify_price_returned(position) -> None:
    """Уведомление когда цена вернулась в диапазон."""
    await send_message(
        f"✅ <b>Цена вернулась в диапазон</b>\n"
        f"Текущая цена: ${position.current_price:.2f}\n"
        f"Диапазон: ${position.lower_price:.2f} — ${position.upper_price:.2f}\n"
        f"Продолжаю мониторинг..."
    )


async def notify_manual_action_needed(position) -> None:
    """Watch-only режим (AUTO_REBALANCE=false): цена вне диапазона дольше
    порога ожидания — бот сам ничего не делает, нужно вмешаться вручную."""
    await send_message(
        f"🔔 <b>Нужен ручной ребаланс</b>\n"
        f"Текущая цена: ${position.current_price:.2f}\n"
        f"Диапазон: ${position.lower_price:.2f} — ${position.upper_price:.2f}\n"
        f"Бот в режиме наблюдения — автоматических действий не будет.\n"
        f"Дальше сообщу, когда цена сама вернётся в диапазон."
    )


async def notify_rebalance_start(position) -> None:
    """Уведомление о начале ребаланса."""
    await send_message(
        f"🔄 <b>Начинаю ребаланс</b>\n"
        f"Старый диапазон: ${position.lower_price:.2f} — ${position.upper_price:.2f}\n"
        f"Собираю fees и закрываю позицию..."
    )


async def notify_rebalance_complete(old_position, new_position) -> None:
    """Уведомление об успешном ребалансе."""
    await send_message(
        f"✅ <b>Ребаланс завершён</b>\n"
        f"Старый диапазон: ${old_position.lower_price:.2f} — ${old_position.upper_price:.2f}\n"
        f"Новый диапазон: ${new_position.lower_price:.2f} — ${new_position.upper_price:.2f}\n"
        f"Fees собрано: {old_position.fees_sol:.4f} SOL + {old_position.fees_usdc:.2f} USDC"
    )


async def notify_rebalance_error(error: str) -> None:
    """Уведомление об ошибке ребаланса."""
    await send_message(
        f"❌ <b>Ошибка ребаланса!</b>\n"
        f"Ошибка: {error}\n"
        f"Требуется ручная проверка!"
    )


async def notify_low_sol_balance(balance: float) -> None:
    """Уведомление о низком балансе SOL."""
    await send_message(
        f"⚠️ <b>Низкий баланс SOL!</b>\n"
        f"Текущий баланс: {balance:.4f} SOL\n"
        f"Пополни кошелёк для оплаты газа!"
    )


async def notify_rpc_down(ticks: int) -> None:
    """Уведомление о недоступности RPC после серии подряд ошибок."""
    await send_message(
        f"❌ <b>RPC недоступен</b>\n"
        f"Мониторинг не работает уже {ticks} тиков подряд."
    )


def format_position_balance(position) -> str:
    """Состав позиции SOL/USDC — моноширинная таблица в <pre>."""
    demo_note = (
        f"\n<i>(демо ~${DEMO_DEPOSIT_USD:.0f}, задай POSITION_MINT)</i>"
        if getattr(position, "is_demo", False)
        else ""
    )
    usd_sol = f"${position.value_sol_usd:.2f}"
    usd_usdc = f"${position.value_usdc_usd:.2f}"
    usd_total = f"${position.total_value_usd:.2f}"
    usd_fees = f"${position.fees_total_usd:.2f}"
    table = (
        f"{'':5}{'qty':>8}  {'USD':>8}\n"
        f"{'SOL':5}{position.amount_sol:>8.4f}  {usd_sol:>8}\n"
        f"{'USDC':5}{position.amount_usdc:>8.2f}  {usd_usdc:>8}\n"
        f"{'─' * 23}\n"
        f"{'TOTAL':5}{'':8}  {usd_total:>8}\n"
        f"{'Fees':5}{'':8}  {usd_fees:>8}"
    )
    return (
        f"💰 <b>Позиция: ${position.total_value_usd:.2f}</b>{demo_note}\n"
        f"<pre>{table}</pre>"
    )


def _format_bound_pct(pct: float, *, in_range_sign: str) -> str:
    """Процент относительно границы: abs + явный знак, без '+-' / '--'.

    pct >= 0 — цена по обычную сторону границы (in_range_sign).
    pct < 0  — цена уже пересекла границу (перелёт) → минус.
    """
    if pct >= 0:
        return f"{in_range_sign}{pct:.1f}%"
    return f"−{abs(pct):.1f}%"


def format_range(position) -> str:
    """Диапазон с процентным отклонением границ от текущей цены."""
    pct_lower = (position.current_price - position.lower_price) / position.current_price * 100
    pct_upper = (position.upper_price - position.current_price) / position.current_price * 100
    return (
        f"   Диапазон: ${position.lower_price:.2f} ({_format_bound_pct(pct_lower, in_range_sign='−')}) "
        f"— ${position.upper_price:.2f} ({_format_bound_pct(pct_upper, in_range_sign='+')})"
    )


def format_range_bar(position) -> str:
    """Прогресс-бар положения цены в диапазоне (16 символов █/░)."""
    span = position.upper_price - position.lower_price
    if span > 0:
        pct = (position.current_price - position.lower_price) / span
    else:
        pct = 0.5

    pct_clamped = max(0.0, min(1.0, pct))
    filled = round(pct_clamped * 16)
    filled = max(0, min(16, filled))
    bar = "█" * filled + "░" * (16 - filled)

    if pct < 0:
        extra = f" ↓ {pct * 100:.1f}%"
    elif pct > 1:
        extra = f" ↑ +{(pct - 1) * 100:.1f}%"
    else:
        extra = ""

    return (
        f"<code>{bar}</code>{extra}\n"
        f"L ${position.lower_price:.2f}  "
        f"C ${position.current_price:.2f}  "
        f"U ${position.upper_price:.2f}"
    )


def format_sparkline() -> str:
    """Мини-график последних цен из price_history; пустая строка если < 2 точек."""
    if len(price_history) < 2:
        return ""
    prices = list(price_history)
    lo = min(prices)
    hi = max(prices)
    if hi <= lo:
        levels = [3] * len(prices)
    else:
        levels = [int((p - lo) / (hi - lo) * 7) for p in prices]
    spark = "".join(_SPARK_CHARS[level] for level in levels)
    return f"<code>{spark}</code>"


def _range_and_spark_section(position, *, include_format_range: bool) -> str:
    """Секция диапазона + опциональный sparkline под баром."""
    parts: list[str] = []
    if include_format_range:
        parts.append(format_range(position))
    parts.append(format_range_bar(position))
    spark = format_sparkline()
    if spark:
        parts.append(spark)
    return "\n".join(parts)


async def send_heartbeat(position) -> None:
    """Heartbeat сообщение каждые 4 часа."""
    sol_balance = await get_sol_balance()
    status = "✅ в диапазоне" if position.in_range else "❌ вне диапазона"
    mode = "DRY RUN" if DRY_RUN else "БОЕВОЙ"
    demo = " [демо]" if getattr(position, "is_demo", False) else ""
    balance_line = (
        f"Баланс кошелька: {sol_balance:.4f} SOL"
        if sol_balance is not None
        else "Кошелёк не настроен (read-only)"
    )

    # Вариант A: бар вместо процентной строки format_range (см. discussion 2026-07-26)
    await send_message(
        f"💓 <b>Бот работает [{mode}]{demo}</b>\n"
        f"{format_position_balance(position)}\n"
        f"📈 Цена SOL: ${position.current_price:.2f}\n"
        f"{_range_and_spark_section(position, include_format_range=False)}\n"
        f"   Статус: {status}\n"
        f"{balance_line}"
    )


async def _reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    *,
    parse_mode: str | None = None,
) -> None:
    """Надёжная отправка ответа: effective_message, иначе send_message в TELEGRAM_CHAT_ID."""
    message = update.effective_message
    if message is not None:
        await message.reply_text(text, parse_mode=parse_mode)
        return
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=parse_mode,
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /status — свежие данные с chain."""
    global current_position

    from orca import get_position

    position = await get_position()
    if position is None:
        await _reply(update, context, "⏳ Не удалось загрузить позицию")
        return

    current_position = position

    sol_balance = await get_sol_balance()
    status = "✅ в диапазоне" if position.in_range else "❌ вне диапазона"
    mode = "DRY RUN" if DRY_RUN else "БОЕВОЙ"
    demo = " [демо]" if getattr(position, "is_demo", False) else ""
    balance_line = (
        f"Баланс SOL: {sol_balance:.4f}"
        if sol_balance is not None
        else "Кошелёк не настроен (read-only)"
    )

    # Вариант A: бар вместо процентной строки format_range (см. discussion 2026-07-26)
    await _reply(
        update,
        context,
        f"📊 <b>Статус [{mode}]{demo}</b>\n"
        f"{format_position_balance(position)}\n"
        f"📈 Цена SOL: ${position.current_price:.2f}\n"
        f"{_range_and_spark_section(position, include_format_range=False)}\n"
        f"   Статус: {status}\n"
        f"{balance_line}",
        parse_mode="HTML",
    )


async def setrange_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /setrange <процент> — временно меняет RANGE_WIDTH_PCT в рантайме."""
    global current_position

    import config
    from orca import get_position, reset_demo_range

    try:
        if not context.args:
            await _reply(
                update,
                context,
                "Использование: /setrange &lt;процент&gt;\n"
                "Пример: /setrange 5 или /setrange 3.5\n"
                "Допустимо: от 1 до 50",
                parse_mode="HTML",
            )
            return

        try:
            pct = float(context.args[0])
        except ValueError:
            await _reply(
                update,
                context,
                "❌ Нужно число, например: /setrange 5 или /setrange 3.5",
            )
            return

        if not (1.0 <= pct <= 50):
            await _reply(
                update,
                context,
                "❌ Процент должен быть от 1 до 50 (включительно).\n"
                "Диапазоны уже 1% нереалистичны для реальной LP-позиции "
                "и ломают демо-расчёт стоимости (не 50/50 при очень узком "
                "диапазоне — свойство математики концентрированной ликвидности).",
            )
            return

        config.RANGE_WIDTH_PCT = pct
        reset_demo_range()

        position = await get_position()
        if position is None:
            await _reply(
                update,
                context,
                f"✅ RANGE_WIDTH_PCT = ±{pct}%\n"
                "⏳ Не удалось загрузить позицию для показа нового диапазона.\n"
                "Изменение действует до перезапуска бота — в .env остаётся прежнее значение по умолчанию.",
            )
            return

        current_position = position

        await _reply(
            update,
            context,
            f"✅ <b>Диапазон обновлён: ±{pct}%</b>\n"
            f"📈 Цена SOL: ${position.current_price:.2f}\n"
            f"   Нижняя: ${position.lower_price:.2f}\n"
            f"   Верхняя: ${position.upper_price:.2f}\n"
            f"Изменение действует до перезапуска бота — в .env остаётся прежнее значение по умолчанию.",
            parse_mode="HTML",
        )
    except Exception as e:
        await _reply(
            update,
            context,
            f"❌ Ошибка /setrange: {e}",
        )


def build_telegram_app() -> Application:
    """Создаёт и настраивает Telegram приложение с командами."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    owner_id = _owner_chat_id()
    if owner_id is None:
        log.error(
            "TELEGRAM_CHAT_ID не задан или некорректен — "
            "/status и /setrange не зарегистрированы"
        )
        return app

    owner_chat = filters.Chat(chat_id=owner_id)
    app.add_handler(CommandHandler("status", status_command, filters=owner_chat))
    app.add_handler(CommandHandler("setrange", setrange_command, filters=owner_chat))
    # Чужие чаты: только warning в лог, без ответа (не светим, что бот живой).
    app.add_handler(
        CommandHandler(
            list(_OWNER_COMMANDS),
            unauthorized_command,
            filters=~owner_chat,
        )
    )
    return app
