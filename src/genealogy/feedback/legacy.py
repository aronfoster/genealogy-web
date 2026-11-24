from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import UUID


@dataclass(frozen=True)
class FeedbackConfig:
    api_key: str = field(repr=False)
    team_id: str
    state_id: str

    def __post_init__(self) -> None:
        if (
            not self.api_key.startswith("lin_api_")
            or not self.api_key.isascii()
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in self.api_key)
        ):
            raise ValueError("GENEALOGY_LINEAR_API_KEY must be a Linear personal API key")
        for name, value in (("TEAM_ID", self.team_id), ("STATE_ID", self.state_id)):
            try:
                if str(UUID(value)) != value:
                    raise ValueError
            except ValueError:
                raise ValueError(f"GENEALOGY_LINEAR_{name} must be a canonical UUID") from None

    @classmethod
    def from_env(cls) -> FeedbackConfig | None:
        mode = os.environ.get("GENEALOGY_FEEDBACK_MODE", "off")
        if mode == "off":
            return None
        if mode != "on":
            raise ValueError("GENEALOGY_FEEDBACK_MODE must be on or off")
        return cls(
            api_key=os.environ.get("GENEALOGY_LINEAR_API_KEY", ""),
            team_id=os.environ.get("GENEALOGY_LINEAR_TEAM_ID", ""),
            state_id=os.environ.get("GENEALOGY_LINEAR_STATE_ID", ""),
        )
