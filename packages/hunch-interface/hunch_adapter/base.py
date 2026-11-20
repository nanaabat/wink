from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable


class Observation(TypedDict, total=False):
    kind: str            # "daylight", "predicted_atmosphere", "time_of_day", ...
    value: Any
    label: str
    source: str          # where this came from (geometry, model guess, user input)
    provider: str
    mock: bool


@runtime_checkable
class HunchProvider(Protocol):
    name: str
    is_mock: bool

    def observe(self, context: dict) -> list[Observation]: ...

    def propose_goal(self, observations: list[Observation], preferences: list[dict]) -> dict:
        """Return a GoalProposal document."""

    def record_feedback(self, feedback: dict) -> None: ...
