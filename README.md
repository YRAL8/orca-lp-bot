# Orca Rebalance Bot — SOL/USDC

Бот для автоматического управления позицией ликвидности на Orca (Solana).

## Что делает бот

- Читает **реальную цену** SOL/USDC с Orca mainnet (on-chain)
- Читает **реальную позицию** по `POSITION_MINT` (или демо-диапазон в dry-run)
- Мониторит цену каждые 5 минут
- Когда цена выходит за границу диапазона — ждёт 20 минут
- Если цена не вернулась — **симулирует** ребаланс (при `DRY_RUN=true`)
- Уведомляет в Telegram о каждом действии
- Heartbeat каждые 4 часа
- Команда /status — статус по запросу

## Режим «на фантиках» (DRY RUN)

```
DRY_RUN=true
DEMO_POSITION=true   # если POSITION_MINT ещё не задан
```

- **Реальные данные** с mainnet (цена, позиция)
- **Ноль транзакций** — только логи и Telegram
- Helius **не обязателен** — есть fallback на публичный RPC (только чтение)
- Кошелёк **не обязателен** для мониторинга

Когда появится позиция — укажи `POSITION_MINT` (NFT mint из Orca UI).

## Установка

### 1. Установить зависимости
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
pip install -r requirements.txt
```

### 2. Заполнить .env файл
```
HELIUS_RPC_URL=          # ключ с helius.dev
WALLET_PRIVATE_KEY=      # приватный ключ кошелька (base58)
WHIRLPOOL_ADDRESS=       # адрес пула SOL/USDC на Orca
POSITION_MINT=           # NFT mint твоей позиции
TELEGRAM_BOT_TOKEN=      # токен от @BotFather
TELEGRAM_CHAT_ID=        # твой chat_id
DRY_RUN=true             # true = без транзакций, только логи
DEMO_POSITION=true       # демо-диапазон, если POSITION_MINT не задан
```

### 3. Запустить в DRY RUN режиме (без реальных транзакций)
```bash
python main.py
```

### 4. Когда всё проверено — переключить на боевой режим
```
DRY_RUN=false
```

## Структура файлов

```
.env              — настройки и ключи (не публиковать!)
config.py         — читает .env
solana_client.py  — подключение к Solana
orca.py           — работа с позицией на Orca
telegram_bot.py   — уведомления и /status
main.py           — главная логика
requirements.txt  — зависимости
bot.log           — логи работы бота
```

## Запуск на VPS (после тестирования)

```bash
# Установить screen для работы в фоне
sudo apt install screen

# Запустить бота в фоне
screen -S orca_bot
python main.py

# Отключиться от screen (бот продолжает работать)
Ctrl+A затем D

# Вернуться к боту
screen -r orca_bot
```

## Важно

- Никогда не публикуй .env файл
- Храни приватный ключ только на сервере
- Всегда тестируй в DRY RUN перед боевым запуском
- Держи на кошельке минимум 0.05 SOL для газа
