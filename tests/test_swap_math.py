"""Арифметика балансирующего свопа перед реоткрытием позиции.

Запуск:  venv/bin/python -m pytest tests/ -q -p no:anchorpy
(про -p no:anchorpy см. комментарий в test_rebalance_retry.py)

Зависимостей от Orca/Solana тут нет — orca_swap_math чистый, поэтому тесты
быстрые и не ходят в сеть.
"""
import math

from orca_swap_math import compute_swap_amount


def _after_swap(sol, usdc, price, reserve, direction, amount):
    """Состав кошелька после свопа и фактическая доля SOL в рабочих деньгах."""
    if direction == "SOL_TO_USDC":
        sol, usdc = sol - amount, usdc + amount * price
    elif direction == "USDC_TO_SOL":
        sol, usdc = sol + amount / price, usdc - amount
    net_sol_usd = (sol - reserve) * price
    total = net_sol_usd + usdc
    return net_sol_usd / total if total > 0 else float("nan")


def test_hits_the_requested_fraction():
    """Своп должен попадать именно в заданную долю, а не всегда в половину."""
    for target in (0.0, 0.27, 0.5, 0.73, 1.0):
        direction, amount = compute_swap_amount(
            sol_balance=1.0, usdc_balance=100.0, current_price=100.0,
            min_sol_reserve=0.1, target_sol_fraction=target,
        )
        got = _after_swap(1.0, 100.0, 100.0, 0.1, direction, amount)
        assert math.isclose(got, target, abs_tol=1e-9), f"target={target}, got={got}"


def test_live_incident_case():
    """Боевой случай 2026-07-28: при доле 0.73 нужен своп, при 0.5 казалось, что всё в порядке.

    Именно из-за жёстко зашитой половины позиция тогда переоткрылась на $87.61
    вместо ~$109, и около $21 осталось лежать на кошельке.
    """
    args = dict(sol_balance=0.818, usdc_balance=54.62,
                current_price=73.2185, min_sol_reserve=0.07)

    assert compute_swap_amount(**args, target_sol_fraction=0.5)[0] == "NONE"

    direction, amount = compute_swap_amount(**args, target_sol_fraction=0.73)
    assert direction == "USDC_TO_SOL" and amount > 0
    assert math.isclose(
        _after_swap(0.818, 54.62, 73.2185, 0.07, direction, amount), 0.73, abs_tol=1e-9
    )


def test_sol_below_reserve_buys_the_reserve_back():
    """SOL ниже резерва: докупать надо и до цели, и на сам резерв.

    Обрезка отрицательной величины нулём занижала бы своп вдвое от нехватки.
    """
    direction, amount = compute_swap_amount(
        sol_balance=0.0, usdc_balance=100.0, current_price=100.0,
        min_sol_reserve=0.1, target_sol_fraction=0.5,
    )
    assert direction == "USDC_TO_SOL"
    assert math.isclose(amount, 55.0, abs_tol=1e-9), "должно быть 55, а не 50"


def test_no_swap_when_pointless_or_impossible():
    below_threshold = compute_swap_amount(
        sol_balance=0.505, usdc_balance=49.0, current_price=100.0,
        min_sol_reserve=0.0, target_sol_fraction=0.5,
    )
    assert below_threshold[0] == "NONE", "мелкий перекос не стоит комиссии за своп"

    for bad in (
        dict(sol_balance=1.0, usdc_balance=100.0, current_price=0.0, min_sol_reserve=0.1),
        dict(sol_balance=0.0, usdc_balance=0.0, current_price=100.0, min_sol_reserve=0.1),
    ):
        assert compute_swap_amount(**bad, target_sol_fraction=0.5)[0] == "NONE"


def test_broken_fraction_falls_back_to_half():
    """NaN/inf/выход за [0,1] не должны ломать расчёт."""
    baseline = compute_swap_amount(
        sol_balance=1.0, usdc_balance=100.0, current_price=100.0,
        min_sol_reserve=0.1, target_sol_fraction=0.5,
    )
    assert compute_swap_amount(
        sol_balance=1.0, usdc_balance=100.0, current_price=100.0,
        min_sol_reserve=0.1, target_sol_fraction=float("nan"),
    ) == baseline

    direction, _ = compute_swap_amount(
        sol_balance=1.0, usdc_balance=100.0, current_price=100.0,
        min_sol_reserve=0.1, target_sol_fraction=5.0,
    )
    assert direction == "USDC_TO_SOL", "доля >1 должна прижаться к 1, а не улететь"
