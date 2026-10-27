"""Rehearse: scenario-driven webhook and API failure testing."""

__version__ = "0.1.0"

from .errors import ScenarioError  # noqa: E402
from .player import Player, StepResult  # noqa: E402
from .recorder import Recorder  # noqa: E402
from .server import FaultServer  # noqa: E402

__all__ = ["Player", "StepResult", "FaultServer", "Recorder", "ScenarioError", "__version__"]
