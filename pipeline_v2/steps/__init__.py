"""Step result type shared across all pipeline_v2 steps."""
from enum import Enum


class StepResult(Enum):
    CONTINUE = "continue"
    EXIT = "exit"
