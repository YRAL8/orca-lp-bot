import asyncio

# /pauza — авто-цикл (monitor_position) стоит, ручные команды (/rebalance,
# /addliquidity) по-прежнему работают. /stop — полная заморозка, ручные команды
# денежных действий тоже отключены. /boevoy снимает оба разом.
bot_paused = False
bot_frozen = False

# Общий lock для авто-ребаланса и ручных денежных команд.
rebalance_lock = asyncio.Lock()
