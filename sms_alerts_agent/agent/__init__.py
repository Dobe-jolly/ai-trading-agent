"""SMS_ALERTS osTicket agent."""
import sys
from pathlib import Path

__version__ = "0.2.0"

# The code both agents share (agentcore) sits next to this agent's folder. Make it importable when the
# agent runs from its own folder (python -m agent) or its tests run, without installing anything.
_ROOT = Path(__file__).resolve().parent.parent.parent
if (_ROOT / "agentcore").is_dir() and str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
