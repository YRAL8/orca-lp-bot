import asyncio
import time

import bot_state
import main


def _fail(*_args, **_kwargs):
    raise RuntimeError("This function must not be called while paused")


async def _run() -> None:
    # If pause works, monitor_position must return before any network/chain calls.
    main.get_sol_balance = _fail
    main.get_position = _fail
    main.get_current_price = _fail
    main.rebalance = _fail

    bot_state.bot_paused = True

    t0 = time.perf_counter()
    await main.monitor_position()
    dt = time.perf_counter() - t0

    print(f"OK: monitor_position returned early (paused), elapsed={dt:.6f}s")


if __name__ == "__main__":
    asyncio.run(_run())
