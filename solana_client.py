import base58
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.core import RPCException
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from spl.token.instructions import get_associated_token_address

from config import WALLET_PRIVATE_KEY, MIN_SOL_BALANCE, get_rpc_url, wallet_configured

USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def get_client() -> AsyncClient:
    """
    Подключение к Solana RPC (Helius или публичный fallback).
    commitment="confirmed" явно — без этого AsyncClient по умолчанию читает на уровне
    "finalized" (~32 слотов, секунды), а наши транзакции подтверждаются на "confirmed"
    (build_and_execute). Из-за рассинхрона чтение баланса сразу после своей же реальной
    транзакции могло вернуть устаревшее (до-транзакционное) значение ещё несколько секунд —
    поймано вживую при первом сквозном тесте rebalance() 2026-07-26: закрытие прошло,
    баланс на проверке показал 0.0695 SOL, а через минуту реальный баланс оказался 0.1001 SOL.
    """
    return AsyncClient(get_rpc_url(), commitment=Confirmed)


def get_wallet_pubkey() -> Pubkey | None:
    """
    Публичный ключ кошелька из base58 приватного ключа.
    В read-only dry-run без ключа возвращает None.
    """
    if not wallet_configured():
        return None

    decoded = base58.b58decode(WALLET_PRIVATE_KEY)
    if len(decoded) == 64:
        return Pubkey.from_bytes(decoded[32:])
    if len(decoded) == 32:
        return Keypair.from_seed(decoded).pubkey()
    raise ValueError(
        "WALLET_PRIVATE_KEY: ожидается base58 строка (32 или 64 байта после декодирования)"
    )


def get_wallet_keypair() -> Keypair:
    """
    Полный Keypair из WALLET_PRIVATE_KEY (base58, 64 байта).
    Кидает ValueError если ключ не настроен или имеет неверную длину.
    """
    if not wallet_configured():
        raise ValueError("WALLET_PRIVATE_KEY не задан")

    decoded = base58.b58decode(WALLET_PRIVATE_KEY)
    if len(decoded) != 64:
        raise ValueError(
            "WALLET_PRIVATE_KEY: для реальных транзакций ожидается 64 байта после base58-декодирования"
        )
    return Keypair.from_bytes(decoded)


async def get_sol_balance() -> float | None:
    """Баланс SOL кошелька. None — если ключ не задан (read-only режим)."""
    pubkey = get_wallet_pubkey()
    if pubkey is None:
        return None

    async with get_client() as client:
        response = await client.get_balance(pubkey)
        return response.value / 1_000_000_000


async def get_usdc_balance() -> float | None:
    """Баланс USDC кошелька (ATA). None — если ключ не задан (read-only режим)."""
    pubkey = get_wallet_pubkey()
    if pubkey is None:
        return None

    usdc_mint = Pubkey.from_string(USDC_MINT)
    ata = get_associated_token_address(pubkey, usdc_mint)

    async with get_client() as client:
        try:
            response = await client.get_token_account_balance(ata)
        except RPCException as e:
            # Ловим ТОЛЬКО "аккаунта нет" (кошелёк ещё не получал USDC) — это законные 0.
            # Любая другая RPC-ошибка (таймаут, rate-limit и т.п.) не должна маскироваться
            # под "баланс нулевой", иначе _compute_reopen_usdc_amount() решит, что реоткрывать
            # нечего, из-за временного сбоя сети, а не реального отсутствия денег (найдено
            # независимыми аудитами, Grok 4.5 / GPT-5.2 / Opus 4.8, 2026-07-26).
            if "could not find account" in str(e).lower():
                return 0.0
            raise

        if response is None or response.value is None:
            return 0.0

        # amount (целое, строка) точнее, чем ui_amount (float) — избегаем ошибок округления
        # при работе с "весь доступный баланс" (найдено GPT-5.2/Opus 4.8, 2026-07-26).
        decimals = response.value.decimals
        raw_amount = int(response.value.amount)
        return raw_amount / 10**decimals


async def check_sol_balance() -> bool:
    """True, если SOL достаточно для газа или кошелёк не настроен (read-only)."""
    balance = await get_sol_balance()
    if balance is None:
        return True
    if balance < MIN_SOL_BALANCE:
        print(f"⚠️ Низкий баланс SOL: {balance:.4f} SOL (минимум {MIN_SOL_BALANCE})")
        return False
    return True
