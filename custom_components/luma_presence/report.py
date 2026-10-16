"""Morning report: a summary of last night, at sunrise.

After a night in which the simulation or a night sequence ran, the
``luma_presence_night_report`` event is fired with what happened and, if a
notify target is configured, a short translated summary is sent.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_sunrise
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .const import DOMAIN, EVENT_ACTION, EVENT_NIGHT_REPORT
from .dream import DreamEngine
from .light_control import LightController
from .models import LumaSettings
from .plan import PlanPlayer, night_of

_LOGGER = logging.getLogger(__name__)

_STARTS = ("simulation_started", "sequence_started")
_DEFAULT_TEXT = (
    "Last night ({mode}): {lights} lights switched {switches} times, rooms: {rooms}. "
    "Realism: {score}. Failures: {failures}."
)


class NightReporter:
    """Collects what LumaPresence did during the night and reports it."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        settings: LumaSettings,
        controller: LightController,
        player: PlanPlayer,
        notify_target: str | None,
        dream: DreamEngine | None = None,
    ) -> None:
        """Initialize the reporter."""
        self.hass = hass
        self._entry_id = entry_id
        self._settings = settings
        self._controller = controller
        self._player = player
        self._dream = dream
        self._notify_target = notify_target or None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._reset()

    def _reset(self) -> None:
        self._active = False
        self._lights: set[str] = set()
        self._switches = 0

    @callback
    def async_start(self) -> None:
        """Listen to LumaPresence's actions and to sunrise."""
        self._unsubs.append(self.hass.bus.async_listen(EVENT_ACTION, self._handle_action))
        self._unsubs.append(async_track_sunrise(self.hass, self._handle_sunrise))

    @callback
    def async_shutdown(self) -> None:
        """Stop listening."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()

    @callback
    def _handle_action(self, event: Event) -> None:
        data = event.data
        if data.get("entry_id") != self._entry_id:
            return
        if data.get("action") in _STARTS:
            self._active = True
        elif data.get("entity_id"):
            self._switches += 1
            if data.get("action") == "on":
                self._lights.add(data["entity_id"])

    @callback
    def _handle_sunrise(self, _now: datetime | None = None) -> None:
        if not self._active and not self._switches:
            self._reset()
            return
        payload = self.report()
        # A new night starts: failures are reset, unresponsive issues of
        # lights that obeyed all night are cleared.
        self._controller.end_night()
        self.hass.bus.async_fire(EVENT_NIGHT_REPORT, payload)
        if self._notify_target:
            self.hass.async_create_task(
                self._async_notify(payload), f"{DOMAIN}_night_report_{self._entry_id}"
            )
        self._reset()

    def report(self) -> dict[str, Any]:
        """What happened last night."""
        night = night_of(dt_util.as_local(dt_util.utcnow()))
        plan = self._player.plan
        tonight = plan is not None and not plan.preview and plan.night == night
        return {
            "entry_id": self._entry_id,
            "night": night.isoformat(),
            "mode": self._settings.mode,
            "source": plan.source if tonight and plan else None,
            "score": plan.score if tonight and plan else None,
            "rooms": plan.rooms if tonight and plan else [],
            "lights_switched": len(self._lights),
            "lights": sorted(self._lights),
            "switches": self._switches,
            "failures": dict(self._controller.failure_counts),
            "context": self._context(),
        }

    def _context(self) -> list[str]:
        if self._dream is None:
            return []
        return list(self._dream.status.get("context", []))

    async def _async_notify(self, payload: dict[str, Any]) -> None:
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "exceptions", [DOMAIN]
        )
        text = translations.get(
            f"component.{DOMAIN}.exceptions.night_report.message", _DEFAULT_TEXT
        )
        message = text.format(
            mode=payload["mode"],
            lights=payload["lights_switched"],
            switches=payload["switches"],
            rooms=", ".join(payload["rooms"]) or "-",
            score="-" if payload["score"] is None else f"{payload['score']:.0f}",
            failures=", ".join(
                f"{entity_id} ×{count}" for entity_id, count in payload["failures"].items()
            )
            or "0",
        )
        target = str(self._notify_target)
        try:
            if target.startswith("notify.") and self.hass.states.get(target) is not None:
                await self.hass.services.async_call(
                    "notify",
                    "send_message",
                    {"entity_id": target, "message": message, "title": "LumaPresence"},
                    blocking=True,
                )
            else:
                await self.hass.services.async_call(
                    "notify",
                    target.removeprefix("notify."),
                    {"message": message, "title": "LumaPresence"},
                    blocking=True,
                )
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.warning("Could not send the night report to %s: %s", target, err)
