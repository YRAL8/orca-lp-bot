"""
Чтение данных Orca Whirlpool с mainnet.
Транзакции (close/open/fees) — только симуляция при DRY_RUN=true.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient

from orca_whirlpool.constants import ORCA_WHIRLPOOL_PROGRAM_ID
from orca_whirlpool.context import WhirlpoolContext
from orca_whirlpool.internal.types.enums import PositionStatus
from orca_whirlpool.quote import (
    QuoteBuilder,
    DecreaseLiquidityQuoteParams,
    IncreaseLiquidityQuoteParams,
)
from orca_whirlpool.types import Percentage
from orca_whirlpool.utils import DecimalUtil, PDAUtil, PositionUtil, PriceMath

from config import (
    WHIRLPOOL_ADDRESS,
    POSITION_MINT,
    RANGE_WIDTH_PCT,
    DRY_RUN,
    DEMO_POSITION,
    DEMO_DEPOSIT_USD,
    is_placeholder,
    get_rpc_url,
)
from solana_client import get_client

log = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ZERO_SLIPPAGE = Percentage.from_fraction(0, 100)


@dataclass
class Position:
    """Текущая LP-позиция на Orca."""
    mint: str
    lower_price: float
    upper_price: float
    current_price: float
    liquidity: int
    fees_sol: float
    fees_usdc: float
    in_range: bool
    is_demo: bool = False
    # Состав позиции в токенах и USD
    amount_sol: float = 0.0
    amount_usdc: float = 0.0
    value_sol_usd: float = 0.0
    value_usdc_usd: float = 0.0
    total_value_usd: float = 0.0
    fees_total_usd: float = 0.0


async def _get_context(client: AsyncClient) -> WhirlpoolContext:
    return WhirlpoolContext(ORCA_WHIRLPOOL_PROGRAM_ID, client, Keypair())


async def _load_whirlpool(ctx: WhirlpoolContext):
    if is_placeholder(WHIRLPOOL_ADDRESS):
        raise ValueError("WHIRLPOOL_ADDRESS не задан в .env")
    return await ctx.fetcher.get_whirlpool(Pubkey.from_string(WHIRLPOOL_ADDRESS))


async def _pool_price_and_decimals(ctx: WhirlpoolContext, whirlpool) -> tuple[float, int, int, str, str]:
    mint_a = await ctx.fetcher.get_token_mint(whirlpool.token_mint_a)
    mint_b = await ctx.fetcher.get_token_mint(whirlpool.token_mint_b)

    price_decimal: Decimal = PriceMath.sqrt_price_x64_to_price(
        whirlpool.sqrt_price,
        mint_a.decimals,
        mint_b.decimals,
    )
    price = float(DecimalUtil.to_fixed(price_decimal, mint_b.decimals))

    symbol_a = _mint_symbol(str(whirlpool.token_mint_a))
    symbol_b = _mint_symbol(str(whirlpool.token_mint_b))
    return price, mint_a.decimals, mint_b.decimals, symbol_a, symbol_b


def _mint_symbol(mint: str) -> str:
    if mint == SOL_MINT:
        return "SOL"
    if mint == USDC_MINT:
        return "USDC"
    return mint[:4] + "…"


def _split_fees(
    whirlpool,
    fee_owed_a: int,
    fee_owed_b: int,
    decimals_a: int,
    decimals_b: int,
) -> tuple[float, float]:
    fees_sol = 0.0
    fees_usdc = 0.0

    if str(whirlpool.token_mint_a) == SOL_MINT:
        fees_sol = fee_owed_a / 10**decimals_a
        fees_usdc = fee_owed_b / 10**decimals_b
    elif str(whirlpool.token_mint_b) == SOL_MINT:
        fees_sol = fee_owed_b / 10**decimals_b
        fees_usdc = fee_owed_a / 10**decimals_a
    else:
        fees_sol = fee_owed_a / 10**decimals_a
        fees_usdc = fee_owed_b / 10**decimals_b

    return fees_sol, fees_usdc


def _amounts_to_sol_usdc(
    whirlpool,
    amount_a: int,
    amount_b: int,
    decimals_a: int,
    decimals_b: int,
    current_price: float,
) -> tuple[float, float, float, float, float]:
    """Конвертирует token_a/b в SOL, USDC и USD-стоимость."""
    amt_a = amount_a / 10**decimals_a
    amt_b = amount_b / 10**decimals_b

    if str(whirlpool.token_mint_a) == SOL_MINT:
        amount_sol, amount_usdc = amt_a, amt_b
    elif str(whirlpool.token_mint_b) == SOL_MINT:
        amount_sol, amount_usdc = amt_b, amt_a
    else:
        # Нестандартная пара — token_a в USD по текущей цене
        amount_sol, amount_usdc = amt_a, amt_b

    value_sol_usd = amount_sol * current_price
    value_usdc_usd = amount_usdc
    total_value_usd = value_sol_usd + value_usdc_usd
    return amount_sol, amount_usdc, value_sol_usd, value_usdc_usd, total_value_usd


def _amounts_from_liquidity(
    whirlpool,
    tick_lower_index: int,
    tick_upper_index: int,
    liquidity: int,
    decimals_a: int,
    decimals_b: int,
    current_price: float,
) -> tuple[float, float, float, float, float]:
    """Считает SOL/USDC в позиции по liquidity и диапазону тиков."""
    if liquidity <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    quote = QuoteBuilder.decrease_liquidity_by_liquidity(
        DecreaseLiquidityQuoteParams(
            liquidity=liquidity,
            tick_current_index=whirlpool.tick_current_index,
            sqrt_price=whirlpool.sqrt_price,
            tick_lower_index=tick_lower_index,
            tick_upper_index=tick_upper_index,
            slippage_tolerance=ZERO_SLIPPAGE,
        )
    )
    return _amounts_to_sol_usdc(
        whirlpool,
        quote.token_est_a,
        quote.token_est_b,
        decimals_a,
        decimals_b,
        current_price,
    )


def _demo_amounts_from_deposit(
    whirlpool,
    tick_lower_index: int,
    tick_upper_index: int,
    decimals_a: int,
    decimals_b: int,
    current_price: float,
    deposit_usd: float,
) -> tuple[float, float, float, float, float]:
    """
    Оценка состава демо-позиции: симулируем депозит deposit_usd через USDC.
    Точность ≈ Orca SDK; для реальных денег нужен POSITION_MINT.
    """
    if deposit_usd <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    input_mint = (
        whirlpool.token_mint_b
        if str(whirlpool.token_mint_b) == USDC_MINT
        else whirlpool.token_mint_a
    )
    usdc_decimals = decimals_b if str(whirlpool.token_mint_b) == USDC_MINT else decimals_a
    input_amount = int(deposit_usd / 2 * 10**usdc_decimals)

    quote = QuoteBuilder.increase_liquidity_by_input_token(
        IncreaseLiquidityQuoteParams(
            input_token_amount=input_amount,
            input_token_mint=input_mint,
            token_mint_a=whirlpool.token_mint_a,
            token_mint_b=whirlpool.token_mint_b,
            tick_current_index=whirlpool.tick_current_index,
            sqrt_price=whirlpool.sqrt_price,
            tick_lower_index=tick_lower_index,
            tick_upper_index=tick_upper_index,
            slippage_tolerance=ZERO_SLIPPAGE,
        )
    )
    return _amounts_to_sol_usdc(
        whirlpool,
        quote.token_est_a,
        quote.token_est_b,
        decimals_a,
        decimals_b,
        current_price,
    )


def _price_to_tick(price: float, decimals_a: int, decimals_b: int, tick_spacing: int) -> int:
    return PriceMath.price_to_initializable_tick_index(
        Decimal(str(price)),
        decimals_a,
        decimals_b,
        tick_spacing,
    )


def _fill_position_amounts(
    position: Position,
    whirlpool,
    tick_lower_index: int,
    tick_upper_index: int,
    decimals_a: int,
    decimals_b: int,
) -> Position:
    """Дополняет Position полями SOL/USDC и USD."""
    if position.is_demo:
        amounts = _demo_amounts_from_deposit(
            whirlpool,
            tick_lower_index,
            tick_upper_index,
            decimals_a,
            decimals_b,
            position.current_price,
            DEMO_DEPOSIT_USD,
        )
    else:
        amounts = _amounts_from_liquidity(
            whirlpool,
            tick_lower_index,
            tick_upper_index,
            position.liquidity,
            decimals_a,
            decimals_b,
            position.current_price,
        )

    position.amount_sol, position.amount_usdc, position.value_sol_usd, position.value_usdc_usd, position.total_value_usd = amounts
    position.fees_total_usd = (
        position.fees_sol * position.current_price + position.fees_usdc
    )
    return position


def _demo_position(current_price: float) -> Position:
    lower = current_price * (1 - RANGE_WIDTH_PCT / 100)
    upper = current_price * (1 + RANGE_WIDTH_PCT / 100)
    return Position(
        mint="DEMO",
        lower_price=lower,
        upper_price=upper,
        current_price=current_price,
        liquidity=0,
        fees_sol=0.0,
        fees_usdc=0.0,
        in_range=True,
        is_demo=True,
    )


async def get_current_price() -> float:
    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)
        price, _, _, _, _ = await _pool_price_and_decimals(ctx, whirlpool)
        return price


async def get_position() -> Optional[Position]:
    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)
        current_price, dec_a, dec_b, sym_a, sym_b = await _pool_price_and_decimals(ctx, whirlpool)

        if is_placeholder(POSITION_MINT):
            if DRY_RUN and DEMO_POSITION:
                pos = _demo_position(current_price)
                tick_lower = _price_to_tick(pos.lower_price, dec_a, dec_b, whirlpool.tick_spacing)
                tick_upper = _price_to_tick(pos.upper_price, dec_a, dec_b, whirlpool.tick_spacing)
                pos = _fill_position_amounts(pos, whirlpool, tick_lower, tick_upper, dec_a, dec_b)
                log.info(
                    "DEMO позиция ~$%.0f | SOL $%.2f + USDC $%.2f = $%.2f | диапазон $%.2f—$%.2f",
                    DEMO_DEPOSIT_USD,
                    pos.value_sol_usd,
                    pos.value_usdc_usd,
                    pos.total_value_usd,
                    pos.lower_price,
                    pos.upper_price,
                )
                return pos
            log.error("POSITION_MINT не задан — укажи NFT mint позиции в .env")
            return None

        position_mint = Pubkey.from_string(POSITION_MINT)
        position_pda = PDAUtil.get_position(ORCA_WHIRLPOOL_PROGRAM_ID, position_mint)
        on_chain = await ctx.fetcher.get_position(position_pda.pubkey)

        if on_chain is None:
            log.error("Позиция не найдена on-chain для mint %s", POSITION_MINT)
            return None

        lower_price = float(
            DecimalUtil.to_fixed(
                PriceMath.tick_index_to_price(on_chain.tick_lower_index, dec_a, dec_b),
                dec_b,
            )
        )
        upper_price = float(
            DecimalUtil.to_fixed(
                PriceMath.tick_index_to_price(on_chain.tick_upper_index, dec_a, dec_b),
                dec_b,
            )
        )

        status = PositionUtil.get_position_status(
            whirlpool.tick_current_index,
            on_chain.tick_lower_index,
            on_chain.tick_upper_index,
        )
        fees_sol, fees_usdc = _split_fees(
            whirlpool,
            on_chain.fee_owed_a,
            on_chain.fee_owed_b,
            dec_a,
            dec_b,
        )

        position = Position(
            mint=POSITION_MINT,
            lower_price=lower_price,
            upper_price=upper_price,
            current_price=current_price,
            liquidity=on_chain.liquidity,
            fees_sol=fees_sol,
            fees_usdc=fees_usdc,
            in_range=status == PositionStatus.PriceIsInRange,
            is_demo=False,
        )
        position = _fill_position_amounts(
            position,
            whirlpool,
            on_chain.tick_lower_index,
            on_chain.tick_upper_index,
            dec_a,
            dec_b,
        )

        log.info(
            "Позиция %s/%s | $%.2f (SOL $%.2f + USDC $%.2f) | цена $%.2f | %s",
            sym_a,
            sym_b,
            position.total_value_usd,
            position.value_sol_usd,
            position.value_usdc_usd,
            current_price,
            "в диапазоне" if position.in_range else "ВНЕ диапазона",
        )
        return position


async def collect_fees(position: Position) -> tuple[float, float]:
    if DRY_RUN:
        log.info(
            "DRY RUN: collect_fees — %.6f SOL + %.4f USDC (≈$%.2f)",
            position.fees_sol,
            position.fees_usdc,
            position.fees_total_usd,
        )
        return position.fees_sol, position.fees_usdc

    raise NotImplementedError("Реальный сбор fees не реализован — включи DRY_RUN=true")


async def close_position(position: Position) -> bool:
    if DRY_RUN:
        label = position.mint[:8] if len(position.mint) > 8 else position.mint
        log.info("DRY RUN: close_position — %s ($%.2f)", label, position.total_value_usd)
        return True

    raise NotImplementedError("Реальное закрытие не реализовано — включи DRY_RUN=true")


async def open_position(current_price: float) -> Optional[Position]:
    lower = current_price * (1 - RANGE_WIDTH_PCT / 100)
    upper = current_price * (1 + RANGE_WIDTH_PCT / 100)

    if DRY_RUN:
        log.info(
            "DRY RUN: open_position — диапазон $%.4f—$%.4f (±%.1f%%)",
            lower,
            upper,
            RANGE_WIDTH_PCT,
        )
        return Position(
            mint="DRY_RUN_NEW",
            lower_price=lower,
            upper_price=upper,
            current_price=current_price,
            liquidity=0,
            fees_sol=0.0,
            fees_usdc=0.0,
            in_range=True,
            is_demo=False,
        )

    raise NotImplementedError("Реальное открытие не реализовано — включи DRY_RUN=true")


async def rebalance(position: Position) -> Optional[Position]:
    log.info("Начинаем ребаланс%s...", " [DRY RUN]" if DRY_RUN else "")

    await collect_fees(position)

    if not await close_position(position):
        log.error("Не удалось закрыть позицию")
        return None

    current_price = await get_current_price()
    new_position = await open_position(current_price)
    if new_position is None:
        log.error("Не удалось открыть новую позицию")
        return None

    new_position.is_demo = position.is_demo
    log.info("Ребаланс завершён успешно")
    return new_position
