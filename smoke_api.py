"""
smoke_api.py

Start a minimal API instance for smoke-testing without starting TradingBot.
Only endpoints that don't require an initialized bot (e.g. /health, /) will work.
"""

from __future__ import annotations

from spot.api_server_spot import create_app


def _bot_getter():
    return None


app = create_app(_bot_getter)

