"""Runtime data models for LumaPresence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    CONF_DESTINATION,
    CONF_ORIGIN,
    CONF_PATH,
    MODE_SUN,
    STOP_KEEP_IF_NIGHT,
    signal_update,
)

if TYPE_CHECKING:
    from .engine import SimulationEngine
    from .learning import LearningGate
    from .light_control import LightController
    from .sequences import SequenceEngine


@dataclass
class LumaSettings:
    """User-adjustable settings, backed by the integration's config entities."""

    mode: str = MODE_SUN
    stop_behavior: str = STOP_KEEP_IF_NIGHT
    min_lights: int = 2
    max_lights: int = 5
    min_front_lights: int = 2
    min_rear_lights: int = 0
    min_dwell: float = 5.0
    max_dwell: float = 15.0
    stop_interval: float = 90.0
    start_time: time = time(18, 30)
    end_time: time = time(0, 30)
    late_end_time: time = time(4, 0)
    late_arrival: bool = False
    delayed_start: bool = True
    delayed_end: bool = True
    learning_paused: bool = False
    sequences_enabled: bool = False
    sequence_start_time: time = time(0, 45)
    sequence_min_interval: int = 45
    sequence_max_interval: int = 90


SCHEDULE_SETTINGS = frozenset(
    {
        "mode",
        "start_time",
        "end_time",
        "late_end_time",
        "late_arrival",
        "delayed_start",
        "delayed_end",
    }
)

SEQUENCE_SETTINGS = frozenset(
    {
        "sequences_enabled",
        "sequence_start_time",
        "sequence_min_interval",
        "sequence_max_interval",
        "delayed_start",
    }
)


@dataclass(frozen=True)
class Route:
    """A night route: origin, ordered path and destination lights."""

    route_id: str
    name: str
    origin: str
    path: tuple[str, ...]
    destination: str

    @classmethod
    def from_data(cls, route_id: str, name: str, data: Mapping[str, Any]) -> Route:
        """Build a route from the data of its config subentry."""
        return cls(
            route_id=route_id,
            name=name,
            origin=data[CONF_ORIGIN],
            path=tuple(data.get(CONF_PATH) or ()),
            destination=data[CONF_DESTINATION],
        )

    @property
    def lights(self) -> list[str]:
        """All the lights of the route, in walking order."""
        return [self.origin, *self.path, self.destination]


@dataclass
class LumaRuntime:
    """Objects shared by all platforms of one config entry."""

    hass: HomeAssistant
    entry_id: str
    version: str
    config: dict[str, Any]
    settings: LumaSettings
    controller: LightController
    engine: SimulationEngine
    sequences: SequenceEngine
    learning: LearningGate

    async def async_setting_changed(self, key: str) -> None:
        """React to a setting changed from one of the config entities."""
        if key == "learning_paused":
            self.learning.async_evaluate()
        if key in SCHEDULE_SETTINGS:
            await self.engine.async_settings_changed()
        if key in SEQUENCE_SETTINGS:
            await self.sequences.async_settings_changed(key)
        async_dispatcher_send(self.hass, signal_update(self.entry_id))


type LumaConfigEntry = ConfigEntry[LumaRuntime]
