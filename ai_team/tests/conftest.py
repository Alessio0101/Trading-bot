import threading
from pathlib import Path

import pytest
from risk_gateway.gateway import PriceSource, RiskGateway
from risk_gateway.server import ThreadingHTTPServer, make_handler

from ai_team.data import SyntheticMarketData


@pytest.fixture
def market():
    return SyntheticMarketData(seed=11, drift=0.0012)


@pytest.fixture
def gateway(tmp_path: Path, market):
    gw = RiskGateway(tmp_path / "gw", PriceSource("synthetic"))
    gw.prices._src = market
    return gw


@pytest.fixture
def gateway_url(gateway):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(gateway))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture(scope="session")
def verified_cycle():
    """One full coordinator cycle shared by the per-agent tests (it is the expensive part)."""
    from ai_team.verify import run_live_cycle

    return run_live_cycle()
