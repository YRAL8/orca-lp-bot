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
    POLL_INTERVAL_SEC,
    REBALANCE_DELAY_MIN,
    is_placeholder,
)
from solana_client import get_sol_balance, get_usdc_balance

log = logging.getLogger(__name__)

# Глобальная ссылка на текущую позицию (обновляется из main.py)
current_position = None

# История цен для тренда (обновляется из main.py / monitor_position)
price_history: deque[float] = deque(maxlen=12)

# Ленивый singleton Bot — создаётся при первом реальном send_message().
_bot: Bot | None = None

# Команды, доступные только владельцу (TELEGRAM_CHAT_ID).
_OWNER_COMMANDS = (
    "status", "setrange", "rebalance", "addliquidity", "open", "pauza", "stop", "boevoy",
)


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
    from config import RANGE_WIDTH_PCT, POSITION_MINT

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


async def notify_position_lost(mint: str) -> None:
    """
    Уведомление о том, что get_position() не смог найти позицию on-chain для
    настроенного POSITION_MINT — раньше это молча логировалось и мониторинг
    выходил без единого алерта (найдено независимым аудитом, Opus 4.8,
    2026-07-27): heartbeat при этом продолжал слать последнюю УСПЕШНУЮ позицию
    как актуальную, маскируя реальную проблему под "всё в порядке".
    """
    await send_message(
        f"❌ <b>Позиция не найдена on-chain</b>\n"
        f"mint: <code>{mint}</code>\n"
        f"Проверь POSITION_MINT в .env и реальное состояние позиции вручную — "
        f"мониторинг и heartbeat приостановлены, пока это не разрешится."
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


def _format_trend_period(seconds: int) -> str:
    """Человекочитаемый период тренда по накопленной истории."""
    minutes = max(1, seconds // 60) if seconds > 0 else 0
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    rem = minutes % 60
    if rem == 0:
        return f"{hours}ч"
    return f"{hours}ч {rem}мин"


def format_price_trend(position) -> str:
    """Стрелка тренда и % изменения от самой старой точки price_history.

    Пустая строка, пока накопилось меньше 2 точек.
    """
    if len(price_history) < 2:
        return ""
    oldest = price_history[0]
    if oldest <= 0:
        return ""
    change_pct = (position.current_price - oldest) / oldest * 100
    if change_pct > 0.5:
        arrow = "↗"
    elif change_pct < -0.5:
        arrow = "↘"
    else:
        arrow = "→"
    period = _format_trend_period(len(price_history) * POLL_INTERVAL_SEC)
    sign = "+" if change_pct >= 0 else ""
    return f" ({arrow} {sign}{change_pct:.1f}% за {period})"


async def send_heartbeat(position) -> None:
    """Heartbeat сообщение каждые 4 часа."""
    try:
        sol_balance = await get_sol_balance()
        status = "✅ в диапазоне" if position.in_range else "❌ вне диапазона"
        mode = "DRY RUN" if DRY_RUN else "БОЕВОЙ"
        demo = " [демо]" if getattr(position, "is_demo", False) else ""
        balance_line = (
            f"Баланс кошелька: {sol_balance:.4f} SOL"
            if sol_balance is not None
            else "Кошелёк не настроен (read-only)"
        )

        await send_message(
            f"💓 <b>Бот работает [{mode}]{demo}</b>\n"
            f"{format_position_balance(position)}\n"
            f"📈 Цена SOL: ${position.current_price:.2f}{format_price_trend(position)}\n"
            f"{format_range_bar(position)}\n"
            f"   Статус: {status}\n"
            f"{balance_line}"
        )
    except Exception as e:
        log.exception("Не удалось сформировать heartbeat: %s", e)
        try:
            await send_message(f"⚠️ <b>Heartbeat не удался</b>\nОшибка: {e}")
        except Exception:
            log.exception("Не удалось отправить даже сообщение об ошибке heartbeat")


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

    try:
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

        await _reply(
            update,
            context,
            f"📊 <b>Статус [{mode}]{demo}</b>\n"
            f"{format_position_balance(position)}\n"
            f"📈 Цена SOL: ${position.current_price:.2f}{format_price_trend(position)}\n"
            f"{format_range_bar(position)}\n"
            f"   Статус: {status}\n"
            f"{balance_line}",
            parse_mode="HTML",
        )
    except Exception as e:
        log.exception("Ошибка /status: %s", e)
        await _reply(update, context, f"❌ Ошибка /status: {e}")


async def setrange_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /setrange <процент> — временно меняет RANGE_WIDTH_PCT в рантайме."""
    global current_position

    import config
    from orca import get_position, reset_demo_range

    old_pct = config.RANGE_WIDTH_PCT

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

        if position.is_demo:
            # Демо-диапазон (_demo_range) уже пересчитан на новый pct внутри
            # get_position(), так что lower_price/upper_price — это и есть новый диапазон.
            range_block = (
                f"   Нижняя: ${position.lower_price:.2f}\n"
                f"   Верхняя: ${position.upper_price:.2f}\n"
            )
        else:
            # РЕАЛЬНУЮ открытую позицию /setrange не двигает — её on-chain диапазон
            # не изменится до следующего /rebalance или /open. Раньше здесь тоже
            # печатались position.lower_price/upper_price, то есть СТАРЫЙ, никак не
            # связанный с новым pct диапазон, подписанный как будто это и есть новый
            # "Диапазон обновлён" — реально вводило в заблуждение, поймано вживую
            # 2026-07-27 (/setrange 2 показал прежние ±8%-границы).
            preview_lower = position.current_price * (1 - pct / 100)
            preview_upper = position.current_price * (1 + pct / 100)
            range_block = (
                f"Текущая открытая позиция НЕ меняется: "
                f"${position.lower_price:.2f}–${position.upper_price:.2f}\n"
                f"Ориентировочно при следующем /rebalance или /open: "
                f"~${preview_lower:.2f}–${preview_upper:.2f}\n"
            )

        await _reply(
            update,
            context,
            f"✅ <b>RANGE_WIDTH_PCT = ±{pct}%</b>\n"
            f"📈 Цена SOL: ${position.current_price:.2f}\n"
            f"{range_block}"
            f"Изменение действует до перезапуска бота — в .env остаётся прежнее значение по умолчанию.",
            parse_mode="HTML",
        )
    except Exception as e:
        # Атомарность: если RANGE_WIDTH_PCT успел измениться до сбоя — откатываем,
        # чтобы сообщение "❌ Ошибка" не расходилось с реальным состоянием конфига.
        if config.RANGE_WIDTH_PCT != old_pct:
            config.RANGE_WIDTH_PCT = old_pct
            reset_demo_range()
        await _reply(
            update,
            context,
            f"❌ Ошибка /setrange: {e}",
        )


async def rebalance_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик /rebalance — вручную запускает ребаланс текущей позиции прямо сейчас,
    независимо от AUTO_REBALANCE (та настройка гейтит только автоматический путь из
    monitor_position(), ручной вызов через Telegram — осознанное действие владельца).
    """
    global current_position

    # main._rebalance_lock — тот же лок, что и у автоматического ребаланса в
    # monitor_position(). Импорт внутри функции (не на уровне модуля) — main.py уже
    # импортирует telegram_bot как tg, импорт в обратную сторону на уровне модуля
    # был бы циклическим.
    import main
    from orca import get_position, rebalance

    if main.bot_frozen:
        await _reply(update, context, "🛑 Бот заморожен (/stop) — сначала /boevoy.")
        return

    if main._rebalance_lock.locked():
        await _reply(update, context, "⏳ Ребаланс уже выполняется — подожди.")
        return

    try:
        position = await get_position()
    except Exception as e:
        await _reply(update, context, f"❌ Не удалось загрузить позицию: {e}")
        return

    if position is None:
        await _reply(update, context, "❌ Нет открытой позиции для ребаланса.")
        return

    await _reply(update, context, "🔄 Начинаю ручной ребаланс...")

    async with main._rebalance_lock:
        try:
            await notify_rebalance_start(position)
            new_position = await rebalance(position)
            if new_position:
                current_position = new_position
                await notify_rebalance_complete(position, new_position)
            else:
                await notify_rebalance_error("Не удалось выполнить ребаланс")
        except Exception as e:
            log.exception("Ошибка при ручном /rebalance: %s", e)
            await notify_rebalance_error(str(e))


async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик /open <сумма USDC> — открывает НОВУЮ позицию, когда открытой позиции
    сейчас нет (после ручного/автоматического close, если reopen не удался — например
    из-за 429 на отправке транзакции, см. инцидент 2026-07-27). В отличие от
    /rebalance, не требует существующей позиции для старта; в отличие от
    /addliquidity, создаёт новый диапазон вокруг текущей цены (RANGE_WIDTH_PCT),
    а не доливает в старый.
    """
    global current_position

    import config
    from orca import get_position, open_position, get_current_price

    import main

    if main.bot_frozen:
        await _reply(update, context, "🛑 Бот заморожен (/stop) — сначала /boevoy.")
        return

    if main._rebalance_lock.locked():
        await _reply(update, context, "⏳ Идёт ребаланс/открытие — подожди.")
        return

    if not context.args:
        await _reply(
            update,
            context,
            "Использование: /open &lt;сумма USDC&gt;\nПример: /open 5",
            parse_mode="HTML",
        )
        return

    try:
        usdc_amount = float(context.args[0])
    except ValueError:
        await _reply(update, context, "❌ Нужно число, например: /open 5")
        return

    if usdc_amount <= 0:
        await _reply(update, context, "❌ Сумма должна быть больше 0.")
        return

    async with main._rebalance_lock:
        try:
            existing = await get_position()
        except Exception as e:
            await _reply(update, context, f"❌ Не удалось проверить текущую позицию: {e}")
            return

        if existing is not None and not existing.is_demo:
            await _reply(
                update,
                context,
                f"❌ Позиция уже открыта (${existing.total_value_usd:.2f}) — "
                "используй /rebalance или /addliquidity, а не /open.",
            )
            return

        usdc_balance = await get_usdc_balance()
        sol_balance = await get_sol_balance()
        if usdc_balance is not None and usdc_amount > usdc_balance:
            await _reply(
                update,
                context,
                f"❌ Запрошено ${usdc_amount:.2f} USDC, на кошельке только ${usdc_balance:.2f} USDC.",
            )
            return
        # OPEN_POSITION_RENT_RESERVE_SOL — отдельный резерв именно под безвозвратную
        # ренту НОВОЙ позиции (position PDA + mint + ATA + metadata), см. config.py.
        # Не путать с MIN_SOL_BALANCE — тот только порог алерта "мало SOL".
        required_reserve = config.MIN_SOL_BALANCE + config.OPEN_POSITION_RENT_RESERVE_SOL
        if sol_balance is not None and sol_balance < required_reserve:
            await _reply(
                update,
                context,
                f"❌ Мало SOL для открытия новой позиции: есть {sol_balance:.4f}, "
                f"нужно минимум {required_reserve:.4f} (резерв на ренту + MIN_SOL_BALANCE).",
            )
            return

        await _reply(update, context, f"🆕 Открываю новую позицию на ${usdc_amount:.2f} USDC...")

        try:
            current_price = await get_current_price()
            new_position = await open_position(current_price, usdc_amount=usdc_amount)
            if new_position:
                current_position = new_position
                await _reply(
                    update,
                    context,
                    f"✅ <b>Позиция открыта</b>\n"
                    f"💰 ${new_position.total_value_usd:.2f} "
                    f"(SOL ${new_position.value_sol_usd:.2f} + USDC ${new_position.value_usdc_usd:.2f})\n"
                    f"Диапазон: ${new_position.lower_price:.2f}–${new_position.upper_price:.2f}",
                    parse_mode="HTML",
                )
            else:
                await _reply(update, context, "❌ Не удалось подтвердить открытие позиции.")
        except Exception as e:
            log.exception("Ошибка при /open: %s", e)
            await _reply(update, context, f"❌ Ошибка: {e!r}")


async def addliquidity_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик /addliquidity <сумма USDC> — доливает ликвидность В ТЕКУЩУЮ открытую
    позицию (increase_liquidity на её существующий диапазон), без close/reopen. Сумму
    выбирает владелец каждый раз явно; бот только проверяет баланс/состояние позиции
    перед отправкой, ничего не решает и не лимитирует сам.
    """
    global current_position

    import config
    from orca import get_position, add_liquidity, estimate_add_liquidity_sol_needed

    # Тот же лок, что и у /rebalance и автоматического ребаланса — доливка во время
    # close/reopen целилась бы в position_pda, которого в этот момент может уже не быть
    # (или ещё не быть) on-chain.
    import main

    if main.bot_frozen:
        await _reply(update, context, "🛑 Бот заморожен (/stop) — сначала /boevoy.")
        return

    if main._rebalance_lock.locked():
        await _reply(update, context, "⏳ Идёт ребаланс — подожди, потом долей.")
        return

    if not context.args:
        await _reply(
            update,
            context,
            "Использование: /addliquidity &lt;сумма USDC&gt;\nПример: /addliquidity 5",
            parse_mode="HTML",
        )
        return

    try:
        usdc_amount = float(context.args[0])
    except ValueError:
        await _reply(update, context, "❌ Нужно число, например: /addliquidity 5")
        return

    if usdc_amount <= 0:
        await _reply(update, context, "❌ Сумма должна быть больше 0.")
        return

    async with main._rebalance_lock:
        try:
            position = await get_position()
        except Exception as e:
            await _reply(update, context, f"❌ Не удалось загрузить позицию: {e}")
            return

        if position is None:
            await _reply(update, context, "❌ Нет открытой позиции для доливки.")
            return
        if position.is_demo:
            await _reply(
                update,
                context,
                "❌ Сейчас активна демо-позиция (POSITION_MINT не задан) — доливать некуда.",
            )
            return

        usdc_balance = await get_usdc_balance()
        sol_balance = await get_sol_balance()
        if usdc_balance is not None and usdc_amount > usdc_balance:
            await _reply(
                update,
                context,
                f"❌ Запрошено ${usdc_amount:.2f} USDC, на кошельке только ${usdc_balance:.2f} USDC.",
            )
            return
        # Точный расчёт вместо грубого "sol_balance <= MIN_SOL_BALANCE": та проверка
        # блокировала даже доливки, которым SOL вообще не нужен (позиция вне
        # диапазона, вход целиком в USDC), и пропускала пограничные случаи, где
        # реально нужная сумма SOL всё равно не влезала в остаток после резерва —
        # транзакция ушла бы в сеть и упала on-chain, впустую сжигая газ (найдено
        # независимым аудитом, GPT-5.2, 2026-07-27).
        try:
            required_sol = await estimate_add_liquidity_sol_needed(position, usdc_amount)
        except Exception as e:
            await _reply(update, context, f"❌ Не удалось оценить нужный SOL: {e}")
            return

        if sol_balance is not None:
            usable_sol = sol_balance - config.MIN_SOL_BALANCE
            if required_sol > usable_sol:
                await _reply(
                    update,
                    context,
                    f"❌ Для доливки ${usdc_amount:.2f} USDC нужно ещё ~{required_sol:.4f} SOL "
                    f"второй ногой, а свободно (за вычетом MIN_SOL_BALANCE="
                    f"{config.MIN_SOL_BALANCE}) только {usable_sol:.4f} SOL. "
                    "Пополни кошелёк или уменьши сумму.",
                )
                return

        warn = (
            ""
            if position.in_range
            else "⚠️ Позиция сейчас ВНЕ диапазона — доливка ляжет в основном в один токен.\n"
        )
        await _reply(update, context, f"{warn}💧 Доливаю ${usdc_amount:.2f} USDC в текущую позицию...")

        try:
            new_position = await add_liquidity(position, usdc_amount)
            if new_position:
                current_position = new_position
                await _reply(
                    update,
                    context,
                    f"✅ <b>Ликвидность добавлена</b>\n"
                    f"💰 Позиция: ${new_position.total_value_usd:.2f} "
                    f"(SOL ${new_position.value_sol_usd:.2f} + USDC ${new_position.value_usdc_usd:.2f})\n"
                    f"Диапазон: ${new_position.lower_price:.2f}–${new_position.upper_price:.2f}",
                    parse_mode="HTML",
                )
            else:
                await _reply(update, context, "❌ Не удалось подтвердить доливку.")
        except Exception as e:
            log.exception("Ошибка при /addliquidity: %s", e)
            await _reply(update, context, f"❌ Ошибка: {e}")


async def pauza_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pauza — останавливает только автоматический цикл (monitor_position);
    ручные команды (/rebalance, /addliquidity) по-прежнему работают."""
    import main

    main.bot_paused = True
    await _reply(
        update,
        context,
        "⏸ Автоматика приостановлена — авто-мониторинг и авто-ребаланс не работают.\n"
        "Ручные команды (/rebalance, /addliquidity) по-прежнему доступны.\n"
        "Вернуть всё: /boevoy",
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop — полная заморозка: и автоматика, и денежные ручные команды отключены."""
    import main

    main.bot_frozen = True
    await _reply(
        update,
        context,
        "🛑 Полная заморозка — автоматика и /rebalance, /addliquidity отключены.\n"
        "Вернуть всё: /boevoy",
    )


async def boevoy_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/boevoy — снимает разом /pauza и /stop, полный боевой режим."""
    import main

    main.bot_paused = False
    main.bot_frozen = False
    await _reply(
        update,
        context,
        "⚔️ Боевой режим — автоматика и все ручные команды снова работают.",
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
    app.add_handler(CommandHandler("rebalance", rebalance_command, filters=owner_chat))
    app.add_handler(CommandHandler("addliquidity", addliquidity_command, filters=owner_chat))
    app.add_handler(CommandHandler("open", open_command, filters=owner_chat))
    app.add_handler(CommandHandler("pauza", pauza_command, filters=owner_chat))
    app.add_handler(CommandHandler("stop", stop_command, filters=owner_chat))
    app.add_handler(CommandHandler("boevoy", boevoy_command, filters=owner_chat))
    # Чужие чаты: только warning в лог, без ответа (не светим, что бот живой).
    app.add_handler(
        CommandHandler(
            list(_OWNER_COMMANDS),
            unauthorized_command,
            filters=~owner_chat,
        )
    )
    return app
