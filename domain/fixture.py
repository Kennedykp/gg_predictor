"""
Fixture contract.

Mirrors exactly the dict `espn.get_fixtures` produces today. Nothing added.

`kickoff` is kept as the raw provider string rather than being parsed into a
datetime: Epic 0 recorded unresolved timezone handling around fixture dates, and
parsing it here would quietly pick a timezone policy. That decision belongs with
the date/timezone work, not with this sub-epic.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional

__all__ = ["Fixture"]


@dataclass(frozen=True)
class Fixture:
    """A single scheduled match."""

    fixture_id: str
    league_id: str
    league_name: str
    home_team_id: str
    home_team_name: str
    away_team_id: str
    away_team_name: str
    kickoff: Optional[str] = None
    status: Optional[str] = None

    @classmethod
    def from_provider_dict(cls, data: Dict[str, Any]) -> "Fixture":
        """Adapter for the dict shape `espn.get_fixtures` returns."""
        return cls(
            fixture_id=str(data.get("fixture_id", "")),
            league_id=str(data.get("league_id", "")),
            league_name=str(data.get("league_name", "")),
            home_team_id=str(data.get("home_team_id", "")),
            home_team_name=str(data.get("home_team_name", "")),
            away_team_id=str(data.get("away_team_id", "")),
            away_team_name=str(data.get("away_team_name", "")),
            kickoff=data.get("datetime"),
            status=data.get("status"),
        )

    @property
    def label(self) -> str:
        """`Home vs Away`, for log lines and rejection messages."""
        return f"{self.home_team_name} vs {self.away_team_name}"
