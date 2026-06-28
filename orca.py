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
from orca_whirlpool.utils import DecimalUtil, PDAUtil, PositionUtil, PriceMath

from config import (
    WHIRLPOOL_ADDRESS,
    POSITION_MINT,
    RANGE_WIDTH_PCT,
    DRY_RUN,
    DEMO_POSITION,
    is_placeholder,
    get_rpc_url,
)
from solana_client import get_client

log = logging.getLogger(__name__)

# Адреса SOL и USDC — для подписи fees в логах
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


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


async def _get_context(client: AsyncClient) -> WhirlpoolContext:
    """WhirlpoolContext без реального кошелька — только чтение аккаунтов."""
    return WhirlpoolContext(ORCA_WHIRLPOOL_PROGRAM_ID, client, Keypair())


async def _load_whirlpool(ctx: WhirlpoolContext):
    if is_placeholder(WHIRLPOOL_ADDRESS):
        raise ValueError("WHIRLPOOL_ADDRESS не задан в .env")
    return await ctx.fetcher.get_whirlpool(Pubkey.from_string(WHIRLPOOL_ADDRESS))


async def _pool_price_and_decimals(ctx: WhirlpoolContext, whirlpool) -> tuple[float, int, int, str, str]:
    """
    Возвращает (цена token_b за token_a, decimals_a, decimals_b, symbol_a, symbol_b).
    Для SOL/USDC: цена ≈ USDC за 1 SOL.
    """
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
    """Приводит fee_owed_a/b к (SOL, USDC) независимо от порядка токенов в пуле."""
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


def _demo_position(current_price: float) -> Position:
    """Демо-диапазон вокруг реальной цены — для dry-run без POSITION_MINT."""
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
    """Текущая цена пула с mainnet (on-chain sqrtPrice)."""
    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)
        price, _, _, sym_a, sym_b = await _pool_price_and_decimals(ctx, whirlpool)
        log.debug(
            "Цена пула %s/%s: %s (tick=%s, rpc=%s)",
            sym_a,
            sym_b,
            price,
            whirlpool.tick_current_index,
            get_rpc_url()[:40],
        )
        return price


async def get_position() -> Optional[Position]:
    """
    Читает LP-позицию с mainnet по POSITION_MINT (NFT mint).
    Если mint не задан и DEMO_POSITION=true — строит демо-диапазон вокруг реальной цены.
    """
    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)
        current_price, dec_a, dec_b, sym_a, sym_b = await _pool_price_and_decimals(ctx, whirlpool)

        if is_placeholder(POSITION_MINT):
            if DRY_RUN and DEMO_POSITION:
                pos = _demo_position(current_price)
                log.info(
                    "DEMO позиция (задай POSITION_MINT для реальной): "
                    "$%.2f, диапазон $%.2f—$%.2f",
                    current_price,
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
        in_range = status == PositionStatus.PriceIsInRange
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
            in_range=in_range,
            is_demo=False,
        )

        log.info(
            "Позиция %s/%s | цена $%.4f | диапазон $%.4f—$%.4f | %s",
            sym_a,
            sym_b,
            current_price,
            lower_price,
            upper_price,
            "в диапазоне" if in_range else "ВНЕ диапазона",
        )
        return position


async def collect_fees(position: Position) -> tuple[float, float]:
    """Сбор fees — в dry-run только лог."""
    if DRY_RUN:
        log.info(
            "DRY RUN: collect_fees — %.6f SOL + %.4f USDC",
            position.fees_sol,
            position.fees_usdc,
        )
        return position.fees_sol, position.fees_usdc

    raise NotImplementedError("Реальный сбор fees не реализован — включи DRY_RUN=true")


async def close_position(position: Position) -> bool:
    """Закрытие позиции — в dry-run только лог."""
    if DRY_RUN:
        label = position.mint[:8] if len(position.mint) > 8 else position.mint
        log.info("DRY RUN: close_position — %s", label)
        return True

    raise NotImplementedError("Реальное закрытие не реализовано — включи DRY_RUN=true")


async def open_position(current_price: float) -> Optional[Position]:
    """Открытие новой позиции ±RANGE_WIDTH_PCT — в dry-run симуляция."""
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
    """Полный цикл ребаланса: fees → close → open (симуляция в dry-run)."""
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
