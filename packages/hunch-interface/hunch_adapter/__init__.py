"""hunch interface for wink: the provider protocol, the no emotion claims guard, and a deterministic mock.
the hunch engine itself is not part of this repo."""
from .base import HunchProvider, Observation
from .mock import MockHunchProvider
from .guard import assert_no_emotion_claims, EmotionClaimError

__version__ = "0.1.0"
__all__ = ["HunchProvider", "Observation", "MockHunchProvider", "assert_no_emotion_claims", "EmotionClaimError"]
