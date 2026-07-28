"""Регрессия на инцидент 2026-07-28: ребаланс закрыл позицию, а открыть новую не смог.

Запуск:  venv/bin/python -m pytest tests/ -q -p no:anchorpy

`-p no:anchorpy` обязателен: плагин anchorpy импортирует `getrootdir` из
`pytest_xprocess`, а установленная версия pytest-xprocess держит его в
`xprocess.pytest_xprocess` — без отключения плагина сбор тестов падает.
Отключать автозагрузку целиком (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1) нельзя:
вместе с ней отключится pytest-asyncio, без которого эти тесты не работают.
"""
import asyncio
from dataclasses import dataclass

import pytest

import config
import main
import orca
import telegram_bot


pytestmark = pytest.mark.asyncio


@dataclass
class _DummyPosition:
    mint: str = "MINT"
    lower_price: float = 90.0
    upper_price: float = 110.0
    current_price: float = 100.0
    liquidity: int = 1
    fees_sol: float = 0.0
    fees_usdc: float = 0.0
    in_range: bool = True
    is_demo: bool = False


async def test_call_with_retry_retries_and_sleeps_exact(monkeypatch):
    sleeps = []

    async def _fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(orca.asyncio, "sleep", _fake_sleep)

    attempts = {"n": 0}

    async def _work():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ValueError("boom")
        return "OK"

    res = await orca._call_with_retry(lambda: _work(), what="unit-test", retries=4, base_delay=1.5)
    assert res == "OK"
    assert sleeps == [1.5, 3.0]


async def test_call_with_retry_raises_after_exhaustion(monkeypatch):
    sleeps = []

    async def _fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(orca.asyncio, "sleep", _fake_sleep)

    async def _always_fail():
        raise RuntimeError("always")

    with pytest.raises(RuntimeError, match="always"):
        await orca._call_with_retry(lambda: _always_fail(), what="unit-test", retries=3, base_delay=2.0)
    assert sleeps == [2.0, 4.0]


async def test_coroutine_reuse_demo_and_factory_guard():
    async def _sample():
        return 1

    coro = _sample()

    async def _naive_retry_reuse(c):
        # Первое await успешно, второе падает: нельзя переиспользовать корутину.
        await c
        await c

    with pytest.raises(RuntimeError) as e:
        await _naive_retry_reuse(coro)
    print("naive coroutine reuse error:", repr(e.value))

    coro2 = _sample()
    try:
        with pytest.raises(TypeError) as e2:
            await orca._call_with_retry(coro2, what="guard")
        print("call_with_retry guard error:", repr(e2.value))
    finally:
        # coro2 не await'ится (guard), закрываем, чтобы не было ResourceWarning.
        coro2.close()


async def test_rebalance_transient_429_succeeds(monkeypatch):
    # Ускоряем тест: без реальных sleep.
    async def _fake_sleep(_delay):
        return None

    monkeypatch.setattr(orca.asyncio, "sleep", _fake_sleep)

    monkeypatch.setattr(orca, "DRY_RUN", False)

    async def _close(_pos):
        return True

    async def _price():
        return 100.0

    async def _swap(_price):
        return True

    async def _compute(_price):
        return 10.0

    monkeypatch.setattr(orca, "close_position", _close)
    monkeypatch.setattr(orca, "get_current_price", _price)
    monkeypatch.setattr(orca, "_swap_to_balance", _swap)
    monkeypatch.setattr(orca, "_compute_reopen_usdc_amount", _compute)

    pending_values = []

    def _record_pending(v: bool):
        pending_values.append(v)
        config.REBALANCE_REOPEN_PENDING = v

    monkeypatch.setattr(orca, "set_rebalance_reopen_pending", _record_pending)

    import httpx

    calls = {"n": 0}
    mint_ids = []

    async def _open_position_stub(_price, usdc_amount=None, *, position_mint_keypair=None):
        assert usdc_amount == 10.0
        assert position_mint_keypair is not None
        mint_ids.append(id(position_mint_keypair))

        calls["n"] += 1
        if calls["n"] <= 2:
            req = httpx.Request("POST", "https://mainnet.helius-rpc.com/")
            resp = httpx.Response(429, request=req)
            raise httpx.HTTPStatusError("429 Too Many Requests", request=req, response=resp)

        # Успех: имитируем, что open_position сам снимает pending.
        _record_pending(False)
        return _DummyPosition(mint="NEW", current_price=100.0, lower_price=92.0, upper_price=108.0)

    monkeypatch.setattr(orca, "open_position", _open_position_stub)

    res = await orca.rebalance(_DummyPosition())
    assert res is not None
    assert calls["n"] == 3
    assert mint_ids[0] == mint_ids[1] == mint_ids[2]
    assert pending_values[0] is True
    assert pending_values[-1] is False


async def test_pending_flag_persisted_and_monitor_ticks_try_reopen(monkeypatch, tmp_path):
    # Пишем в реальный файл через set_key: проверяем персистентность.
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")

    monkeypatch.setattr(orca, "DRY_RUN", False)

    # Ускоряем тест: без реальных sleep.
    async def _fake_sleep(_delay):
        return None

    monkeypatch.setattr(orca.asyncio, "sleep", _fake_sleep)

    # Подменяем путь .env, чтобы не трогать настоящий.
    monkeypatch.setattr(orca, "find_dotenv", lambda: str(env_path))

    async def _close(_pos):
        return True

    async def _price():
        return 100.0

    async def _swap(_price):
        return True

    async def _compute(_price):
        return 10.0

    monkeypatch.setattr(orca, "close_position", _close)
    monkeypatch.setattr(orca, "get_current_price", _price)
    monkeypatch.setattr(orca, "_swap_to_balance", _swap)
    monkeypatch.setattr(orca, "_compute_reopen_usdc_amount", _compute)

    # open_position всегда падает -> retries исчерпаны -> исключение наружу,
    # но флаг должен быть уже выставлен и записан.
    async def _always_429(*_args, **_kwargs):
        raise RuntimeError("429")

    monkeypatch.setattr(orca, "open_position", _always_429)

    with pytest.raises(RuntimeError, match="429"):
        await orca.rebalance(_DummyPosition())

    env_text = env_path.read_text(encoding="utf-8")
    assert "REBALANCE_REOPEN_PENDING=true" in env_text
    assert config.REBALANCE_REOPEN_PENDING is True

    # Теперь имитируем следующий тик monitor_position: позиция None, pending=true, AUTO_REBALANCE=true
    monkeypatch.setattr(main, "AUTO_REBALANCE", True)

    async def _dummy_get_sol_balance():
        return 1.0

    async def _dummy_get_position():
        return None

    async def _dummy_get_current_price():
        return 100.0

    reopen_calls = {"n": 0}

    async def _dummy_reopen_after_failed_rebalance(_price):
        reopen_calls["n"] += 1
        # Успешное восстановление — снимаем флаг.
        orca.set_rebalance_reopen_pending(False)
        return _DummyPosition(mint="RECOVERED", current_price=100.0, lower_price=92.0, upper_price=108.0)

    monkeypatch.setattr(main, "get_sol_balance", _dummy_get_sol_balance)
    monkeypatch.setattr(main, "get_position", _dummy_get_position)
    monkeypatch.setattr(main, "get_current_price", _dummy_get_current_price)
    monkeypatch.setattr(main, "reopen_after_failed_rebalance", _dummy_reopen_after_failed_rebalance)

    # Telegram уведомления отключаем.
    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(main.tg, "notify_reopen_pending", _noop)
    monkeypatch.setattr(main.tg, "notify_reopen_recovered", _noop)
    monkeypatch.setattr(main.tg, "notify_position_lost", _noop)

    await main.monitor_position()
    assert reopen_calls["n"] == 1


async def test_withdraw_clears_pending_flag(monkeypatch):
    # Ставим флаг заранее
    calls = []

    def _set_pending(v: bool):
        calls.append(v)
        config.REBALANCE_REOPEN_PENDING = v

    monkeypatch.setattr(orca, "set_rebalance_reopen_pending", _set_pending)
    _set_pending(True)

    # Подменяем зависимости withdraw_command
    async def _dummy_get_position():
        return _DummyPosition(is_demo=False)

    async def _dummy_close_position(_pos):
        return True

    monkeypatch.setattr(telegram_bot, "_reply", lambda *_args, **_kwargs: asyncio.sleep(0))

    # withdraw_command импортирует orca.get_position/close_position внутри — патчим в модуле orca.
    monkeypatch.setattr(orca, "get_position", _dummy_get_position)
    monkeypatch.setattr(orca, "close_position", _dummy_close_position)

    class _DummyUpdate:
        effective_message = None

    class _DummyContext:
        args = ["confirm"]
        bot = None

    await telegram_bot.withdraw_command(_DummyUpdate(), _DummyContext())

    # Должен снять флаг (хотя бы один раз) и не выставлять обратно.
    assert False in calls
