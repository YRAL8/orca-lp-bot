import base58
from solana.rpc.async_api import AsyncClient
from solders.pubkey import Pubkey

from config import WALLET_PRIVATE_KEY, MIN_SOL_BALANCE, get_rpc_url, wallet_configured


def get_client() -> AsyncClient:
    """Подключение к Solana RPC (Helius или публичный fallback)."""
    return AsyncClient(get_rpc_url())


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
        return Pubkey.from_bytes(decoded)
    raise ValueError(
        "WALLET_PRIVATE_KEY: ожидается base58 строка (32 или 64 байта после декодирования)"
    )


async def get_sol_balance() -> float | None:
    """Баланс SOL кошелька. None — если ключ не задан (read-only режим)."""
    pubkey = get_wallet_pubkey()
    if pubkey is None:
        return None

    async with get_client() as client:
        response = await client.get_balance(pubkey)
        return response.value / 1_000_000_000


async def check_sol_balance() -> bool:
    """True, если SOL достаточно для газа или кошелёк не настроен (read-only)."""
    balance = await get_sol_balance()
    if balance is None:
        return True
    if balance < MIN_SOL_BALANCE:
        print(f"⚠️ Низкий баланс SOL: {balance:.4f} SOL (минимум {MIN_SOL_BALANCE})")
        return False
    return True
