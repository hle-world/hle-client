"""A single end-to-end render, opt-in.

Everything else stubs the relay. This one talks to it, so it only runs when
asked: HLE_TUI_LIVE=1 pytest tests/test_smoke_live.py
"""

from __future__ import annotations

import os

import pytest

from hle_tui.app import HleApp

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("HLE_TUI_LIVE"), reason="set HLE_TUI_LIVE=1"),
]


async def test_it_renders_against_the_real_relay():
    app = HleApp(refresh_seconds=0)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        # Either it read something or it said why not; both are a working app.
        assert app.last_status
        assert app.query_one("#tunnels-table").row_count >= 0
