"""Morning report: a summary of last night, at sunrise.

After a night in which the simulation, a night sequence, a cover, an
ambience player or a deterrence reaction did something, the
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

from .ai import AiAssistant, area_names, build_keys, report_payload
from .const import DOMAIN, EVENT_ACTION, EVENT_NIGHT_REPORT, OWNER_AMBIENCE
from .dream import DreamEngine
from .light_control import LightController
from .models import LumaSettings
from .plan import NightPlan, PlanPlayer, clock_text, night_of
from .scope import entity_area_id

_LOGGER = logging.getLogger(__name__)

_STARTS = ("simulation_started", "sequence_started")
_DEFAULT_TEXT = (
    "Last night ({mode}): {lights} lights switched {switches} times, rooms: {rooms}. "
    "Realism: {score}. Failures: {failures}. Covers moved: {covers}. "
    "Audio sessions: {ambience}. Deterrence reactions: {deterrence}."
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
        ai: AiAssistant | None = None,
    ) -> None:
        """Initialize the reporter."""
        self.hass = hass
        self._entry_id = entry_id
        self._settings = settings
        self._controller = controller
        self._player = player
        self._dream = dream
        self._ai = ai
        # The last report sent (diagnostics).
        self.last_report: dict[str, Any] | None = None
        self._notify_target = notify_target or None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._reset()

    def _reset(self) -> None:
        self._active = False
        self._lights: set[str] = set()
        self._switches = 0
        self._cover_moves = 0
        self._covers: set[str] = set()
        self._ambience_sessions = 0
        self._deterrence = 0
        self._deterrence_facades: list[str] = []
        self._routes = 0

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
        action = data.get("action")
        if action in _STARTS:
            self._active = True
            if action == "sequence_started":
                self._routes += 1
        elif action == "deterrence":
            self._active = True
            self._deterrence += 1
            if data.get("facade"):
                self._deterrence_facades.append(str(data["facade"]))
        elif data.get("kind") == "cover":
            self._active = True
            self._cover_moves += 1
            self._covers.add(data["entity_id"])
        elif data.get("kind") == OWNER_AMBIENCE:
            if action == "ambience_started":
                self._active = True
                self._ambience_sessions += 1
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
        self.last_report = payload
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
            "cover_moves": self._cover_moves,
            "covers": sorted(self._covers),
            "ambience_sessions": self._ambience_sessions,
            "deterrence_reactions": self._deterrence,
            "deterrence_facades": list(self._deterrence_facades),
            "routes_run": self._routes,
            "scenario": plan.scenario if tonight and plan else None,
            "explanation": plan.explanation if tonight and plan else None,
            "room_times": self._room_times(plan) if tonight and plan else {},
            "failures_by_room": self._failures_by_room(),
        }

    def _room_of(self, entity_id: str) -> str:
        return entity_area_id(self.hass, entity_id) or f"light:{entity_id}"

    def _room_times(self, plan: NightPlan) -> dict[str, list[str]]:
        """Rooms used last night and when, by room id."""
        start, end = plan.window
        open_at = {t: start for t, v in plan.state_at(start).items() if v == "on"}
        room_of = {
            t.target: t.room or self._room_of(t.target) for t in plan.transitions
        } | {t: self._room_of(t) for t in open_at}
        times: dict[str, list[str]] = {}
        for transition in plan.in_window():
            if transition.state == "on":
                open_at.setdefault(transition.target, transition.seconds)
            elif (began := open_at.pop(transition.target, None)) is not None:
                times.setdefault(room_of[transition.target], []).append(
                    f"{clock_text(began)}-{clock_text(transition.seconds)}"
                )
        for target, began in open_at.items():
            times.setdefault(room_of[target], []).append(f"{clock_text(began)}-{clock_text(end)}")
        return times

    def _failures_by_room(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for entity_id, count in self._controller.failure_counts.items():
            room = self._room_of(entity_id)
            result[room] = result.get(room, 0) + count
        return result

    def _context(self) -> list[str]:
        if self._dream is None:
            return []
        return list(self._dream.status.get("context", []))

    async def _async_ai_text(self, payload: dict[str, Any]) -> str | None:
        """The report written by the AI, with real room names, or None."""
        if self._ai is None or not self._ai.enabled or not self._ai.report_enabled:
            return None
        rooms = set(payload.get("room_times", {})) | set(payload.get("failures_by_room", {}))
        names, aliases = area_names(self.hass, rooms)
        keys = build_keys(rooms, names, aliases)
        return await self._ai.async_report(report_payload(payload, keys), keys)

    async def _async_notify(self, payload: dict[str, Any]) -> None:
        if (message := await self._async_ai_text(payload)) is not None:
            await self._async_send(message)
            return
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
            covers=payload["cover_moves"],
            ambience=payload["ambience_sessions"],
            deterrence=payload["deterrence_reactions"],
        )
        if payload.get("scenario"):
            message += f" ({payload['scenario']})"
        await self._async_send(message)

    async def _async_send(self, message: str) -> None:
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
