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
        f"   Диапазон: ${position.lower_price:.2f} — ${position.upper_price:.2f}\n"
        f"   Статус: {status}\n"
        f"{balance_line}"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик /status — свежие данные с chain."""
    global current_position

    from orca import get_position

    position = await get_position()
    if position is None:
        await update.message.reply_text("⏳ Не удалось загрузить позицию")
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

    await update.message.reply_text(
        f"📊 <b>Статус [{mode}]{demo}</b>\n"
        f"{format_position_balance(position)}\n"
        f"📈 Цена SOL: ${position.current_price:.2f}\n"
        f"   Диапазон: ${position.lower_price:.2f} — ${position.upper_price:.2f}\n"
        f"   Статус: {status}\n"
        f"{balance_line}",
        parse_mode="HTML"
    )


def build_telegram_app() -> Application:
    """Создаёт и настраивает Telegram приложение с командами."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("status", status_command))
    return app
