import asyncio
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY_RUN, DEMO_DEPOSIT_USD, is_placeholder
from solana_client import get_sol_balance


# Глобальная ссылка на текущую позицию (обновляется из main.py)
current_position = None


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
    
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode="HTML"
    )


async def notify_startup() -> None:
    """Уведомление о запуске бота."""
    from config import POLL_INTERVAL_SEC, RANGE_WIDTH_PCT, DEMO_POSITION

    mode = "🔸 DRY RUN (без транзакций)" if DRY_RUN else "🟢 БОЕВОЙ режим"
    demo = "\n📎 Демо-позиция (задай POSITION_MINT)" if DEMO_POSITION else ""
    await send_message(
        f"🤖 <b>Бот запущен</b>\n"
        f"{mode}\n"
        f"Пара: SOL/USDC\n"
        f"Новый диапазон при rebalance: ±{RANGE_WIDTH_PCT}%\n"
        f"Мониторинг каждые {POLL_INTERVAL_SEC // 60} мин{demo}"
    )


async def notify_out_of_range(position) -> None:
    """Уведомление когда цена вышла за границу."""
    await send_message(
        f"⚠️ <b>Цена вышла за границу!</b>\n"
        f"Текущая цена: ${position.current_price:.2f}\n"
        f"Диапазон: ${position.lower_price:.2f} — ${position.upper_price:.2f}\n"
        f"⏳ Жду 20 минут перед ребалансом..."
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


def format_position_balance(position) -> str:
    """Текстовый блок: состав позиции SOL/USDC в USD."""
    demo_note = f"\n<i>(демо ~${DEMO_DEPOSIT_USD:.0f}, задай POSITION_MINT)</i>" if getattr(position, "is_demo", False) else ""
    return (
        f"💰 <b>Позиция: ${position.total_value_usd:.2f}</b>{demo_note}\n"
        f"   SOL:  {position.amount_sol:.4f}  (${position.value_sol_usd:.2f})\n"
        f"   USDC: {position.amount_usdc:.2f}  (${position.value_usdc_usd:.2f})\n"
        f"💵 Fees: {position.fees_sol:.4f} SOL + ${position.fees_usdc:.2f} USDC "
        f"(≈${position.fees_total_usd:.2f})"
    )


def format_range(position) -> str:
    """Диапазон с процентным отклонением границ от текущей цены."""
    pct_lower = (position.current_price - position.lower_price) / position.current_price * 100
    pct_upper = (position.upper_price - position.current_price) / position.current_price * 100
    return (
        f"   Диапазон: ${position.lower_price:.2f} (−{pct_lower:.1f}%) "
        f"— ${position.upper_price:.2f} (+{pct_upper:.1f}%)"
    )


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

    await send_message(
        f"💓 <b>Бот работает [{mode}]{demo}</b>\n"
        f"{format_position_balance(position)}\n"
        f"📈 Цена SOL: ${position.current_price:.2f}\n"
        f"{format_range(position)}\n"
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

    await _reply(
        update,
        context,
        f"📊 <b>Статус [{mode}]{demo}</b>\n"
        f"{format_position_balance(position)}\n"
        f"📈 Цена SOL: ${position.current_price:.2f}\n"
        f"{format_range(position)}\n"
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
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("setrange", setrange_command))
    return app
