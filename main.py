import asyncio
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import telegram_bot as tg
from orca import get_position, rebalance, get_current_price
from solana_client import get_sol_balance, check_sol_balance
from config import (
    POLL_INTERVAL_SEC, REBALANCE_DELAY_MIN,
    HEARTBEAT_INTERVAL_HOURS, DRY_RUN, MIN_SOL_BALANCE,
    TELEGRAM_BOT_TOKEN, is_placeholder,
)

# Настройка логов
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# Флаг чтобы не запускать два ребаланса одновременно
rebalance_in_progress = False

# Время когда цена первый раз вышла за границу
out_of_range_since: datetime = None


async def monitor_position() -> None:
    """
    Основной цикл мониторинга.
    Запускается каждые POLL_INTERVAL_SEC секунд.
    """
    global rebalance_in_progress, out_of_range_since

    # Если уже идёт ребаланс — пропускаем
    if rebalance_in_progress:
        log.info("Ребаланс в процессе, пропускаем мониторинг")
        return

    # Проверяем баланс SOL (пропускаем, если кошелёк не настроен — read-only dry-run)
    sol_balance = await get_sol_balance()
    if sol_balance is not None and sol_balance < MIN_SOL_BALANCE:
        await tg.notify_low_sol_balance(sol_balance)
        log.warning(f"Низкий баланс SOL: {sol_balance:.4f}")
        return

    # Читаем позицию
    position = await get_position()
    if position is None:
        log.error("Не удалось получить позицию")
        return

    # Обновляем глобальную позицию для /status
    tg.current_position = position

    log.info(
        f"Цена: ${position.current_price:.2f} | "
        f"Диапазон: ${position.lower_price:.2f}—${position.upper_price:.2f} | "
        f"{'✅ в диапазоне' if position.in_range else '❌ вне диапазона'}"
    )

    # Цена внутри диапазона — всё хорошо
    if position.in_range:
        out_of_range_since = None
        return

    # Цена вышла за границу
    now = datetime.now()

    if out_of_range_since is None:
        # Первый раз фиксируем выход
        out_of_range_since = now
        log.warning(f"Цена вышла за границу! Жду {REBALANCE_DELAY_MIN} минут...")
        await tg.notify_out_of_range(position)
        return

    # Проверяем сколько времени прошло с момента выхода
    minutes_out = (now - out_of_range_since).total_seconds() / 60

    if minutes_out < REBALANCE_DELAY_MIN:
        log.info(f"Цена вне диапазона {minutes_out:.1f} мин из {REBALANCE_DELAY_MIN} мин")
        return

    # Прошло 20 минут — проверяем ещё раз актуальную цену
    current_price = await get_current_price()
    still_out = not (position.lower_price <= current_price <= position.upper_price)

    if not still_out:
        # Цена вернулась — отменяем ребаланс
        out_of_range_since = None
        position.current_price = current_price
        position.in_range = True
        tg.current_position = position
        log.info("Цена вернулась в диапазон, ребаланс отменён")
        await tg.notify_price_returned(position)
        return

    # Делаем ребаланс
    rebalance_in_progress = True
    out_of_range_since = None

    try:
        log.info("Начинаем ребаланс...")
        await tg.notify_rebalance_start(position)

        new_position = await rebalance(position)

        if new_position:
            tg.current_position = new_position
            await tg.notify_rebalance_complete(position, new_position)
            log.info(f"Ребаланс завершён. Новый диапазон: ${new_position.lower_price:.2f}—${new_position.upper_price:.2f}")
        else:
            await tg.notify_rebalance_error("Не удалось выполнить ребаланс")
            log.error("Ребаланс не удался")

    except Exception as e:
        log.exception(f"Ошибка при ребалансе: {e}")
        await tg.notify_rebalance_error(str(e))

    finally:
        rebalance_in_progress = False


async def heartbeat() -> None:
    """Отправляет heartbeat в Telegram каждые 4 часа."""
    if tg.current_position:
        await tg.send_heartbeat(tg.current_position)
        log.info("Heartbeat отправлен")


async def main() -> None:
    """Точка входа. Запускает бота."""
    log.info("=" * 50)
    log.info(f"Запуск бота | DRY RUN: {DRY_RUN}")
    log.info("=" * 50)

    # Загружаем позицию при старте
    position = await get_position()
    tg.current_position = position

    # Уведомляем о запуске
    await tg.notify_startup()

    # Настраиваем планировщик
    scheduler = AsyncIOScheduler()

    # Мониторинг каждые 5 минут
    scheduler.add_job(
        monitor_position,
        "interval",
        seconds=POLL_INTERVAL_SEC,
        id="monitor"
    )

    # Heartbeat каждые 4 часа
    scheduler.add_job(
        heartbeat,
        "interval",
        hours=HEARTBEAT_INTERVAL_HOURS,
        id="heartbeat"
    )

    scheduler.start()
    log.info(f"Планировщик запущен. Мониторинг каждые {POLL_INTERVAL_SEC} сек")

    # Запускаем Telegram бота для команды /status
    if not is_placeholder(TELEGRAM_BOT_TOKEN):
        app = tg.build_telegram_app()
        async with app:
            await app.start()
            await app.updater.start_polling()
            log.info("Telegram бот запущен, команда /status активна")

            # Бесконечный цикл
            try:
                while True:
                    await asyncio.sleep(1)
            except (KeyboardInterrupt, SystemExit):
                log.info("Остановка бота...")
            finally:
                await app.updater.stop()
                await app.stop()
    else:
        log.warning("Telegram токен не задан, /status недоступен")
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            log.info("Остановка бота...")

    scheduler.shutdown()
    log.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
