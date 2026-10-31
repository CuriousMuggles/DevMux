# DevMux - Test Suite

import sys
from pathlib import Path
for _plugin in ("usb-relay", "db-flash", "mijia"):
    sys.path.append(str(Path(__file__).resolve().parents[1] / "plugins" / _plugin))
