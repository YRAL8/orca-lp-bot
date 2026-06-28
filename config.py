import os
from dotenv import load_dotenv

load_dotenv()

# Solana
HELIUS_RPC_URL = os.getenv("HELIUS_RPC_URL", "").strip()
# Публичный RPC — fallback для read-only dry-run без Helius
PUBLIC_RPC_URL = os.getenv("PUBLIC_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "").strip()

# Orca
WHIRLPOOL_ADDRESS = os.getenv("WHIRLPOOL_ADDRESS", "").strip()
POSITION_MINT = os.getenv("POSITION_MINT", "").strip()

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Настройки бота
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
RANGE_WIDTH_PCT = float(os.getenv("RANGE_WIDTH_PCT", "8"))
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "300"))
REBALANCE_DELAY_MIN = int(os.getenv("REBALANCE_DELAY_MIN", "20"))
HEARTBEAT_INTERVAL_HOURS = int(os.getenv("HEARTBEAT_INTERVAL_HOURS", "4"))
MIN_SOL_BALANCE = float(os.getenv("MIN_SOL_BALANCE", "0.05"))

# Если POSITION_MINT не задан — в dry-run строим демо-диапазон вокруг реальной цены
DEMO_POSITION = os.getenv("DEMO_POSITION", "true").lower() == "true"

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
