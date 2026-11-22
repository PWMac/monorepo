import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="oe_test_")
os.environ.setdefault("ORACLE_EDGE_DATA", _tmp)
os.environ.setdefault("ORACLE_EDGE_LOGS", _tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_sec_ceo_network(monkeypatch):
    """CEO look-ups read SEC certifications over the network; tests get "SEC unreachable" unless they say otherwise."""
    from oracle_edge.sources import sec_ceo

    monkeypatch.setattr(sec_ceo, "current", lambda cik, refresh=False: None)
