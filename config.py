import os
from dotenv import load_dotenv

load_dotenv()

# Persistent data (mounted in Docker as /app/data). Used for cycle journal/state.
DATA_DIR = os.getenv("DATA_DIR", "/app/data").strip() or "/app/data"

# Solana
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", "").strip()
# Публичный RPC — fallback для read-only dry-run без Helius
PUBLIC_RPC_URL = os.getenv("PUBLIC_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "").strip()

# Orca
WHIRLPOOL_ADDRESS = os.getenv("WHIRLPOOL_ADDRESS", "").strip()
POSITION_MINT = os.getenv("POSITION_MINT", "").strip()
REBALANCE_REOPEN_PENDING = os.getenv("REBALANCE_REOPEN_PENDING", "false").lower() == "true"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Настройки бота
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
# Мониторинг vs действие — независимо от DRY_RUN. Пока close/open/collect_fees
# для реальных транзакций не реализованы (см. orca.py), держим false: бот
# только следит за диапазоном и шлёт алерты, не пытается ребалансить.
AUTO_REBALANCE = os.getenv("AUTO_REBALANCE", "false").lower() == "true"
RANGE_WIDTH_PCT = float(os.getenv("RANGE_WIDTH_PCT", "8"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "300"))
REBALANCE_DELAY_MIN = int(os.getenv("REBALANCE_DELAY_MIN", "20"))
HEARTBEAT_INTERVAL_HOURS = int(os.getenv("HEARTBEAT_INTERVAL_HOURS", "4"))
MIN_SOL_BALANCE = float(os.getenv("MIN_SOL_BALANCE", "0.05"))

# Если POSITION_MINT не задан — в dry-run строим демо-диапазон вокруг реальной цены
DEMO_POSITION = os.getenv("DEMO_POSITION", "true").lower() == "true"
# Сумма для демо-отчёта (USD), когда нет реальной позиции
DEMO_DEPOSIT_USD = float(os.getenv("DEMO_DEPOSIT_USD", "1000"))
# Реальный single-side депозит в USDC для открытия позиции
OPEN_POSITION_USDC_AMOUNT = float(os.getenv("OPEN_POSITION_USDC_AMOUNT", "2.0"))
# Priority fee (micro-lamports за compute unit) для реальных транзакций — страховка от
# непопадания в блок при перегрузке сети. Реальные последние fee на этот пул сейчас
# в основном 0 (не перегружен), но скромная ненулевая цена почти ничего не стоит
# (для лимита 400k CU это максимум ~0.002 SOL) и заметно повышает шанс попасть в блок.
PRIORITY_FEE_MICROLAMPORTS = int(os.getenv("PRIORITY_FEE_MICROLAMPORTS", "5000"))
# Резерв SOL под безвозвратную ренту открытия НОВОЙ позиции (position PDA + mint +
# position ATA + Metaplex metadata) — отдельно от MIN_SOL_BALANCE, у которого другая роль
# (порог алерта "мало SOL", не "сколько нужно на следующее открытие"). ~0.012-0.015 SOL
# по факту реальных открытий сегодня; берём с запасом. Смешение этих двух ролей в одну
# константу — реальный баг, найденный независимо тремя аудитами (Grok 4.5, GPT-5.2,
# Opus 4.8, 2026-07-26): без отдельного резерва ребаланс мог потратить SOL до самой
# границы MIN_SOL_BALANCE, а потом ещё и ренту сверху — уходя ниже порога алерта сразу
# после "успешного" ребаланса.
OPEN_POSITION_RENT_RESERVE_SOL = float(os.getenv("OPEN_POSITION_RENT_RESERVE_SOL", "0.02"))

_PLACEHOLDER_MARKERS = (
    "YOUR_",
    "CHANGE_ME",
    "TODO",
    "PLACEHOLDER",
)


def is_placeholder(value: str) -> bool:
    """True, если значение не заполнено или осталось шаблоном из env_template."""
    if not value:
        return True
    upper = value.upper()
    return any(marker in upper for marker in _PLACEHOLDER_MARKERS)


def get_rpc_url() -> str:
    """Helius, если ключ задан; иначе публичный mainnet RPC (только чтение)."""
    if not is_placeholder(HELIUS_RPC_URL):
        return HELIUS_RPC_URL
    return PUBLIC_RPC_URL


def wallet_configured() -> bool:
    return not is_placeholder(WALLET_PRIVATE_KEY)
