import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dhan_autotrader.broker import DhanBroker  # noqa: E402

DhanBroker.LTP_MIN_INTERVAL = 0  # no rate-limit sleeps in tests
