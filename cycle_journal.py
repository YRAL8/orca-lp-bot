from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# Orca fee_rate=400 => 0.04% = 0.0004 of input notional.
POOL_FEE_RATE = 400
POOL_FEE_PCT = 0.0004

# Rough, intentionally simple: close + (maybe swap) + open. Tiny, but explicit.
NETWORK_FEE_LAMPORTS_EST = 41_000
LAMPORTS_PER_SOL = 1_000_000_000

DEFAULT_DATA_DIR = os.getenv("DATA_DIR", "/app/data")
STATE_FILENAME = "cycle_state.json"
JOURNAL_FILENAME = "cycle_journal.jsonl"

# Журнал пишется вечно, даже если про него забыли — поэтому он ограничен.
# Одна строка ~0.5 КБ. При самом частом ребалансе (узкий диапазон 0.5% дал
# сегодня ~15 циклов в сутки) 5000 записей — это больше года наблюдений и
# примерно 2.5 МБ. Обрезаем по КОЛИЧЕСТВУ строк, а не по размеру: обрезка по
# байтам разрубила бы строку пополам и сломала разбор.
MAX_JOURNAL_RECORDS = 5000
# Страховка на случай, если строки окажутся сильно длиннее ожидаемого.
MAX_JOURNAL_BYTES = 8 * 1024 * 1024


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).astimezone(timezone.utc)


def compute_would_be_value_usd(*, open_sol_qty: float, open_usdc_qty: float, close_price: float) -> float:
    return float(open_sol_qty * close_price + open_usdc_qty)


def compute_divergence_usd(
    *,
    open_sol_qty: float,
    open_usdc_qty: float,
    close_price: float,
    close_position_value_usd: float,
) -> float:
    """Расхождение = фактическая стоимость позиции при закрытии - 'стоило бы'."""
    would_be = compute_would_be_value_usd(
        open_sol_qty=open_sol_qty,
        open_usdc_qty=open_usdc_qty,
        close_price=close_price,
    )
    return float(close_position_value_usd - would_be)


@dataclass
class ActiveCycle:
    mint: str
    open_time_utc: str
    range_width_pct: float
    lower_price: float
    upper_price: float
    open_price: float
    open_sol_qty: float
    open_usdc_qty: float
    open_position_value_usd: float
    # Market path accumulation
    samples: int = 0
    path_sum: float = 0.0
    min_price: float = 0.0
    max_price: float = 0.0
    last_price: float = 0.0
    # Incomplete cycle flag + reasons.
    incomplete: bool = False
    incomplete_reasons: list[str] | None = None


@dataclass
class PendingClose:
    cycle: ActiveCycle
    close_time_utc: str
    close_price: float
    close_position_value_usd: float
    fees_sol: float
    fees_usdc: float


@dataclass
class PendingSwap:
    direction: str  # "SOL_TO_USDC" | "USDC_TO_SOL"
    amount_in: float
    price: float


@dataclass
class StateFile:
    version: int = 1
    active: ActiveCycle | None = None
    pending_close: PendingClose | None = None
    pending_swap: PendingSwap | None = None


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


class CycleJournal:
    """
    Observer-only journal: never throws to caller. All public methods are safe.

    - State persists unfinished cycle to survive restarts.
    - Finished cycles are appended to JSONL: one line per cycle.
    """

    def __init__(self, *, data_dir: str = DEFAULT_DATA_DIR, dry_run: bool = False) -> None:
        self.data_dir = data_dir
        self.dry_run = dry_run

    @property
    def state_path(self) -> str:
        return os.path.join(self.data_dir, STATE_FILENAME)

    @property
    def journal_path(self) -> str:
        return os.path.join(self.data_dir, JOURNAL_FILENAME)

    def _ensure_dir(self) -> bool:
        if self.dry_run:
            return False
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            return True
        except Exception:
            log.warning("CycleJournal: не удалось создать data_dir=%r", self.data_dir, exc_info=True)
            return False

    def _load_state(self) -> StateFile:
        if self.dry_run:
            return StateFile()
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return StateFile()
        except Exception:
            log.warning("CycleJournal: не удалось прочитать state", exc_info=True)
            return StateFile()

        try:
            active = raw.get("active")
            pending_close = raw.get("pending_close")
            pending_swap = raw.get("pending_swap")

            st = StateFile(version=int(raw.get("version", 1)))
            if active:
                st.active = ActiveCycle(**active)
            if pending_close:
                cycle = ActiveCycle(**pending_close["cycle"])
                st.pending_close = PendingClose(
                    cycle=cycle,
                    close_time_utc=pending_close["close_time_utc"],
                    close_price=float(pending_close["close_price"]),
                    close_position_value_usd=float(pending_close["close_position_value_usd"]),
                    fees_sol=float(pending_close["fees_sol"]),
                    fees_usdc=float(pending_close["fees_usdc"]),
                )
            if pending_swap:
                st.pending_swap = PendingSwap(
                    direction=str(pending_swap["direction"]),
                    amount_in=float(pending_swap["amount_in"]),
                    price=float(pending_swap["price"]),
                )
            return st
        except Exception:
            log.warning("CycleJournal: state JSON некорректен, игнорируем", exc_info=True)
            return StateFile()

    def _atomic_write_json(self, path: str, obj: Any) -> None:
        if self.dry_run:
            return
        if not self._ensure_dir():
            return
        try:
            data = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            fd, tmp = tempfile.mkstemp(prefix=".tmp.", suffix=".json", dir=self.data_dir)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(data)
                os.replace(tmp, path)
            finally:
                try:
                    if os.path.exists(tmp):
                        os.unlink(tmp)
                except Exception:
                    pass
        except Exception:
            log.warning("CycleJournal: не удалось записать %s", path, exc_info=True)

    def _save_state(self, st: StateFile) -> None:
        if self.dry_run:
            return
        payload: dict[str, Any] = {"version": st.version, "active": None, "pending_close": None, "pending_swap": None}
        if st.active is not None:
            payload["active"] = asdict(st.active)
        if st.pending_close is not None:
            payload["pending_close"] = {
                "cycle": asdict(st.pending_close.cycle),
                "close_time_utc": st.pending_close.close_time_utc,
                "close_price": st.pending_close.close_price,
                "close_position_value_usd": st.pending_close.close_position_value_usd,
                "fees_sol": st.pending_close.fees_sol,
                "fees_usdc": st.pending_close.fees_usdc,
            }
        if st.pending_swap is not None:
            payload["pending_swap"] = asdict(st.pending_swap)
        self._atomic_write_json(self.state_path, payload)

    def _append_jsonl(self, obj: dict[str, Any]) -> None:
        if self.dry_run:
            return
        if not self._ensure_dir():
            return
        try:
            line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
            with open(self.journal_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._trim_journal()
        except Exception:
            log.warning("CycleJournal: не удалось append JSONL", exc_info=True)

    def _trim_journal(self) -> None:
        """Оставляет последние MAX_JOURNAL_RECORDS записей.

        Проверка дешёвая (обычно только stat файла), полное переписывание
        случается раз в тысячи циклов. Сбой обрезки не должен мешать записи —
        лучше слегка разросшийся журнал, чем потерянные наблюдения.
        """
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            size = os.path.getsize(self.journal_path)
            if len(lines) <= MAX_JOURNAL_RECORDS and size <= MAX_JOURNAL_BYTES:
                return
            if size > MAX_JOURNAL_BYTES and len(lines) > 1:
                # Строки оказались длиннее ожидаемого — режем жёстче по размеру.
                lines = lines[-max(1, len(lines) // 2):]
            keep = lines[-MAX_JOURNAL_RECORDS:]
            tmp = self.journal_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.writelines(keep)
            os.replace(tmp, self.journal_path)
            log.info(
                "CycleJournal: журнал обрезан до %d последних записей", len(keep)
            )
        except Exception:
            log.warning("CycleJournal: не удалось обрезать журнал", exc_info=True)

    def mark_startup_restart_incomplete(self) -> None:
        """
        Если процесс перезапустился посреди цикла — помечаем его неполным, даже если
        state восстановлен: в downtime тиков не было, path может быть занижен.
        """
        if self.dry_run:
            return
        st = self._load_state()
        if st.active is None:
            return
        if not st.active.incomplete:
            st.active.incomplete = True
        reasons = list(st.active.incomplete_reasons or [])
        if "restart" not in reasons:
            reasons.append("restart")
        st.active.incomplete_reasons = reasons
        self._save_state(st)

    def ensure_active_from_position(
        self,
        position: Any,
        *,
        now: datetime | None = None,
        range_width_pct: float | None = None,
        force_incomplete: bool = True,
    ) -> None:
        """
        Если state отсутствует/не совпадает с текущей on-chain позицией — создаём
        active cycle из текущего снапшота и помечаем неполным (старт посреди цикла).
        """
        if self.dry_run:
            return
        st = self._load_state()
        mint = str(getattr(position, "mint", "") or "")
        if not mint:
            return

        if st.active is not None and st.active.mint == mint:
            return

        now = now or _utc_now()
        if range_width_pct is None:
            try:
                import config  # local import (no cycles)

                range_width_pct = float(getattr(config, "RANGE_WIDTH_PCT", 0.0))
            except Exception:
                range_width_pct = 0.0
        open_price = _safe_float(getattr(position, "current_price", 0.0))
        active = ActiveCycle(
            mint=mint,
            open_time_utc=_utc_iso(now),
            range_width_pct=float(range_width_pct),
            lower_price=_safe_float(getattr(position, "lower_price", 0.0)),
            upper_price=_safe_float(getattr(position, "upper_price", 0.0)),
            open_price=open_price,
            open_sol_qty=_safe_float(getattr(position, "amount_sol", 0.0)),
            open_usdc_qty=_safe_float(getattr(position, "amount_usdc", 0.0)),
            open_position_value_usd=_safe_float(getattr(position, "total_value_usd", 0.0)),
            samples=0,
            path_sum=0.0,
            min_price=open_price,
            max_price=open_price,
            last_price=open_price,
            incomplete=bool(force_incomplete),
            incomplete_reasons=(["restart"] if force_incomplete else []),
        )
        st.active = active
        self._save_state(st)

    def on_open(self, position: Any, *, range_width_pct: float, now: datetime | None = None) -> None:
        """Начало нового цикла: открыта позиция (open_position)."""
        if self.dry_run:
            return
        now = now or _utc_now()
        # Если по какой-то причине остался незавершённый pending_close — допишем его,
        # чтобы не потерять цикл при ручном вмешательстве/перезапуске.
        self.finalize_pending_cycle()
        st = self._load_state()
        mint = str(getattr(position, "mint", "") or "")
        if not mint:
            return
        open_price = _safe_float(getattr(position, "current_price", 0.0))
        active = ActiveCycle(
            mint=mint,
            open_time_utc=_utc_iso(now),
            range_width_pct=float(range_width_pct),
            lower_price=_safe_float(getattr(position, "lower_price", 0.0)),
            upper_price=_safe_float(getattr(position, "upper_price", 0.0)),
            open_price=open_price,
            open_sol_qty=_safe_float(getattr(position, "amount_sol", 0.0)),
            open_usdc_qty=_safe_float(getattr(position, "amount_usdc", 0.0)),
            open_position_value_usd=_safe_float(getattr(position, "total_value_usd", 0.0)),
            samples=0,
            path_sum=0.0,
            min_price=open_price,
            max_price=open_price,
            last_price=open_price,
            incomplete=False,
            incomplete_reasons=[],
        )
        st.active = active
        self._save_state(st)

    def on_monitor_tick(self, price: float, *, now: datetime | None = None) -> None:
        if self.dry_run:
            return
        now = now or _utc_now()
        _ = now  # reserved for future per-tick timestamping if needed.
        st = self._load_state()
        if st.active is None:
            return
        p = float(price)
        st.active.samples += 1
        if st.active.last_price > 0:
            st.active.path_sum += abs(p - st.active.last_price)
        st.active.last_price = p
        if st.active.min_price <= 0:
            st.active.min_price = p
        if st.active.max_price <= 0:
            st.active.max_price = p
        st.active.min_price = min(st.active.min_price, p)
        st.active.max_price = max(st.active.max_price, p)
        self._save_state(st)

    def mark_add_liquidity_incomplete(self) -> None:
        if self.dry_run:
            return
        st = self._load_state()
        if st.active is None:
            return
        st.active.incomplete = True
        reasons = list(st.active.incomplete_reasons or [])
        if "add_liquidity" not in reasons:
            reasons.append("add_liquidity")
        st.active.incomplete_reasons = reasons
        self._save_state(st)

    def capture_close_snapshot(self, position: Any, *, now: datetime | None = None) -> None:
        """
        Фиксируем закрытие цикла (до свопа и до реоткрытия) — но не пишем JSONL
        сразу: ждём результата swap_to_balance (pending_swap), чтобы посчитать издержки.
        """
        if self.dry_run:
            return
        now = now or _utc_now()
        st = self._load_state()
        if st.active is None:
            # нет активного state — создадим из текущего снапшота, пометим неполным
            self.ensure_active_from_position(position, now=now, force_incomplete=True)
            st = self._load_state()
        if st.active is None:
            return

        close_price = _safe_float(getattr(position, "current_price", 0.0))
        pending = PendingClose(
            cycle=st.active,
            close_time_utc=_utc_iso(now),
            close_price=close_price,
            close_position_value_usd=_safe_float(getattr(position, "total_value_usd", 0.0)),
            fees_sol=_safe_float(getattr(position, "fees_sol", 0.0)),
            fees_usdc=_safe_float(getattr(position, "fees_usdc", 0.0)),
        )
        st.pending_close = pending
        st.active = None
        st.pending_swap = None  # reset; swap belongs to this transition
        self._save_state(st)

    def record_swap_success(self, *, direction: str, amount_in: float, price: float) -> None:
        """Фиксируем факт успешного балансирующего свопа (для издержек цикла)."""
        if self.dry_run:
            return
        st = self._load_state()
        if st.pending_close is None:
            return
        if direction not in ("SOL_TO_USDC", "USDC_TO_SOL"):
            return
        st.pending_swap = PendingSwap(direction=direction, amount_in=float(amount_in), price=float(price))
        self._save_state(st)

    def finalize_pending_cycle(self) -> None:
        """Пишет одну JSONL-строку для pending_close (если она есть) и очищает pending_*."""
        if self.dry_run:
            return
        st = self._load_state()
        if st.pending_close is None:
            return

        cycle = st.pending_close.cycle
        close_price = float(st.pending_close.close_price)
        close_value_usd = float(st.pending_close.close_position_value_usd)

        fees_usd = float(st.pending_close.fees_sol * close_price + st.pending_close.fees_usdc)
        divergence_usd = compute_divergence_usd(
            open_sol_qty=cycle.open_sol_qty,
            open_usdc_qty=cycle.open_usdc_qty,
            close_price=close_price,
            close_position_value_usd=close_value_usd,
        )

        swap_fee_usd = 0.0
        swap_direction = "NONE"
        swap_amount_in = 0.0
        if st.pending_swap is not None:
            swap_direction = st.pending_swap.direction
            swap_amount_in = float(st.pending_swap.amount_in)
            if swap_direction == "SOL_TO_USDC":
                swap_fee_usd = POOL_FEE_PCT * (swap_amount_in * float(st.pending_swap.price))
            elif swap_direction == "USDC_TO_SOL":
                swap_fee_usd = POOL_FEE_PCT * swap_amount_in

        tx_fee_usd = (NETWORK_FEE_LAMPORTS_EST / LAMPORTS_PER_SOL) * close_price
        costs_usd = float(swap_fee_usd + tx_fee_usd)

        pnl_usd = float(fees_usd - abs(divergence_usd) - costs_usd)

        open_time = _parse_utc_iso(cycle.open_time_utc)
        close_time = _parse_utc_iso(st.pending_close.close_time_utc)
        duration_hours = float((close_time - open_time).total_seconds() / 3600.0)

        net_pct = (close_price - cycle.open_price) / cycle.open_price if cycle.open_price > 0 else 0.0
        path_pct = (cycle.path_sum / cycle.open_price) if cycle.open_price > 0 else 0.0
        efficiency: float | None
        if path_pct > 0:
            efficiency = abs(net_pct) / path_pct
        else:
            efficiency = None
        amplitude_pct = ((cycle.max_price - cycle.min_price) / cycle.open_price) if cycle.open_price > 0 else 0.0

        record: dict[str, Any] = {
            "version": 1,
            "mint": cycle.mint,
            "open_time_utc": cycle.open_time_utc,
            "close_time_utc": st.pending_close.close_time_utc,
            "range_width_pct": cycle.range_width_pct,
            "lower_price": cycle.lower_price,
            "upper_price": cycle.upper_price,
            "open_price": cycle.open_price,
            "close_price": close_price,
            "open_sol_qty": cycle.open_sol_qty,
            "open_usdc_qty": cycle.open_usdc_qty,
            "open_position_value_usd": cycle.open_position_value_usd,
            "close_position_value_usd": close_value_usd,
            "fees_sol": st.pending_close.fees_sol,
            "fees_usdc": st.pending_close.fees_usdc,
            "fees_usd": fees_usd,
            "divergence_usd": divergence_usd,
            "swap_direction": swap_direction,
            "swap_amount_in": swap_amount_in,
            "swap_fee_usd": float(swap_fee_usd),
            "tx_fee_lamports_est": NETWORK_FEE_LAMPORTS_EST,
            "tx_fee_usd_est": float(tx_fee_usd),
            "costs_usd_est": costs_usd,
            "pnl_usd": pnl_usd,
            "samples": cycle.samples,
            "path_sum": cycle.path_sum,
            "min_price": cycle.min_price,
            "max_price": cycle.max_price,
            "net_pct": float(net_pct),
            "path_pct": float(path_pct),
            "efficiency": float(efficiency) if efficiency is not None else None,
            "amplitude_pct": float(amplitude_pct),
            "duration_hours": duration_hours,
            "incomplete": bool(cycle.incomplete),
            "incomplete_reasons": list(cycle.incomplete_reasons or []),
        }

        self._append_jsonl(record)
        st.pending_close = None
        st.pending_swap = None
        self._save_state(st)

    def read_recent_cycles(self, *, limit: int = 10) -> list[dict[str, Any]]:
        """Read-only helper (safe): returns last `limit` JSON objects from journal."""
        try:
            with open(self.journal_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except FileNotFoundError:
            return []
        except Exception:
            log.warning("CycleJournal: не удалось прочитать journal", exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return list(reversed(out))


# Default singleton for app code (configured by config at runtime).
_DEFAULT: CycleJournal | None = None


def get_default_journal() -> CycleJournal:
    global _DEFAULT
    if _DEFAULT is not None:
        return _DEFAULT
    try:
        import config  # local import to avoid cycles

        data_dir = getattr(config, "DATA_DIR", DEFAULT_DATA_DIR)
        dry_run = bool(getattr(config, "DRY_RUN", False))
    except Exception:
        data_dir = DEFAULT_DATA_DIR
        dry_run = False
    _DEFAULT = CycleJournal(data_dir=str(data_dir), dry_run=bool(dry_run))
    return _DEFAULT


def safe_call(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:
        log.warning("CycleJournal observer call failed: %s", getattr(fn, "__name__", "<?>"), exc_info=True)

