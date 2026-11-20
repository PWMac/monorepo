import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="oe_test_")
os.environ.setdefault("ORACLE_EDGE_DATA", _tmp)
os.environ.setdefault("ORACLE_EDGE_LOGS", _tmp)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
