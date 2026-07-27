"""
Чтение данных Orca Whirlpool с mainnet.
Транзакции (close/open/fees) — только симуляция при DRY_RUN=true.
"""
from __future__ import annotations

import asyncio
import logging

from dotenv import find_dotenv, set_key
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
    CollectFeesQuoteParams,
    DecreaseLiquidityQuoteParams,
    IncreaseLiquidityQuoteParams,
)
from orca_whirlpool.types import Percentage
from orca_whirlpool.utils import (
    DecimalUtil,
    PDAUtil,
    PositionUtil,
    PriceMath,
    TokenUtil,
    TickArrayUtil,
    TickUtil,
)
from orca_whirlpool.transaction import TransactionBuilder, Instruction
from orca_whirlpool.instruction import (
    WhirlpoolIx,
    OpenPositionWithMetadataParams,
    IncreaseLiquidityParams,
    InitializeTickArrayParams,
    DecreaseLiquidityParams,
    ClosePositionParams,
    CollectFeesParams,
    CollectRewardParams,
    UpdateFeesAndRewardsParams,
)
import config
from config import (
    WHIRLPOOL_ADDRESS,
    POSITION_MINT,
    DRY_RUN,
    DEMO_POSITION,
    DEMO_DEPOSIT_USD,
    is_placeholder,
    get_rpc_url,
)
from solana_client import get_client, get_wallet_keypair, get_sol_balance, get_usdc_balance

log = logging.getLogger(__name__)

# Зафиксированный демо-диапазон: создаётся один раз, пока не вызовут reset_demo_range().
_demo_range: dict | None = None  # lower_price, upper_price, tick_lower, tick_upper

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


async def _get_signature_status_with_retry(
    client: AsyncClient,
    signature,
    retries: int = 5,
    base_delay: float = 1.0,
):
    """
    client.get_signature_statuses() с retry+backoff — публичный RPC под нагрузкой
    (много реальных транзакций подряд) может ответить 429. Транзакция при этом уже
    реально отправлена (build_and_execute успел сделать send_raw_transaction) — сама
    отправка не откатывается из-за ошибки этой ПОСЛЕДУЮЩЕЙ проверки статуса, так что
    без retry мы теряем возможность узнать (и сохранить) результат уже случившейся
    транзакции (найдено вживую: реальный 429 во время автоматического триггера
    ребаланса, 2026-07-26).
    """
    last_error = None
    for attempt in range(retries):
        try:
            return await client.get_signature_statuses([signature])
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                delay = base_delay * (2 ** attempt)
                log.warning(
                    "get_signature_statuses не удался (попытка %d/%d): %s — повтор через %.1fс",
                    attempt + 1,
                    retries,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)
    raise last_error


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


async def _get_tick_for_index(
    ctx: WhirlpoolContext,
    whirlpool,
    whirlpool_pubkey: Pubkey,
    tick_index: int,
):
    start_tick_index = TickUtil.get_start_tick_index(
        tick_index,
        whirlpool.tick_spacing,
    )
    tick_array_pda = PDAUtil.get_tick_array(
        ORCA_WHIRLPOOL_PROGRAM_ID,
        whirlpool_pubkey,
        start_tick_index,
    )
    tick_array = await ctx.fetcher.get_tick_array(tick_array_pda.pubkey)
    if tick_array is None:
        raise ValueError(f"TickArray не найден для start_tick_index={start_tick_index}")
    return TickArrayUtil.get_tick_from_array(
        tick_array,
        tick_index,
        whirlpool.tick_spacing,
    )


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


def reset_demo_range() -> None:
    """Сбросить кэш демо-диапазона — следующий get_position() соберёт его заново."""
    global _demo_range
    _demo_range = None


def _demo_position(current_price: float, lower_price: float, upper_price: float) -> Position:
    return Position(
        mint="DEMO",
        lower_price=lower_price,
        upper_price=upper_price,
        current_price=current_price,
        liquidity=0,
        fees_sol=0.0,
        fees_usdc=0.0,
        in_range=lower_price <= current_price <= upper_price,
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
                global _demo_range
                if _demo_range is None:
                    raw_lower = current_price * (1 - config.RANGE_WIDTH_PCT / 100)
                    raw_upper = current_price * (1 + config.RANGE_WIDTH_PCT / 100)
                    tick_lower = _price_to_tick(
                        raw_lower, dec_a, dec_b, whirlpool.tick_spacing
                    )
                    tick_upper = _price_to_tick(
                        raw_upper, dec_a, dec_b, whirlpool.tick_spacing
                    )
                    # Align displayed range with ticks used for amounts (rounding can be asymmetric).
                    _demo_range = {
                        "lower_price": float(
                            DecimalUtil.to_fixed(
                                PriceMath.tick_index_to_price(tick_lower, dec_a, dec_b),
                                dec_b,
                            )
                        ),
                        "upper_price": float(
                            DecimalUtil.to_fixed(
                                PriceMath.tick_index_to_price(tick_upper, dec_a, dec_b),
                                dec_b,
                            )
                        ),
                        "tick_lower": tick_lower,
                        "tick_upper": tick_upper,
                    }
                pos = _demo_position(
                    current_price,
                    _demo_range["lower_price"],
                    _demo_range["upper_price"],
                )
                pos = _fill_position_amounts(
                    pos,
                    whirlpool,
                    _demo_range["tick_lower"],
                    _demo_range["tick_upper"],
                    dec_a,
                    dec_b,
                )
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
        fee_owed_a = on_chain.fee_owed_a
        fee_owed_b = on_chain.fee_owed_b
        try:
            whirlpool_pubkey = Pubkey.from_string(WHIRLPOOL_ADDRESS)
            tick_lower = await _get_tick_for_index(
                ctx,
                whirlpool,
                whirlpool_pubkey,
                on_chain.tick_lower_index,
            )
            tick_upper = await _get_tick_for_index(
                ctx,
                whirlpool,
                whirlpool_pubkey,
                on_chain.tick_upper_index,
            )
            fees_quote = QuoteBuilder.collect_fees(
                CollectFeesQuoteParams(
                    whirlpool=whirlpool,
                    position=on_chain,
                    tick_lower=tick_lower,
                    tick_upper=tick_upper,
                )
            )
            fee_owed_a = fees_quote.fee_a
            fee_owed_b = fees_quote.fee_b
        except Exception:
            log.exception(
                "Не удалось рассчитать актуальные on-chain fees quote, используем сырые fee_owed."
            )

        fees_sol, fees_usdc = _split_fees(
            whirlpool,
            fee_owed_a,
            fee_owed_b,
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

    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)

        wallet = get_wallet_keypair()
        wallet_pubkey = wallet.pubkey()
        position_mint = Pubkey.from_string(position.mint)
        whirlpool_pubkey = Pubkey.from_string(WHIRLPOOL_ADDRESS)
        position_pda = PDAUtil.get_position(ORCA_WHIRLPOOL_PROGRAM_ID, position_mint)
        on_chain = await ctx.fetcher.get_position(position_pda.pubkey)
        if on_chain is None:
            raise ValueError(f"Позиция не найдена on-chain для mint {position.mint}")

        position_token_account = TokenUtil.derive_ata(wallet_pubkey, position_mint)
        token_owner_a = await TokenUtil.resolve_or_create_ata(
            client,
            wallet_pubkey,
            whirlpool.token_mint_a,
            funder=wallet_pubkey,
            idempotent=True,
        )
        token_owner_b = await TokenUtil.resolve_or_create_ata(
            client,
            wallet_pubkey,
            whirlpool.token_mint_b,
            funder=wallet_pubkey,
            idempotent=True,
        )

        collect_fees_ix = WhirlpoolIx.collect_fees(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            CollectFeesParams(
                whirlpool=whirlpool_pubkey,
                position_authority=wallet_pubkey,
                position=position_pda.pubkey,
                position_token_account=position_token_account,
                token_owner_account_a=token_owner_a.pubkey,
                token_vault_a=whirlpool.token_vault_a,
                token_owner_account_b=token_owner_b.pubkey,
                token_vault_b=whirlpool.token_vault_b,
            ),
        )

        tx_builder = TransactionBuilder(client, wallet)
        tx_builder.set_compute_unit_limit(200_000)
        tx_builder.set_compute_unit_price(config.PRIORITY_FEE_MICROLAMPORTS)
        tx_builder.add_instruction(token_owner_a.instruction)
        tx_builder.add_instruction(token_owner_b.instruction)

        # collect_fees сам по себе только переносит уже записанный on-chain fee_owed —
        # он не пересчитывает его из накопленной fee_growth (это делает
        # update_fees_and_rewards). Без этого шага standalone collect_fees() уходил бы
        # впустую, если позиция не проходила decrease_liquidity с момента последнего
        # начисления (найдено независимым аудитом, Grok 4.5, 2026-07-26).
        if on_chain.liquidity > 0:
            tick_array_lower_pda = PDAUtil.get_tick_array(
                ORCA_WHIRLPOOL_PROGRAM_ID,
                whirlpool_pubkey,
                TickUtil.get_start_tick_index(on_chain.tick_lower_index, whirlpool.tick_spacing),
            )
            tick_array_upper_pda = PDAUtil.get_tick_array(
                ORCA_WHIRLPOOL_PROGRAM_ID,
                whirlpool_pubkey,
                TickUtil.get_start_tick_index(on_chain.tick_upper_index, whirlpool.tick_spacing),
            )
            update_fees_ix = WhirlpoolIx.update_fees_and_rewards(
                ORCA_WHIRLPOOL_PROGRAM_ID,
                UpdateFeesAndRewardsParams(
                    whirlpool=whirlpool_pubkey,
                    position=position_pda.pubkey,
                    tick_array_lower=tick_array_lower_pda.pubkey,
                    tick_array_upper=tick_array_upper_pda.pubkey,
                ),
            )
            tx_builder.add_instruction(update_fees_ix)

        tx_builder.add_instruction(collect_fees_ix)

        log.info(
            "Prepared REAL collect_fees tx (not sent): mint=%s position_pda=%s whirlpool=%s",
            position.mint,
            position_pda.pubkey,
            whirlpool_pubkey,
        )
        log.info(
            "Prepared addresses: wallet=%s pos_token_ata=%s owner_ata_a=%s owner_ata_b=%s",
            wallet_pubkey,
            position_token_account,
            token_owner_a.pubkey,
            token_owner_b.pubkey,
        )
        log.info(
            "On-chain fees before collect: fee_owed_a=%d fee_owed_b=%d",
            on_chain.fee_owed_a,
            on_chain.fee_owed_b,
        )

        signature = await tx_builder.build_and_execute()
        status_resp = await _get_signature_status_with_retry(client, signature)
        status = status_resp.value[0]
        if status is None or status.err is not None:
            raise RuntimeError(
                f"Транзакция collect_fees не удалась on-chain: signature={signature} "
                f"err={status.err if status else 'нет статуса'}"
            )
        log.info("collect_fees успешно отправлена и подтверждена: %s", signature)

        return position.fees_sol, position.fees_usdc


async def close_position(position: Position) -> bool:
    if DRY_RUN:
        label = position.mint[:8] if len(position.mint) > 8 else position.mint
        log.info("DRY RUN: close_position — %s ($%.2f)", label, position.total_value_usd)
        return True

    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)

        wallet = get_wallet_keypair()
        wallet_pubkey = wallet.pubkey()
        position_mint = Pubkey.from_string(position.mint)
        whirlpool_pubkey = Pubkey.from_string(WHIRLPOOL_ADDRESS)
        position_pda = PDAUtil.get_position(ORCA_WHIRLPOOL_PROGRAM_ID, position_mint)
        on_chain = await ctx.fetcher.get_position(position_pda.pubkey)
        if on_chain is None:
            log.error("Позиция не найдена on-chain для mint %s", position.mint)
            return False

        position_token_account = TokenUtil.derive_ata(wallet_pubkey, position_mint)
        token_owner_a = await TokenUtil.resolve_or_create_ata(
            client,
            wallet_pubkey,
            whirlpool.token_mint_a,
            funder=wallet_pubkey,
            idempotent=True,
        )
        token_owner_b = await TokenUtil.resolve_or_create_ata(
            client,
            wallet_pubkey,
            whirlpool.token_mint_b,
            funder=wallet_pubkey,
            idempotent=True,
        )

        tick_array_lower_pda = PDAUtil.get_tick_array(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            whirlpool_pubkey,
            TickUtil.get_start_tick_index(on_chain.tick_lower_index, whirlpool.tick_spacing),
        )
        tick_array_upper_pda = PDAUtil.get_tick_array(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            whirlpool_pubkey,
            TickUtil.get_start_tick_index(on_chain.tick_upper_index, whirlpool.tick_spacing),
        )

        tx_builder = TransactionBuilder(client, wallet)
        tx_builder.set_compute_unit_limit(400_000)
        tx_builder.set_compute_unit_price(config.PRIORITY_FEE_MICROLAMPORTS)
        tx_builder.add_instruction(token_owner_a.instruction)
        tx_builder.add_instruction(token_owner_b.instruction)

        decrease_quote = None
        if on_chain.liquidity > 0:
            decrease_quote = QuoteBuilder.decrease_liquidity_by_liquidity(
                DecreaseLiquidityQuoteParams(
                    liquidity=on_chain.liquidity,
                    tick_current_index=whirlpool.tick_current_index,
                    sqrt_price=whirlpool.sqrt_price,
                    tick_lower_index=on_chain.tick_lower_index,
                    tick_upper_index=on_chain.tick_upper_index,
                    # 20%, не 1%: для позиции в диапазоне 1% на token-суммы даёт
                    # ценовой допуск всего ~±0.08% (концентрированная ликвидность
                    # "рычагует" чувствительность суммы к цене ~13x) — почти
                    # гарантированный отказ на нормальном дрейфе цены за секунды
                    # между расчётом котировки и подтверждением в блоке (найдено
                    # независимым аудитом, Opus 4.8, 2026-07-26, с расчётом).
                    # Пробовал полностью убрать защиту (0%) — тоже упало в реальной
                    # simulateTransaction с той же TokenMinSubceeded, т.к. нулевой
                    # допуск не прощает вообще никакого дрейфа. 20% ~ ±1.6% по цене —
                    # разумный запас для выхода из своей же позиции (это не своп,
                    # sandwich неприменим — token_min тут не защита от атаки, а просто
                    # защита от грубой ошибки в расчёте).
                    slippage_tolerance=Percentage.from_fraction(20, 100),
                )
            )
            decrease_liquidity_ix = WhirlpoolIx.decrease_liquidity(
                ORCA_WHIRLPOOL_PROGRAM_ID,
                DecreaseLiquidityParams(
                    liquidity_amount=decrease_quote.liquidity,
                    token_min_a=decrease_quote.token_min_a,
                    token_min_b=decrease_quote.token_min_b,
                    whirlpool=whirlpool_pubkey,
                    position_authority=wallet_pubkey,
                    position=position_pda.pubkey,
                    position_token_account=position_token_account,
                    token_owner_account_a=token_owner_a.pubkey,
                    token_owner_account_b=token_owner_b.pubkey,
                    token_vault_a=whirlpool.token_vault_a,
                    token_vault_b=whirlpool.token_vault_b,
                    tick_array_lower=tick_array_lower_pda.pubkey,
                    tick_array_upper=tick_array_upper_pda.pubkey,
                ),
            )
            tx_builder.add_instruction(decrease_liquidity_ix)

        collect_fees_ix = WhirlpoolIx.collect_fees(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            CollectFeesParams(
                whirlpool=whirlpool_pubkey,
                position_authority=wallet_pubkey,
                position=position_pda.pubkey,
                position_token_account=position_token_account,
                token_owner_account_a=token_owner_a.pubkey,
                token_vault_a=whirlpool.token_vault_a,
                token_owner_account_b=token_owner_b.pubkey,
                token_vault_b=whirlpool.token_vault_b,
            ),
        )
        close_position_ix = WhirlpoolIx.close_position(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            ClosePositionParams(
                position_authority=wallet_pubkey,
                receiver=wallet_pubkey,
                position=position_pda.pubkey,
                position_mint=position_mint,
                position_token_account=position_token_account,
            ),
        )
        tx_builder.add_instruction(collect_fees_ix)

        # Whirlpool отклонит close_position, если у позиции остался неполученный
        # reward (is_position_empty требует liquidity==0 И fee_owed==0 И
        # reward.amount_owed==0 для всех инициализированных слотов). У этого пула
        # инициализирован reward[0] (ORCA), сейчас emissions=0 и amount_owed=0 — но
        # проверяем явно, а не полагаемся на текущее состояние (найдено независимыми
        # аудитами, Grok 4.5 и GPT-5.2, 2026-07-26).
        for reward_index, reward_info in enumerate(on_chain.reward_infos):
            if reward_info.amount_owed <= 0:
                continue
            reward_mint = whirlpool.reward_infos[reward_index].mint
            reward_owner = await TokenUtil.resolve_or_create_ata(
                client,
                wallet_pubkey,
                reward_mint,
                funder=wallet_pubkey,
                idempotent=True,
            )
            tx_builder.add_instruction(reward_owner.instruction)
            collect_reward_ix = WhirlpoolIx.collect_reward(
                ORCA_WHIRLPOOL_PROGRAM_ID,
                CollectRewardParams(
                    reward_index=reward_index,
                    whirlpool=whirlpool_pubkey,
                    position_authority=wallet_pubkey,
                    position=position_pda.pubkey,
                    position_token_account=position_token_account,
                    reward_owner_account=reward_owner.pubkey,
                    reward_vault=whirlpool.reward_infos[reward_index].vault,
                ),
            )
            tx_builder.add_instruction(collect_reward_ix)
            log.info(
                "Reward %d owed=%d — добавлена collect_reward перед close_position",
                reward_index,
                reward_info.amount_owed,
            )

        tx_builder.add_instruction(close_position_ix)

        log.info(
            "Prepared REAL close_position tx (not sent): mint=%s position_pda=%s whirlpool=%s",
            position.mint,
            position_pda.pubkey,
            whirlpool_pubkey,
        )
        log.info(
            "Prepared addresses: wallet=%s pos_token_ata=%s owner_ata_a=%s owner_ata_b=%s "
            "tick_array_lower=%s tick_array_upper=%s",
            wallet_pubkey,
            position_token_account,
            token_owner_a.pubkey,
            token_owner_b.pubkey,
            tick_array_lower_pda.pubkey,
            tick_array_upper_pda.pubkey,
        )
        if decrease_quote is not None:
            log.info(
                "Prepared decrease_liquidity: liquidity=%d token_min_a=%d token_min_b=%d",
                decrease_quote.liquidity,
                decrease_quote.token_min_a,
                decrease_quote.token_min_b,
            )
        else:
            log.info("On-chain liquidity already zero: skip decrease_liquidity")

        signature = await tx_builder.build_and_execute()
        status_resp = await _get_signature_status_with_retry(client, signature)
        status = status_resp.value[0]
        if status is None or status.err is not None:
            raise RuntimeError(
                f"Транзакция close_position не удалась on-chain: signature={signature} "
                f"err={status.err if status else 'нет статуса'}"
            )
        log.info("close_position успешно отправлена и подтверждена: %s", signature)

        # Сбрасываем POSITION_MINT в .env — позиции больше нет on-chain, иначе монитор
        # каждые 5 минут молча логирует ошибку "не найдено", без алерта в Telegram
        # (тот же класс проблемы, что и при open — Opus 4.8, 2026-07-26). Если это
        # часть rebalance(), следующий сразу за этим open_position() перезапишет
        # значение новым mint'ом.
        # In-memory обновляем ВСЕГДА (это то, чем реально пользуется живой процесс:
        # get_position()/monitor_position() читают модульный POSITION_MINT, а не файл).
        # Раньше это было вложено внутрь "if dotenv_path" — в Docker-контейнере
        # find_dotenv() всегда возвращает пусто (docker-compose передаёт .env как
        # переменные окружения, не монтирует сам файл), так что in-memory обновление
        # никогда не срабатывало на сервере — бот считал позицию старой/закрытой
        # даже после успешного реального ребаланса (поймано вживую через /rebalance
        # на сервере, 2026-07-26).
        global POSITION_MINT
        POSITION_MINT = "YOUR_POSITION_NFT_MINT"
        config.POSITION_MINT = "YOUR_POSITION_NFT_MINT"

        dotenv_path = find_dotenv()
        if dotenv_path:
            set_key(dotenv_path, "POSITION_MINT", "YOUR_POSITION_NFT_MINT", quote_mode="never")
            log.info("POSITION_MINT сброшен в .env после закрытия позиции")
        else:
            log.warning(
                "Не удалось найти .env файл на диске (ожидаемо в Docker) — "
                "POSITION_MINT сброшен только в памяти процесса, не в файле"
            )

        return True


# Запас сверх точного расчёта при определении суммы реоткрытия: оценка здесь и реальная
# котировка внутри open_position() разнесены по времени и RPC-вызовам (цена/тик могут
# успеть сдвинуться), плюс страхует любую остаточную ошибку округления float-баланса.
# Найдено независимо всеми тремя аудитами (Grok 4.5, GPT-5.2, Opus 4.8, 2026-07-26):
# без запаса подобранная здесь сумма могла требовать чуть больше SOL/USDC, чем реально
# осталось на кошельке к моменту фактической отправки.
_REOPEN_SAFETY_HAIRCUT = 0.98  # 2%


async def _compute_reopen_usdc_amount(current_price: float) -> float:
    sol_balance = await get_sol_balance()
    usdc_balance = await get_usdc_balance()
    if sol_balance is None or usdc_balance is None:
        log.warning("Кошелёк не настроен: невозможно вычислить сумму реоткрытия, вернём 0")
        return 0.0

    # MIN_SOL_BALANCE — это порог АЛЕРТА ("мало SOL"), а не резерв под ренту открытия.
    # Ренту НОВОЙ позиции (PDA + mint + ATA + Metaplex metadata, ~0.012-0.015 SOL по факту
    # сегодняшних реальных открытий) нужно резервировать ОТДЕЛЬНО, иначе после реоткрытия
    # баланс уйдёт ниже MIN_SOL_BALANCE сразу же, а на пограничных кошельках сама
    # транзакция открытия может не хватить SOL на ренту сверх суммы депозита (найдено
    # независимо всеми тремя аудитами, 2026-07-26).
    usable_sol_lamports = max(
        0,
        int(
            (sol_balance - config.MIN_SOL_BALANCE - config.OPEN_POSITION_RENT_RESERVE_SOL)
            * 1_000_000_000
        ),
    )
    if usable_sol_lamports <= 0:
        log.warning(
            "Свободный SOL после резерва MIN_SOL_BALANCE+OPEN_POSITION_RENT_RESERVE_SOL "
            "отсутствует: balance=%.9f",
            sol_balance,
        )
        return 0.0

    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)
        _, dec_a, dec_b, _, _ = await _pool_price_and_decimals(ctx, whirlpool)

        raw_lower = current_price * (1 - config.RANGE_WIDTH_PCT / 100)
        raw_upper = current_price * (1 + config.RANGE_WIDTH_PCT / 100)
        tick_lower = _price_to_tick(raw_lower, dec_a, dec_b, whirlpool.tick_spacing)
        tick_upper = _price_to_tick(raw_upper, dec_a, dec_b, whirlpool.tick_spacing)

        usdc_mint = Pubkey.from_string(USDC_MINT)
        sol_mint = Pubkey.from_string(SOL_MINT)

        if whirlpool.token_mint_a == usdc_mint:
            usdc_decimals = dec_a
        elif whirlpool.token_mint_b == usdc_mint:
            usdc_decimals = dec_b
        else:
            raise ValueError("Whirlpool не содержит USDC mint, расчёт суммы реоткрытия невозможен")

        usdc_input_units = round(usdc_balance * 10**usdc_decimals)
        if usdc_input_units <= 0:
            log.warning("Баланс USDC нулевой: нечего реинвестировать после закрытия")
            return 0.0

        # Тот же slippage (1%), что использует open_position() для реальной транзакции —
        # раньше здесь стоял ZERO_SLIPPAGE, из-за чего token_max в оценке был занижен
        # относительно того, что open_position() реально запросит (найдено независимо
        # всеми тремя аудитами, 2026-07-26).
        estimate_slippage = Percentage.from_fraction(1, 100)

        usdc_limited_quote = QuoteBuilder.increase_liquidity_by_input_token(
            IncreaseLiquidityQuoteParams(
                input_token_amount=usdc_input_units,
                input_token_mint=usdc_mint,
                token_mint_a=whirlpool.token_mint_a,
                token_mint_b=whirlpool.token_mint_b,
                tick_current_index=whirlpool.tick_current_index,
                sqrt_price=whirlpool.sqrt_price,
                tick_lower_index=tick_lower,
                tick_upper_index=tick_upper,
                slippage_tolerance=estimate_slippage,
            )
        )

        if usdc_limited_quote.liquidity <= 0:
            # Диапазон не покрывает текущую цену (рассинхрон между отдельными RPC-вызовами
            # current_price/whirlpool, или округление тиков) — не пытаемся реоткрыть на
            # заведомо нулевой ликвидности (найдено независимо всеми тремя аудитами).
            log.warning(
                "Нулевая ликвидность в оценке реоткрытия (диапазон $%.4f-$%.4f не "
                "покрывает текущую цену) — реоткрытие отменено",
                raw_lower,
                raw_upper,
            )
            return 0.0

        if whirlpool.token_mint_a == sol_mint:
            required_sol_lamports = usdc_limited_quote.token_max_a
        elif whirlpool.token_mint_b == sol_mint:
            required_sol_lamports = usdc_limited_quote.token_max_b
        else:
            raise ValueError("Whirlpool не содержит SOL mint, расчёт суммы реоткрытия невозможен")

        if required_sol_lamports <= usable_sol_lamports:
            log.info(
                "Реоткрытие ограничено USDC: требуется SOL=%d lamports, доступно SOL=%d lamports",
                required_sol_lamports,
                usable_sol_lamports,
            )
            return usdc_balance * _REOPEN_SAFETY_HAIRCUT

        sol_limited_quote = QuoteBuilder.increase_liquidity_by_input_token(
            IncreaseLiquidityQuoteParams(
                input_token_amount=usable_sol_lamports,
                input_token_mint=sol_mint,
                token_mint_a=whirlpool.token_mint_a,
                token_mint_b=whirlpool.token_mint_b,
                tick_current_index=whirlpool.tick_current_index,
                sqrt_price=whirlpool.sqrt_price,
                tick_lower_index=tick_lower,
                tick_upper_index=tick_upper,
                slippage_tolerance=estimate_slippage,
            )
        )

        if sol_limited_quote.liquidity <= 0:
            log.warning(
                "Нулевая ликвидность в SOL-ограниченной оценке реоткрытия — реоткрытие отменено"
            )
            return 0.0

        if whirlpool.token_mint_a == usdc_mint:
            usdc_units_by_sol_limit = sol_limited_quote.token_max_a
        else:
            usdc_units_by_sol_limit = sol_limited_quote.token_max_b

        usdc_amount_by_sol_limit = usdc_units_by_sol_limit / 10**usdc_decimals
        log.info(
            "Реоткрытие ограничено SOL: требуется SOL=%d lamports, доступно SOL=%d lamports, "
            "допустимый USDC=%.6f",
            required_sol_lamports,
            usable_sol_lamports,
            usdc_amount_by_sol_limit,
        )
        return max(0.0, usdc_amount_by_sol_limit * _REOPEN_SAFETY_HAIRCUT)


async def open_position(current_price: float, usdc_amount: Optional[float] = None) -> Optional[Position]:
    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)
        _, dec_a, dec_b, _, _ = await _pool_price_and_decimals(ctx, whirlpool)

        raw_lower = current_price * (1 - config.RANGE_WIDTH_PCT / 100)
        raw_upper = current_price * (1 + config.RANGE_WIDTH_PCT / 100)
        tick_lower = _price_to_tick(raw_lower, dec_a, dec_b, whirlpool.tick_spacing)
        tick_upper = _price_to_tick(raw_upper, dec_a, dec_b, whirlpool.tick_spacing)
        # Align range with initializable ticks (rounding can be asymmetric).
        lower = float(
            DecimalUtil.to_fixed(
                PriceMath.tick_index_to_price(tick_lower, dec_a, dec_b),
                dec_b,
            )
        )
        upper = float(
            DecimalUtil.to_fixed(
                PriceMath.tick_index_to_price(tick_upper, dec_a, dec_b),
                dec_b,
            )
        )

        if DRY_RUN:
            log.info(
                "DRY RUN: open_position — диапазон $%.4f—$%.4f (±%.1f%%)",
                lower,
                upper,
                config.RANGE_WIDTH_PCT,
            )
            # Синхронизируем демо-кэш с только что "открытой" позицией — иначе
            # следующий get_position() в demo-режиме вернёт старый диапазон,
            # будто ребаланса не было (найдено при аудите 2026-07-26).
            global _demo_range
            _demo_range = {
                "lower_price": lower,
                "upper_price": upper,
                "tick_lower": tick_lower,
                "tick_upper": tick_upper,
            }
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

        wallet = get_wallet_keypair()
        wallet_pubkey = wallet.pubkey()
        whirlpool_pubkey = Pubkey.from_string(WHIRLPOOL_ADDRESS)
        real_slippage = Percentage.from_fraction(1, 100)

        usdc_mint = Pubkey.from_string(USDC_MINT)
        if whirlpool.token_mint_a == usdc_mint:
            usdc_decimals = dec_a
        elif whirlpool.token_mint_b == usdc_mint:
            usdc_decimals = dec_b
        else:
            raise ValueError("Whirlpool не содержит USDC mint, OPEN_POSITION_USDC_AMOUNT неприменим")

        usdc_amount_for_open = (
            usdc_amount if usdc_amount is not None else config.OPEN_POSITION_USDC_AMOUNT
        )
        if usdc_amount_for_open <= 0:
            raise ValueError("Сумма USDC для открытия позиции должна быть > 0")

        # round(), не int() — float*10**decimals может дать 1999999.9999... для "ровных" сумм
        # (найдено независимым аудитом, GPT-5.2, 2026-07-26).
        input_amount_usdc = round(usdc_amount_for_open * 10**usdc_decimals)
        quote = QuoteBuilder.increase_liquidity_by_input_token(
            IncreaseLiquidityQuoteParams(
                input_token_amount=input_amount_usdc,
                input_token_mint=usdc_mint,
                token_mint_a=whirlpool.token_mint_a,
                token_mint_b=whirlpool.token_mint_b,
                tick_current_index=whirlpool.tick_current_index,
                sqrt_price=whirlpool.sqrt_price,
                tick_lower_index=tick_lower,
                tick_upper_index=tick_upper,
                slippage_tolerance=real_slippage,
            )
        )

        if quote.liquidity <= 0:
            # current_price аргумент и цена пула на момент котировки могут разойтись
            # (отдельные RPC-вызовы) — тогда диапазон уже вне цены, и мы открыли бы
            # пустую позицию, впустую потратив ренту (найдено независимым аудитом,
            # Opus 4.8, 2026-07-26).
            raise ValueError(
                f"Расчётная ликвидность равна нулю (диапазон ${lower:.4f}-${upper:.4f} "
                f"не покрывает текущую цену пула) — открытие позиции отменено"
            )

        token_max_a = quote.token_max_a
        token_max_b = quote.token_max_b
        token_est_a = quote.token_est_a
        token_est_b = quote.token_est_b

        sol_mint = Pubkey.from_string(SOL_MINT)
        if whirlpool.token_mint_a == sol_mint:
            wrapped_sol_lamports = token_max_a
            usdc_units_for_deposit = token_max_b
        elif whirlpool.token_mint_b == sol_mint:
            wrapped_sol_lamports = token_max_b
            usdc_units_for_deposit = token_max_a
        else:
            raise ValueError("Whirlpool не содержит SOL mint, wrapped SOL счёт не может быть создан")

        position_mint_keypair = Keypair()
        position_mint = position_mint_keypair.pubkey()
        position_pda = PDAUtil.get_position(ORCA_WHIRLPOOL_PROGRAM_ID, position_mint)
        metadata_pda = PDAUtil.get_position_metadata(position_mint)

        token_owner_a = await TokenUtil.resolve_or_create_ata(
            client,
            wallet_pubkey,
            whirlpool.token_mint_a,
            wrapped_sol_amount=wrapped_sol_lamports if whirlpool.token_mint_a == sol_mint else 0,
            funder=wallet_pubkey,
            idempotent=True,
        )
        token_owner_b = await TokenUtil.resolve_or_create_ata(
            client,
            wallet_pubkey,
            whirlpool.token_mint_b,
            wrapped_sol_amount=wrapped_sol_lamports if whirlpool.token_mint_b == sol_mint else 0,
            funder=wallet_pubkey,
            idempotent=True,
        )

        # position_mint ещё не существует on-chain — его создаёт (is_signer=True) сама
        # инструкция open_position_with_metadata, и она же создаёт position_token_account
        # через CPI в ASSOCIATED_TOKEN_PROGRAM_ID (подтверждено чтением исходников
        # anchorpy-обвязки: accounts включают ASSOCIATED_TOKEN_PROGRAM_ID). Отдельная
        # create_associated_token_account здесь упала бы (mint ещё не создан) и вдобавок
        # была бы дублирующей — нашёл независимый аудит (Grok 4.5) 2026-07-26, подтвердил
        # чтением исходников.
        position_token_account = TokenUtil.derive_ata(wallet_pubkey, position_mint)

        lower_start_tick = TickUtil.get_start_tick_index(tick_lower, whirlpool.tick_spacing)
        upper_start_tick = TickUtil.get_start_tick_index(tick_upper, whirlpool.tick_spacing)
        tick_array_lower_pda = PDAUtil.get_tick_array(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            whirlpool_pubkey,
            lower_start_tick,
        )
        tick_array_upper_pda = PDAUtil.get_tick_array(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            whirlpool_pubkey,
            upper_start_tick,
        )

        tick_array_lower_onchain = await ctx.fetcher.get_tick_array(tick_array_lower_pda.pubkey)
        tick_array_upper_onchain = await ctx.fetcher.get_tick_array(tick_array_upper_pda.pubkey)

        maybe_init_tick_lower_ix: Instruction | None = None
        maybe_init_tick_upper_ix: Instruction | None = None
        if tick_array_lower_onchain is None:
            maybe_init_tick_lower_ix = WhirlpoolIx.initialize_tick_array(
                ORCA_WHIRLPOOL_PROGRAM_ID,
                InitializeTickArrayParams(
                    start_tick_index=lower_start_tick,
                    whirlpool=whirlpool_pubkey,
                    funder=wallet_pubkey,
                    tick_array=tick_array_lower_pda.pubkey,
                ),
            )
        if tick_array_upper_onchain is None:
            maybe_init_tick_upper_ix = WhirlpoolIx.initialize_tick_array(
                ORCA_WHIRLPOOL_PROGRAM_ID,
                InitializeTickArrayParams(
                    start_tick_index=upper_start_tick,
                    whirlpool=whirlpool_pubkey,
                    funder=wallet_pubkey,
                    tick_array=tick_array_upper_pda.pubkey,
                ),
            )

        open_position_ix = WhirlpoolIx.open_position_with_metadata(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            OpenPositionWithMetadataParams(
                tick_lower_index=tick_lower,
                tick_upper_index=tick_upper,
                position_pda=position_pda,
                metadata_pda=metadata_pda,
                funder=wallet_pubkey,
                owner=wallet_pubkey,
                position_mint=position_mint,
                position_token_account=position_token_account,
                whirlpool=whirlpool_pubkey,
            ),
        )
        open_position_ix = Instruction(
            instructions=open_position_ix.instructions,
            cleanup_instructions=open_position_ix.cleanup_instructions,
            signers=[*open_position_ix.signers, position_mint_keypair],
        )

        increase_liquidity_ix = WhirlpoolIx.increase_liquidity(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            IncreaseLiquidityParams(
                liquidity_amount=quote.liquidity,
                token_max_a=token_max_a,
                token_max_b=token_max_b,
                whirlpool=whirlpool_pubkey,
                position_authority=wallet_pubkey,
                position=position_pda.pubkey,
                position_token_account=position_token_account,
                token_owner_account_a=token_owner_a.pubkey,
                token_owner_account_b=token_owner_b.pubkey,
                token_vault_a=whirlpool.token_vault_a,
                token_vault_b=whirlpool.token_vault_b,
                tick_array_lower=tick_array_lower_pda.pubkey,
                tick_array_upper=tick_array_upper_pda.pubkey,
            ),
        )

        tx_builder = TransactionBuilder(client, wallet)
        tx_builder.set_compute_unit_limit(400_000)
        tx_builder.set_compute_unit_price(config.PRIORITY_FEE_MICROLAMPORTS)
        tx_builder.add_instruction(token_owner_a.instruction)
        tx_builder.add_instruction(token_owner_b.instruction)
        if maybe_init_tick_lower_ix is not None:
            tx_builder.add_instruction(maybe_init_tick_lower_ix)
        if maybe_init_tick_upper_ix is not None:
            tx_builder.add_instruction(maybe_init_tick_upper_ix)
        tx_builder.add_instruction(open_position_ix)
        tx_builder.add_instruction(increase_liquidity_ix)

        log.info(
            "Prepared REAL open_position tx (not sent): price=$%.4f range=$%.4f-$%.4f "
            "input_usdc=%.6f (%d units) liquidity=%d",
            current_price,
            lower,
            upper,
            usdc_amount_for_open,
            input_amount_usdc,
            quote.liquidity,
        )
        log.info(
            "Prepared addresses: wallet=%s position_mint=%s position_pda=%s metadata_pda=%s "
            "pos_token_ata=%s owner_ata_a=%s owner_ata_b=%s tick_array_lower=%s tick_array_upper=%s",
            wallet_pubkey,
            position_mint,
            position_pda.pubkey,
            metadata_pda.pubkey,
            position_token_account,
            token_owner_a.pubkey,
            token_owner_b.pubkey,
            tick_array_lower_pda.pubkey,
            tick_array_upper_pda.pubkey,
        )
        log.info(
            "Prepared token amounts: token_est_a=%d token_est_b=%d token_max_a=%d token_max_b=%d "
            "wrapped_sol_lamports=%d usdc_units=%d",
            token_est_a,
            token_est_b,
            token_max_a,
            token_max_b,
            wrapped_sol_lamports,
            usdc_units_for_deposit,
        )
        log.info(
            "Tick arrays init required: lower=%s upper=%s",
            tick_array_lower_onchain is None,
            tick_array_upper_onchain is None,
        )

        signature = await tx_builder.build_and_execute()
        # confirm_transaction() (вызванный внутри build_and_execute) проверяет только
        # confirmation_status, но НЕ поле err — транзакция может "подтвердиться" и при
        # этом провалиться on-chain (например, если цена ушла за пределы slippage между
        # отправкой и приёмом в блок). Перепроверяем сами (найдено независимым аудитом,
        # Opus 4.8, 2026-07-26).
        status_resp = await _get_signature_status_with_retry(client, signature)
        status = status_resp.value[0]
        if status is None or status.err is not None:
            raise RuntimeError(
                f"Транзакция open_position не удалась on-chain: signature={signature} err={status.err if status else 'нет статуса'}"
            )
        log.info("open_position успешно отправлена и подтверждена: %s", signature)

        # Сохраняем mint новой позиции в .env, иначе после рестарта бот смотрит на
        # старый (уже закрытый) mint, get_position() находит on_chain=None и монитор
        # молча логирует ошибку каждые 5 минут без алерта в Telegram (найдено
        # независимым аудитом, Opus 4.8, 2026-07-26). Обновляем и in-memory значения
        # (config.POSITION_MINT + локальное POSITION_MINT в этом модуле), чтобы это
        # подхватилось в том же процессе без рестарта.
        # In-memory обновляем ВСЕГДА — это то, чем реально пользуется живой процесс
        # (get_position()/monitor_position() читают модульный POSITION_MINT, а не
        # файл). Раньше это было вложено внутрь "if dotenv_path" — в Docker-контейнере
        # find_dotenv() всегда возвращает пусто (docker-compose передаёт .env как
        # переменные окружения, не монтирует сам файл), так что in-memory обновление
        # никогда не срабатывало на сервере: после реального ребаланса бот продолжал
        # думать, что активна старая (уже закрытая) позиция (поймано вживую через
        # /rebalance на сервере, 2026-07-26).
        new_mint_str = str(position_mint)
        global POSITION_MINT
        POSITION_MINT = new_mint_str
        config.POSITION_MINT = new_mint_str

        dotenv_path = find_dotenv()
        if dotenv_path:
            set_key(dotenv_path, "POSITION_MINT", new_mint_str, quote_mode="never")
            log.info("POSITION_MINT сохранён в .env: %s", new_mint_str)
        else:
            log.warning(
                "Не удалось найти .env файл на диске (ожидаемо в Docker) — "
                "POSITION_MINT сохранён только в памяти процесса, не в файле: %s",
                new_mint_str,
            )

        position = Position(
            mint=str(position_mint),
            lower_price=lower,
            upper_price=upper,
            current_price=current_price,
            liquidity=quote.liquidity,
            fees_sol=0.0,
            fees_usdc=0.0,
            in_range=True,
            is_demo=False,
        )
        # Без этого amount_sol/amount_usdc/*_value_usd остаются на дефолтных 0.0
        # (Position их не заполняет сама) — раньше это было незаметно, потому что
        # ни один вызывающий код не печатал эти поля из возврата open_position()
        # напрямую (notify_rebalance_complete печатает только диапазон и fees
        # СТАРОЙ позиции). Впервые проявилось вживую через новую команду /open,
        # которая как раз показывает total_value_usd/value_sol_usd/value_usdc_usd
        # из свежесозданной позиции — 2026-07-27, "$0.00" при реально открытой на
        # $1.93 позиции.
        _fill_position_amounts(position, whirlpool, tick_lower, tick_upper, dec_a, dec_b)
        return position


async def estimate_add_liquidity_sol_needed(position: Position, usdc_amount: float) -> float:
    """
    Сколько SOL реально потребует add_liquidity(position, usdc_amount) второй ногой
    депозита — read-only оценка тем же quote (те же tick-диапазон и slippage 1%,
    что и в самой add_liquidity()), без кошелька и без отправки чего-либо.

    Раньше /addliquidity проверял только грубое "sol_balance <= MIN_SOL_BALANCE",
    без расчёта, сколько SOL реально нужно под конкретную сумму — на пограничном
    балансе транзакция могла упасть on-chain уже после отправки, впустую сжигая
    комиссию; и наоборот, блокировала доливку даже когда позиция вне диапазона и
    SOL-нога вообще не нужна (найдено независимым аудитом, GPT-5.2, 2026-07-27).
    """
    if usdc_amount <= 0:
        raise ValueError("Сумма USDC для доливки должна быть > 0")
    if position.is_demo:
        raise ValueError("Сейчас демо-позиция (POSITION_MINT не задан) — доливать некуда")

    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)
        _, dec_a, dec_b, _, _ = await _pool_price_and_decimals(ctx, whirlpool)

        position_mint = Pubkey.from_string(position.mint)
        position_pda = PDAUtil.get_position(ORCA_WHIRLPOOL_PROGRAM_ID, position_mint)
        on_chain = await ctx.fetcher.get_position(position_pda.pubkey)
        if on_chain is None:
            raise ValueError(f"Позиция не найдена on-chain для mint {position.mint}")

        tick_lower = on_chain.tick_lower_index
        tick_upper = on_chain.tick_upper_index

        usdc_mint = Pubkey.from_string(USDC_MINT)
        if whirlpool.token_mint_a == usdc_mint:
            usdc_decimals = dec_a
        elif whirlpool.token_mint_b == usdc_mint:
            usdc_decimals = dec_b
        else:
            raise ValueError("Whirlpool не содержит USDC mint, доливка невозможна")

        input_amount_usdc = round(usdc_amount * 10**usdc_decimals)
        quote = QuoteBuilder.increase_liquidity_by_input_token(
            IncreaseLiquidityQuoteParams(
                input_token_amount=input_amount_usdc,
                input_token_mint=usdc_mint,
                token_mint_a=whirlpool.token_mint_a,
                token_mint_b=whirlpool.token_mint_b,
                tick_current_index=whirlpool.tick_current_index,
                sqrt_price=whirlpool.sqrt_price,
                tick_lower_index=tick_lower,
                tick_upper_index=tick_upper,
                slippage_tolerance=Percentage.from_fraction(1, 100),
            )
        )

        if quote.liquidity <= 0:
            raise ValueError(
                f"Расчётная ликвидность равна нулю (диапазон ${position.lower_price:.4f}-"
                f"${position.upper_price:.4f} не покрывает текущую цену пула) — доливка отменена"
            )

        sol_mint = Pubkey.from_string(SOL_MINT)
        if whirlpool.token_mint_a == sol_mint:
            wrapped_sol_lamports = quote.token_max_a
        elif whirlpool.token_mint_b == sol_mint:
            wrapped_sol_lamports = quote.token_max_b
        else:
            raise ValueError("Whirlpool не содержит SOL mint, доливка невозможна")

        return wrapped_sol_lamports / 1_000_000_000


async def add_liquidity(position: Position, usdc_amount: float) -> Optional[Position]:
    """
    Доливает ликвидность в УЖЕ открытую позицию (increase_liquidity на её существующий
    диапазон) — в отличие от rebalance(), НЕ закрывает и не открывает позицию заново.
    usdc_amount задаёт входную сумму в USDC; вторая нога (SOL) добавляется автоматически
    в пропорции, которую требует текущая цена и диапазон — как и в open_position().
    """
    if usdc_amount <= 0:
        raise ValueError("Сумма USDC для доливки должна быть > 0")
    if position.is_demo:
        raise ValueError("Сейчас демо-позиция (POSITION_MINT не задан) — доливать некуда")

    async with get_client() as client:
        ctx = await _get_context(client)
        whirlpool = await _load_whirlpool(ctx)
        _, dec_a, dec_b, _, _ = await _pool_price_and_decimals(ctx, whirlpool)

        # position.mint, а не глобальный POSITION_MINT — тот же паттерн, что в
        # close_position()/collect_fees(). Доливаем ровно в ту позицию, которую
        # передал вызывающий код, а не в ту, что сейчас считается "активной"
        # глобально (найдено независимым аудитом, GPT-5.2, 2026-07-27).
        position_mint = Pubkey.from_string(position.mint)
        position_pda = PDAUtil.get_position(ORCA_WHIRLPOOL_PROGRAM_ID, position_mint)
        on_chain = await ctx.fetcher.get_position(position_pda.pubkey)
        if on_chain is None:
            raise ValueError(f"Позиция не найдена on-chain для mint {position.mint}")

        # Диапазон берём с существующей on-chain позиции, а не пересчитываем из
        # RANGE_WIDTH_PCT — доливка идёт в уже открытый диапазон, а не в новый.
        tick_lower = on_chain.tick_lower_index
        tick_upper = on_chain.tick_upper_index

        usdc_mint = Pubkey.from_string(USDC_MINT)
        if whirlpool.token_mint_a == usdc_mint:
            usdc_decimals = dec_a
        elif whirlpool.token_mint_b == usdc_mint:
            usdc_decimals = dec_b
        else:
            raise ValueError("Whirlpool не содержит USDC mint, доливка невозможна")

        if DRY_RUN:
            log.info(
                "DRY RUN: add_liquidity — %.6f USDC в существующий диапазон $%.4f-$%.4f",
                usdc_amount,
                position.lower_price,
                position.upper_price,
            )
            return position

        wallet = get_wallet_keypair()
        wallet_pubkey = wallet.pubkey()
        whirlpool_pubkey = Pubkey.from_string(WHIRLPOOL_ADDRESS)
        real_slippage = Percentage.from_fraction(1, 100)

        # round(), не int() — та же причина, что в open_position() (float*10**decimals
        # может дать 1999999.9999... для "ровных" сумм).
        input_amount_usdc = round(usdc_amount * 10**usdc_decimals)
        quote = QuoteBuilder.increase_liquidity_by_input_token(
            IncreaseLiquidityQuoteParams(
                input_token_amount=input_amount_usdc,
                input_token_mint=usdc_mint,
                token_mint_a=whirlpool.token_mint_a,
                token_mint_b=whirlpool.token_mint_b,
                tick_current_index=whirlpool.tick_current_index,
                sqrt_price=whirlpool.sqrt_price,
                tick_lower_index=tick_lower,
                tick_upper_index=tick_upper,
                slippage_tolerance=real_slippage,
            )
        )

        if quote.liquidity <= 0:
            raise ValueError(
                f"Расчётная ликвидность равна нулю (диапазон ${position.lower_price:.4f}-"
                f"${position.upper_price:.4f} не покрывает текущую цену пула) — доливка отменена"
            )

        token_max_a = quote.token_max_a
        token_max_b = quote.token_max_b

        sol_mint = Pubkey.from_string(SOL_MINT)
        if whirlpool.token_mint_a == sol_mint:
            wrapped_sol_lamports = token_max_a
        elif whirlpool.token_mint_b == sol_mint:
            wrapped_sol_lamports = token_max_b
        else:
            raise ValueError("Whirlpool не содержит SOL mint, wrapped SOL счёт не может быть создан")

        # ATA обеих сторон уже должны существовать (позиция открыта) — idempotent-запрос
        # тут просто найдёт их, а не создаст заново, лишней ренты не будет.
        token_owner_a = await TokenUtil.resolve_or_create_ata(
            client,
            wallet_pubkey,
            whirlpool.token_mint_a,
            wrapped_sol_amount=wrapped_sol_lamports if whirlpool.token_mint_a == sol_mint else 0,
            funder=wallet_pubkey,
            idempotent=True,
        )
        token_owner_b = await TokenUtil.resolve_or_create_ata(
            client,
            wallet_pubkey,
            whirlpool.token_mint_b,
            wrapped_sol_amount=wrapped_sol_lamports if whirlpool.token_mint_b == sol_mint else 0,
            funder=wallet_pubkey,
            idempotent=True,
        )

        position_token_account = TokenUtil.derive_ata(wallet_pubkey, position_mint)

        # Тик-массивы для этого диапазона уже инициализированы (позиция в нём открыта) —
        # в отличие от open_position(), initialize_tick_array здесь не нужен.
        lower_start_tick = TickUtil.get_start_tick_index(tick_lower, whirlpool.tick_spacing)
        upper_start_tick = TickUtil.get_start_tick_index(tick_upper, whirlpool.tick_spacing)
        tick_array_lower_pda = PDAUtil.get_tick_array(
            ORCA_WHIRLPOOL_PROGRAM_ID, whirlpool_pubkey, lower_start_tick
        )
        tick_array_upper_pda = PDAUtil.get_tick_array(
            ORCA_WHIRLPOOL_PROGRAM_ID, whirlpool_pubkey, upper_start_tick
        )

        increase_liquidity_ix = WhirlpoolIx.increase_liquidity(
            ORCA_WHIRLPOOL_PROGRAM_ID,
            IncreaseLiquidityParams(
                liquidity_amount=quote.liquidity,
                token_max_a=token_max_a,
                token_max_b=token_max_b,
                whirlpool=whirlpool_pubkey,
                position_authority=wallet_pubkey,
                position=position_pda.pubkey,
                position_token_account=position_token_account,
                token_owner_account_a=token_owner_a.pubkey,
                token_owner_account_b=token_owner_b.pubkey,
                token_vault_a=whirlpool.token_vault_a,
                token_vault_b=whirlpool.token_vault_b,
                tick_array_lower=tick_array_lower_pda.pubkey,
                tick_array_upper=tick_array_upper_pda.pubkey,
            ),
        )

        tx_builder = TransactionBuilder(client, wallet)
        tx_builder.set_compute_unit_limit(400_000)
        tx_builder.set_compute_unit_price(config.PRIORITY_FEE_MICROLAMPORTS)
        tx_builder.add_instruction(token_owner_a.instruction)
        tx_builder.add_instruction(token_owner_b.instruction)
        tx_builder.add_instruction(increase_liquidity_ix)

        log.info(
            "Prepared REAL add_liquidity tx (not sent): mint=%s range=$%.4f-$%.4f "
            "input_usdc=%.6f (%d units) liquidity=%d",
            position.mint,
            position.lower_price,
            position.upper_price,
            usdc_amount,
            input_amount_usdc,
            quote.liquidity,
        )

        signature = await tx_builder.build_and_execute()
        # Тот же двойной чек, что и во всех остальных реальных транзакциях этого модуля:
        # confirm_transaction() проверяет только confirmation_status, не err.
        status_resp = await _get_signature_status_with_retry(client, signature)
        status = status_resp.value[0]
        if status is None or status.err is not None:
            raise RuntimeError(
                f"Транзакция add_liquidity не удалась on-chain: signature={signature} "
                f"err={status.err if status else 'нет статуса'}"
            )
        log.info("add_liquidity успешно отправлена и подтверждена: %s", signature)

        # Синхронизируем глобальный POSITION_MINT с position.mint (тот же паттерн,
        # что в open_position()) — обычно уже совпадает, но если вызывающий код
        # держал не самый свежий Position, доливка сама по себе явное решение
        # оператора считать position.mint активной позицией, так что self-heal
        # здесь корректен, а не просто noop.
        global POSITION_MINT
        POSITION_MINT = position.mint
        config.POSITION_MINT = position.mint

        dotenv_path = find_dotenv()
        if dotenv_path:
            set_key(dotenv_path, "POSITION_MINT", position.mint, quote_mode="never")
        else:
            log.warning(
                "Не удалось найти .env файл на диске (ожидаемо в Docker) — "
                "POSITION_MINT сохранён только в памяти процесса, не в файле: %s",
                position.mint,
            )

    return await get_position()


async def rebalance(position: Position) -> Optional[Position]:
    log.info("Начинаем ребаланс%s...", " [DRY RUN]" if DRY_RUN else "")

    if not await close_position(position):
        log.error("Не удалось закрыть позицию")
        return None

    current_price = await get_current_price()

    usdc_amount = None
    if not DRY_RUN:
        usdc_amount = await _compute_reopen_usdc_amount(current_price)
        log.info(
            "Пересчитанная сумма для реоткрытия: %.6f USDC (по факту доступного баланса)",
            usdc_amount,
        )
        if usdc_amount <= 0:
            log.error("Недостаточно средств для реоткрытия позиции после ребаланса")
            return None

    new_position = await open_position(current_price, usdc_amount=usdc_amount)
    if new_position is None:
        log.error("Не удалось открыть новую позицию")
        return None

    new_position.is_demo = position.is_demo
    log.info("Ребаланс завершён успешно")
    return new_position
