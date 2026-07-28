from __future__ import annotations

import math


def compute_swap_amount(
    sol_balance: float,
    usdc_balance: float,
    current_price: float,
    min_sol_reserve: float,
    target_sol_fraction: float = 0.5,
) -> tuple[str, float]:
    """
    Чистая арифметика: определяет, нужно ли свопнуть кошелёк к целевому соотношению
    SOL/USDC (по USD) перед реоткрытием, и сколько именно.

    Возвращает (direction, amount):
    - direction: "SOL_TO_USDC", "USDC_TO_SOL" или "NONE"
    - amount: сумма входного токена в его натуральных единицах (SOL или USDC)
    """
    if current_price <= 0:
        return "NONE", 0.0

    # Нормализуем/ограничиваем долю, чтобы не взорваться на NaN/inf/выходе за диапазон.
    if not math.isfinite(target_sol_fraction):
        target_sol_fraction = 0.5
    target_sol_fraction = min(1.0, max(0.0, float(target_sol_fraction)))

    # ВАЖНО: net_sol_usd намеренно НЕ обрезается снизу нулём. Если SOL меньше
    # резерва (типично после выхода цены ВВЕРХ: позиция закрылась в 100% USDC,
    # а SOL ушёл на газ), то нам нужно докупить SOL не только до цели, но и на
    # сам резерв — отрицательное значение здесь это и выражает.
    # Обрезка до нуля занижала своп ровно на половину недостающего резерва:
    # при sol=0, usdc=$100 и резерве 0.1 SOL ($10) свопалось $50 вместо $55, и
    # после свопа рабочий SOL был $40 против $50 USDC — перекос $10, из-за
    # которого реоткрытие упиралось в нехватку SOL и часть денег оставалась
    # лежать зря. Не "упрощать" обратно.
    net_sol_usd = (sol_balance - min_sol_reserve) * current_price
    usable_usdc = max(0.0, usdc_balance)

    total_usable_usd = net_sol_usd + usable_usdc
    if total_usable_usd <= 0:
        return "NONE", 0.0

    target_sol_usd = total_usable_usd * target_sol_fraction
    delta_sol_usd = net_sol_usd - target_sol_usd  # >0: избыток SOL, <0: недостаток SOL
    diff_usd = abs(delta_sol_usd)
    threshold_usd = max(total_usable_usd * 0.02, 1.0)
    if diff_usd < threshold_usd:
        return "NONE", 0.0

    if delta_sol_usd > 0:
        swap_usd = delta_sol_usd
        # больше, чем реально свободно сверх резерва, свопнуть нельзя
        sol_to_swap = min(
            max(0.0, sol_balance - min_sol_reserve),
            swap_usd / current_price,
        )
        if sol_to_swap <= 0:
            return "NONE", 0.0
        return "SOL_TO_USDC", sol_to_swap

    usdc_to_swap = min(usable_usdc, -delta_sol_usd)
    if usdc_to_swap <= 0:
        return "NONE", 0.0
    return "USDC_TO_SOL", usdc_to_swap

