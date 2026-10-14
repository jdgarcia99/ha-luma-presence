"""Runtime data models for LumaPresence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import MODE_SUN, STOP_KEEP_IF_NIGHT, signal_update

if TYPE_CHECKING:
    from .engine import SimulationEngine
    from .learning import LearningGate


@dataclass
class LumaSettings:
    """User-adjustable settings, backed by the integration's config entities."""

    mode: str = MODE_SUN
    stop_behavior: str = STOP_KEEP_IF_NIGHT
    min_lights: int = 2
    max_lights: int = 5
    min_front_lights: int = 2
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


@dataclass
class LumaRuntime:
    """Objects shared by all platforms of one config entry."""

    hass: HomeAssistant
    entry_id: str
    config: dict[str, Any]
    settings: LumaSettings
    engine: SimulationEngine
    learning: LearningGate

    async def async_setting_changed(self, key: str) -> None:
        """React to a setting changed from one of the config entities."""
        if key == "learning_paused":
            self.learning.async_evaluate()
        elif key in SCHEDULE_SETTINGS:
            await self.engine.async_settings_changed()
        async_dispatcher_send(self.hass, signal_update(self.entry_id))


type LumaConfigEntry = ConfigEntry[LumaRuntime]
