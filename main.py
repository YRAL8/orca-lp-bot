import asyncio
import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
import telegram_bot as tg
from orca import get_position, rebalance, get_current_price
from solana_client import get_sol_balance
from config import (
    POLL_INTERVAL_SEC, REBALANCE_DELAY_MIN,
    HEARTBEAT_INTERVAL_HOURS, DRY_RUN, AUTO_REBALANCE, MIN_SOL_BALANCE,
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
# httpx/httpcore/telegram на INFO логируют полные URL (с bot token / api-key).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# Раньше был обычный bool-флаг (rebalance_in_progress) — между его проверкой в начале
# monitor_position() и фактическим выставлением в True перед ребалансом происходит
# несколько await (get_sol_balance, get_position, get_current_price), т.е. в принципе
# небезопасное для параллельного входа окно. Live-прогон 2026-07-26 напечатал
# "Начинаем ребаланс..." дважды подряд — расследование показало, что это НЕ гонка:
# это две разные строчки лога (одна в monitor_position(), другая в orca.rebalance()
# — там текст "Начинаем ребаланс%s..." с DRY_RUN-суффиксом), которые совпадают
# по тексту только когда DRY_RUN=False. Реальной параллельности не было — только
# один close/open прошёл. asyncio.Lock здесь оставлен как более надёжный примитив
# взамен bool-флага (не полагается на порядок await), а не как фикс подтверждённого
# бага.
_rebalance_lock = asyncio.Lock()

# Время когда цена первый раз вышла за границу
out_of_range_since: datetime = None

# В режиме AUTO_REBALANCE=false: уже отправили «нужен ручной ребаланс»
# для текущего выхода из диапазона? Чтобы не повторять каждые 5 минут.
stale_alert_sent = False

# Уже отправили алерт о низком SOL? Чтобы не повторять каждые 5 минут,
# пока баланс не вернётся выше MIN_SOL_BALANCE.
low_sol_alert_sent = False

# Уже отправили алерт "позиция не найдена on-chain"? Чтобы не повторять
# каждые POLL_INTERVAL_SEC, пока позиция не найдётся снова.
position_lost_alert_sent = False

# Порог подряд идущих ошибок тиков мониторинга для алерта о недоступности RPC.
RPC_DOWN_ALERT_THRESHOLD_TICKS = 3

# Сколько тиков подряд упало из-за исключений (например, RPC/сеть).
rpc_error_streak = 0

# Анти-спам: уже отправляли алерт "RPC недоступен" для текущей серии ошибок?
rpc_down_alert_sent = False

# /pauza — авто-цикл (monitor_position) стоит, ручные команды (/rebalance,
# /addliquidity) по-прежнему работают. /stop — полная заморозка, ручные команды
# денежных действий тоже отключены. /boevoy снимает оба разом.
bot_paused = False
bot_frozen = False


def _reset_rpc_error_state() -> None:
    """Сбрасывает состояние RPC-ошибок после успешного тика мониторинга."""
    global rpc_error_streak, rpc_down_alert_sent
    rpc_error_streak = 0
    rpc_down_alert_sent = False


async def monitor_position() -> None:
    """
    Основной цикл мониторинга.
    Запускается каждые POLL_INTERVAL_SEC секунд.
    """
    global out_of_range_since, stale_alert_sent, low_sol_alert_sent, rpc_error_streak, rpc_down_alert_sent, position_lost_alert_sent

    if bot_paused or bot_frozen:
        log.info("Бот на паузе/заморожен (/pauza, /stop) — пропускаем тик мониторинга")
        return

    # Если уже идёт ребаланс — пропускаем. Проверка + вход в lock ниже идут без await
    # между ними, так что в однопоточном asyncio-цикле это безопасно (в отличие от
    # прежнего bool-флага, выставлявшегося только в конце после нескольких await).
    if _rebalance_lock.locked():
        log.info("Ребаланс в процессе, пропускаем мониторинг")
        return

    try:
        # Анти-спам по SOL: обновляем каждый тик, но мониторинг диапазона не блокируем.
        # (пропускаем, если кошелёк не настроен — read-only dry-run, balance is None)
        sol_balance = await get_sol_balance()
        if sol_balance is not None:
            if sol_balance < MIN_SOL_BALANCE:
                if not low_sol_alert_sent:
                    await tg.notify_low_sol_balance(sol_balance)
                    log.warning(f"Низкий баланс SOL: {sol_balance:.4f}")
                    low_sol_alert_sent = True
            else:
                low_sol_alert_sent = False

        # Читаем позицию
        position = await get_position()
        if position is None:
            log.error("Не удалось получить позицию")
            # Раньше здесь не было алерта вообще — мониторинг тихо выходил, а
            # heartbeat каждые HEARTBEAT_INTERVAL_HOURS продолжал слать
            # последнюю УСПЕШНУЮ позицию как актуальную (tg.current_position
            # не обнулялся), маскируя реальную проблему под "всё в порядке"
            # (найдено независимым аудитом, Opus 4.8, 2026-07-27). Теперь явно
            # алертим один раз и глушим heartbeat, пока позиция не найдётся снова.
            if not position_lost_alert_sent:
                try:
                    # config.POSITION_MINT (атрибут модуля), не from-import — иначе
                    # значение зафиксировалось бы на момент импорта и не видело бы
                    # обновлений, которые orca.py делает в рантайме через
                    # `config.POSITION_MINT = ...` (тот же класс бага, что уже был
                    # найден с локальными re-import'ами в setrange_command).
                    await tg.notify_position_lost(config.POSITION_MINT)
                    position_lost_alert_sent = True
                except Exception as notify_error:
                    log.exception("Не удалось отправить alert о потере позиции: %s", notify_error)
            tg.current_position = None
            _reset_rpc_error_state()
            return
        position_lost_alert_sent = False

        # Обновляем глобальную позицию для /status
        tg.current_position = position
        tg.price_history.append(position.current_price)

        log.info(
            f"Цена: ${position.current_price:.2f} | "
            f"Позиция: ${position.total_value_usd:.2f} "
            f"(SOL ${position.value_sol_usd:.2f} + USDC ${position.value_usdc_usd:.2f}) | "
            f"Диапазон: ${position.lower_price:.2f}—${position.upper_price:.2f} | "
            f"{'✅ в диапазоне' if position.in_range else '❌ вне диапазона'}"
        )

        # Цена внутри диапазона — всё хорошо
        if position.in_range:
            # Если до этого копился «стухший» алерт — сообщаем, что цена вернулась
            if out_of_range_since is not None:
                await tg.notify_price_returned(position)
                log.info("Цена вернулась в диапазон")
            out_of_range_since = None
            stale_alert_sent = False
            _reset_rpc_error_state()
            return

        # Цена вышла за границу
        now = datetime.now()

        if out_of_range_since is None:
            # Первый раз фиксируем выход
            out_of_range_since = now
            stale_alert_sent = False
            log.warning(f"Цена вышла за границу! Жду {REBALANCE_DELAY_MIN} минут...")
            await tg.notify_out_of_range(position)
            _reset_rpc_error_state()
            return

        # Проверяем сколько времени прошло с момента выхода
        minutes_out = (now - out_of_range_since).total_seconds() / 60

        if minutes_out < REBALANCE_DELAY_MIN:
            log.info(f"Цена вне диапазона {minutes_out:.1f} мин из {REBALANCE_DELAY_MIN} мин")
            _reset_rpc_error_state()
            return

        if not AUTO_REBALANCE:
            # Только наблюдение: сообщаем один раз, что пора вмешаться вручную,
            # и молчим дальше, пока цена сама не вернётся в диапазон (см. выше).
            # Реальный ребаланс (close/open позиции) ещё не реализован в orca.py.
            if not stale_alert_sent:
                await tg.notify_manual_action_needed(position)
                log.warning("Цена вне диапазона дольше %d мин — нужен ручной ребаланс (AUTO_REBALANCE=false)", REBALANCE_DELAY_MIN)
                stale_alert_sent = True
            _reset_rpc_error_state()
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
            _reset_rpc_error_state()
            return

        # Низкий SOL блокирует только реальное действие, не мониторинг.
        # out_of_range_since не трогаем — на следующем тике попробуем снова.
        if sol_balance is not None and sol_balance < MIN_SOL_BALANCE:
            if not low_sol_alert_sent:
                await tg.notify_low_sol_balance(sol_balance)
                low_sol_alert_sent = True
            log.warning(f"Низкий баланс SOL: {sol_balance:.4f} — ребаланс отложен")
            _reset_rpc_error_state()
            return

        # Делаем ребаланс
        out_of_range_since = None

        async with _rebalance_lock:
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

        _reset_rpc_error_state()
    except Exception as e:
        rpc_error_streak += 1
        log.exception("Ошибка тика мониторинга (streak=%d): %s", rpc_error_streak, e)
        if rpc_error_streak >= RPC_DOWN_ALERT_THRESHOLD_TICKS and not rpc_down_alert_sent:
            try:
                await tg.notify_rpc_down(rpc_error_streak)
                rpc_down_alert_sent = True
            except Exception as notify_error:
                log.exception("Не удалось отправить alert о недоступности RPC: %s", notify_error)


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
    try:
        position = await get_position()
        tg.current_position = position
    except Exception as e:
        tg.current_position = None
        log.exception("Не удалось получить позицию при старте: %s", e)

    # Уведомляем о запуске
    try:
        await tg.notify_startup()
    except Exception as e:
        log.exception("Не удалось отправить startup-уведомление: %s", e)

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
    telegram_polling_started = False
    if not is_placeholder(TELEGRAM_BOT_TOKEN):
        app = tg.build_telegram_app()
        try:
            await app.bot.get_me()
        except Exception as e:
            log.exception(
                "Telegram токен невалиден или API недоступен, работаем без /status: %s",
                e,
            )
        else:
            try:
                async with app:
                    await app.start()
                    await app.updater.start_polling()
                    telegram_polling_started = True
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
            except Exception as e:
                log.exception("Ошибка запуска Telegram, продолжаем без /status: %s", e)

    if is_placeholder(TELEGRAM_BOT_TOKEN) or not telegram_polling_started:
        if is_placeholder(TELEGRAM_BOT_TOKEN):
            log.warning("Telegram токен не задан, /status недоступен")
        else:
            log.warning("Telegram /status отключён из-за ошибки токена или API")
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            log.info("Остановка бота...")

    scheduler.shutdown()
    log.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
