import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import pytest

import cycle_journal
import telegram_bot


@dataclass
class _Pos:
    mint: str
    lower_price: float
    upper_price: float
    current_price: float
    amount_sol: float
    amount_usdc: float
    total_value_usd: float
    fees_sol: float = 0.0
    fees_usdc: float = 0.0
    is_demo: bool = False


def _read_jsonl_lines(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f.read().splitlines() if line.strip()]


def test_1_divergence_matches_manual_cycle_numbers():
    # Given from user:
    # open: 0.7372 SOL + $52.24 USDC, open price $73.11, open value $106.14
    # close: price $72.63, close value $105.57
    div = cycle_journal.compute_divergence_usd(
        open_sol_qty=0.7372,
        open_usdc_qty=52.24,
        close_price=72.63,
        close_position_value_usd=105.57,
    )
    would_be = cycle_journal.compute_would_be_value_usd(open_sol_qty=0.7372, open_usdc_qty=52.24, close_price=72.63)
    print(f"would_be={would_be:.2f} divergence={div:.2f}")
    assert would_be == pytest.approx(105.78, abs=0.01)
    assert div == pytest.approx(-0.21, abs=0.01)


def test_2_market_character_efficiency_trend_vs_sideways(tmp_path):
    j = cycle_journal.CycleJournal(data_dir=str(tmp_path), dry_run=False)
    t0 = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)

    # Trend series: 100 -> 110 -> 120 -> 130 (no returns) => efficiency ~ 1
    pos_open = _Pos(
        mint="M1",
        lower_price=90,
        upper_price=110,
        current_price=100.0,
        amount_sol=1.0,
        amount_usdc=0.0,
        total_value_usd=100.0,
    )
    j.on_open(pos_open, range_width_pct=0.5, now=t0)
    j.on_monitor_tick(110.0, now=t0 + timedelta(minutes=5))
    j.on_monitor_tick(120.0, now=t0 + timedelta(minutes=10))
    j.on_monitor_tick(130.0, now=t0 + timedelta(minutes=15))
    pos_close = _Pos(
        mint="M1",
        lower_price=90,
        upper_price=110,
        current_price=130.0,
        amount_sol=1.0,
        amount_usdc=0.0,
        total_value_usd=130.0,
        fees_sol=0.0,
        fees_usdc=0.0,
    )
    j.capture_close_snapshot(pos_close, now=t0 + timedelta(minutes=15))
    j.finalize_pending_cycle()
    rec = _read_jsonl_lines(j.journal_path)[0]
    eff_trend = rec["efficiency"]

    # Sideways series: 100 -> 110 -> 100 (returns) => efficiency ~ 0
    j2 = cycle_journal.CycleJournal(data_dir=str(tmp_path / "s2"), dry_run=False)
    j2.on_open(pos_open, range_width_pct=0.5, now=t0)
    j2.on_monitor_tick(110.0, now=t0 + timedelta(minutes=5))
    j2.on_monitor_tick(100.0, now=t0 + timedelta(minutes=10))
    pos_close2 = _Pos(
        mint="M1",
        lower_price=90,
        upper_price=110,
        current_price=100.0,
        amount_sol=1.0,
        amount_usdc=0.0,
        total_value_usd=100.0,
        fees_sol=0.0,
        fees_usdc=0.0,
    )
    j2.capture_close_snapshot(pos_close2, now=t0 + timedelta(minutes=10))
    j2.finalize_pending_cycle()
    rec2 = _read_jsonl_lines(j2.journal_path)[0]
    eff_sideways = rec2["efficiency"]

    print(f"eff_trend={eff_trend:.6f} eff_sideways={eff_sideways:.6f}")
    assert eff_trend == pytest.approx(1.0, abs=1e-9)
    assert eff_sideways == pytest.approx(0.0, abs=1e-9)


def test_3_full_cycle_writes_one_jsonl_line(tmp_path):
    j = cycle_journal.CycleJournal(data_dir=str(tmp_path), dry_run=False)
    t0 = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    pos_open = _Pos(
        mint="M2",
        lower_price=99.0,
        upper_price=101.0,
        current_price=100.0,
        amount_sol=0.7372,
        amount_usdc=52.24,
        total_value_usd=106.14,
    )
    j.on_open(pos_open, range_width_pct=0.5, now=t0)
    for i, p in enumerate([100.5, 99.7, 100.2, 100.0], start=1):
        j.on_monitor_tick(p, now=t0 + timedelta(minutes=5 * i))
    pos_close = _Pos(
        mint="M2",
        lower_price=99.0,
        upper_price=101.0,
        current_price=72.63,
        amount_sol=0.0,
        amount_usdc=0.0,
        total_value_usd=105.57,
        fees_sol=0.0030,
        fees_usdc=0.21,
    )
    j.capture_close_snapshot(pos_close, now=t0 + timedelta(hours=1))
    j.finalize_pending_cycle()

    lines = _read_jsonl_lines(j.journal_path)
    assert len(lines) == 1
    print(json.dumps(lines[0], ensure_ascii=False, separators=(",", ":")))


def test_4_restart_mid_cycle_marks_incomplete(tmp_path):
    t0 = datetime(2026, 7, 28, 12, 0, 0, tzinfo=timezone.utc)
    pos_open = _Pos(
        mint="M3",
        lower_price=99.0,
        upper_price=101.0,
        current_price=100.0,
        amount_sol=1.0,
        amount_usdc=0.0,
        total_value_usd=100.0,
    )
    j = cycle_journal.CycleJournal(data_dir=str(tmp_path), dry_run=False)
    j.on_open(pos_open, range_width_pct=3.0, now=t0)
    j.on_monitor_tick(101.0, now=t0 + timedelta(minutes=5))

    # "Restart": new instance loads same state and marks incomplete.
    j_restart = cycle_journal.CycleJournal(data_dir=str(tmp_path), dry_run=False)
    j_restart.mark_startup_restart_incomplete()

    pos_close = _Pos(
        mint="M3",
        lower_price=99.0,
        upper_price=101.0,
        current_price=100.0,
        amount_sol=1.0,
        amount_usdc=0.0,
        total_value_usd=100.0,
        fees_sol=0.0,
        fees_usdc=0.0,
    )
    j_restart.capture_close_snapshot(pos_close, now=t0 + timedelta(hours=1))
    j_restart.finalize_pending_cycle()
    rec = _read_jsonl_lines(j_restart.journal_path)[0]
    print(f"incomplete={rec['incomplete']} reasons={rec['incomplete_reasons']}")
    assert rec["incomplete"] is True
    assert "restart" in rec["incomplete_reasons"]


def test_5_pnl_empty_journal_message_does_not_crash():
    msg = telegram_bot._render_pnl_message(cycles=[], recent_limit=10)
    print(msg)
    assert "журнал пуст" in msg.lower()

