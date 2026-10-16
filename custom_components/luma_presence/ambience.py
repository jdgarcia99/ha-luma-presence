"""Ambience: a radio or TV sound in an occupied room, during a real absence.

A media player plays while its room is occupied tonight (one of the room's
simulation lights is on, switched on by the simulation, whatever the mode),
only while the simulation runs during a real absence, never after the quiet
time, and never while it is day. Its previous volume and source are restored
afterwards.

The TV backlight needs nothing here: a WLED strip or similar is a light.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, time
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AMBIENCE_MEDIA,
    CONF_AMBIENCE_PLAYERS,
    CONF_AMBIENCE_QUIET_AFTER,
    CONF_AMBIENCE_VOLUME,
    CONF_LIGHTS,
    DEFAULT_AMBIENCE_QUIET_AFTER,
    DEFAULT_AMBIENCE_VOLUME,
    DOMAIN,
    OWNER_AMBIENCE,
    OWNER_SIMULATION,
    signal_update,
)
from .light_control import LightController
from .plan import DAY_S, NOON_S
from .scope import entity_area_id

if TYPE_CHECKING:
    from .engine import SimulationEngine
    from .plan import PlanPlayer

_LOGGER = logging.getLogger(__name__)

MEDIA_TYPE = "music"


def parse_media(text: str, players: list[str]) -> dict[str, str]:
    """Media per player from lines "media_player.x = content id".

    A line without a player applies to every player without a line of its
    own. Raises ValueError for a line naming an unknown player.
    """
    default: str | None = None
    media: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        player, sep, content = line.partition("=")
        if sep and player.strip().startswith("media_player."):
            if player.strip() not in players or not content.strip():
                raise ValueError(player.strip())
            media[player.strip()] = content.strip()
        else:
            default = line
    if default is not None:
        for player in players:
            media.setdefault(player, default)
    return media


@dataclass
class _Session:
    """What a player was doing before LumaPresence used it."""

    state: str
    volume: float | None
    source: str | None


class AmbienceEngine:
    """Plays sound in occupied rooms during a real absence."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        config: Mapping[str, Any],
        controller: LightController,
        engine: SimulationEngine,
        player: PlanPlayer,
    ) -> None:
        """Initialize the ambience engine."""
        self.hass = hass
        self._entry_id = entry_id
        self._controller = controller
        self._engine = engine
        self._player = player
        self.players: list[str] = list(config.get(CONF_AMBIENCE_PLAYERS) or [])
        try:
            self._media = parse_media(
                str(config.get(CONF_AMBIENCE_MEDIA) or ""), self.players
            )
        except ValueError:
            self._media = {}
        self._volume = float(config.get(CONF_AMBIENCE_VOLUME, DEFAULT_AMBIENCE_VOLUME))
        self._quiet_after = _time(
            config.get(CONF_AMBIENCE_QUIET_AFTER), DEFAULT_AMBIENCE_QUIET_AFTER
        )
        self._lights: list[str] = list(config.get(CONF_LIGHTS) or [])
        self.sessions: dict[str, _Session] = {}
        self._busy: set[str] = set()
        self._unsubs: list[CALLBACK_TYPE] = []
        # Daylight guard (set by the integration).
        self.daylight: Callable[[], bool] = lambda: False

    @property
    def configured(self) -> bool:
        """Return True when at least one player has something to play."""
        return any(player in self._media for player in self.players)

    @callback
    def async_start(self) -> None:
        """Follow the simulation lights, the engine and the quiet time."""
        if not self.configured:
            return
        if self._lights:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, self._lights, self._handle_light
                )
            )
        self._unsubs.append(
            async_dispatcher_connect(
                self.hass, signal_update(self._entry_id), self.async_evaluate
            )
        )
        quiet = self._quiet_after
        self._unsubs.append(
            async_track_time_change(
                self.hass,
                self._handle_quiet,
                hour=quiet.hour,
                minute=quiet.minute,
                second=quiet.second,
            )
        )
        self.async_evaluate()

    async def async_shutdown(self) -> None:
        """Stop listening and give the players back."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for player in list(self.sessions):
            await self._async_stop(player)

    @callback
    def _handle_light(self, _event: Event[EventStateChangedData]) -> None:
        self.async_evaluate()

    @callback
    def _handle_quiet(self, _now: datetime) -> None:
        self.async_evaluate()

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------
    def quiet(self) -> bool:
        """After the quiet time (until noon) no sound is played."""
        local = dt_util.as_local(dt_util.utcnow()).time()

        def since_noon(clock: time) -> int:
            seconds = clock.hour * 3600 + clock.minute * 60 + clock.second
            return (seconds - NOON_S) % DAY_S

        return since_noon(local) >= since_noon(self._quiet_after)

    def occupied(self, player: str) -> bool:
        """The player's room has a light on that the simulation switched on."""
        area = entity_area_id(self.hass, player)
        if area is None:
            return False
        for light in self._lights:
            if entity_area_id(self.hass, light) != area:
                continue
            state = self.hass.states.get(light)
            if (
                state is not None
                and state.state == STATE_ON
                and self._controller.owner_of(light) == OWNER_SIMULATION
            ):
                return True
        return False

    def _wanted(self, player: str) -> bool:
        return (
            player in self._media
            and self._engine.status == "simulating"
            and self._player.real_absence()
            and not self.daylight()
            and not self.quiet()
            and self.occupied(player)
        )

    @callback
    def async_evaluate(self) -> None:
        """Start or stop each player to match the rules."""
        for player in self.players:
            if player in self._busy:
                continue
            wanted = self._wanted(player)
            if wanted and player not in self.sessions:
                self._run(player, self._async_start(player))
            elif not wanted and player in self.sessions:
                self._run(player, self._async_stop(player))

    @callback
    def _run(self, player: str, job: Any) -> None:
        self._busy.add(player)

        async def _job() -> None:
            try:
                await job
            finally:
                self._busy.discard(player)

        self.hass.async_create_task(_job(), f"{DOMAIN}_ambience_{self._entry_id}")

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------
    async def _async_start(self, player: str) -> None:
        state = self.hass.states.get(player)
        if state is None or state.state in ("unavailable", "unknown"):
            return
        self.sessions[player] = _Session(
            state=state.state,
            volume=state.attributes.get("volume_level"),
            source=state.attributes.get("source"),
        )
        target = {ATTR_ENTITY_ID: player}
        await self._controller.async_call(
            "media_player", "volume_set", {**target, "volume_level": self._volume}
        )
        await self._controller.async_call(
            "media_player",
            "play_media",
            {
                **target,
                "media_content_id": self._media[player],
                "media_content_type": MEDIA_TYPE,
            },
            entity_id=player,
            action="ambience_started",
            kind=OWNER_AMBIENCE,
        )

    async def _async_stop(self, player: str) -> None:
        session = self.sessions.pop(player, None)
        if session is None:
            return
        target = {ATTR_ENTITY_ID: player}
        if session.state == STATE_OFF:
            await self._controller.async_call("media_player", "turn_off", target)
        else:
            await self._controller.async_call("media_player", "media_stop", target)
        if session.volume is not None:
            await self._controller.async_call(
                "media_player", "volume_set", {**target, "volume_level": session.volume}
            )
        if session.source is not None:
            await self._controller.async_call(
                "media_player", "select_source", {**target, "source": session.source}
            )
        self._controller.record(player, "ambience_stopped", kind=OWNER_AMBIENCE)


def _time(value: Any, default: str) -> time:
    try:
        return time.fromisoformat(str(value or default))
    except ValueError:
        return time.fromisoformat(default)
