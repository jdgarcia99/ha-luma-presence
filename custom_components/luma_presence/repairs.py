"""Repairs: replace an entity that no longer exists, or remove it everywhere."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, split_entity_id
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .config_flow import async_texts, text
from .const import DOMAIN
from .references import (
    MISSING_ISSUE_PREFIX,
    Usage,
    async_replace,
    entity_usages,
    learned_event_count,
    plan_replacement,
    removal_consequences,
    replacement_domains,
    replacement_filter,
)

CONF_REPLACEMENT = "replacement"
STEP_REPLACE = "replace"
STEP_REMOVE = "remove"


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the flow that fixes a missing-entity issue."""
    assert data is not None
    return ReplaceEntityFlow(str(data["entry_id"]), str(data["entity_id"]))


class ReplaceEntityFlow(RepairsFlow):
    """Replace a missing entity with another one, or remove it everywhere.

    The options, every route and deterrence trigger, and (for a replacement)
    the learned data are updated in one go, and the entry is reloaded.
    """

    def __init__(self, entry_id: str, entity_id: str) -> None:
        """Initialize the flow."""
        self._entry_id = entry_id
        self._entity_id = entity_id
        # Not self.issue_id: where it comes from changed between Home Assistant versions.
        self._issue = MISSING_ISSUE_PREFIX.format(entry_id=entry_id) + entity_id

    def _usages(self) -> tuple[ConfigEntry, list[Usage]]:
        """The entry and the uses of the entity; aborts if there is nothing to fix."""
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        usages = entity_usages(entry).get(self._entity_id) if entry else None
        if entry is None or not usages:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue)
            raise data_entry_flow.AbortFlow("not_used")
        if self.hass.states.get(self._entity_id) is not None:
            ir.async_delete_issue(self.hass, DOMAIN, self._issue)
            raise data_entry_flow.AbortFlow("entity_back")
        return entry, usages

    async def _placeholders(self, entry: ConfigEntry, usages: list[Usage]) -> dict[str, str]:
        texts = await async_texts(self.hass)
        lines = [
            text(texts, f"usage_{usage.key}", name=usage.name or "") for usage in usages
        ]
        if count := learned_event_count(entry, self._entity_id):
            lines.append(text(texts, "usage_learned", count=count))
        return {
            "title": entry.title,
            "entity_id": self._entity_id,
            "usages": "\n".join(f"- {line}" for line in lines),
        }

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Where the entity is used; replace it or remove it."""
        entry, usages = self._usages()
        return self.async_show_menu(
            step_id="init",
            menu_options=[STEP_REPLACE, STEP_REMOVE],
            description_placeholders=await self._placeholders(entry, usages),
        )

    async def async_step_replace(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Choose the entity that takes its place everywhere."""
        entry, usages = self._usages()
        errors: dict[str, str] = {}
        if user_input is not None:
            new = str(user_input[CONF_REPLACEMENT])
            if new == self._entity_id or split_entity_id(new)[0] not in (
                replacement_domains(self._entity_id, usages)
            ):
                errors[CONF_REPLACEMENT] = "replacement_not_allowed"
            elif self.hass.states.get(new) is None:
                errors[CONF_REPLACEMENT] = "replacement_missing"
            else:
                plan = plan_replacement(self.hass, entry, self._entity_id, new)
                errors = plan.errors
                if not errors:
                    await async_replace(self.hass, entry, self._entity_id, new, plan)
                    return self.async_create_entry(data={})
        schema = vol.Schema(
            {
                vol.Required(CONF_REPLACEMENT): EntitySelector(
                    EntitySelectorConfig(filter=replacement_filter(self._entity_id, usages))
                )
            }
        )
        return self.async_show_form(
            step_id=STEP_REPLACE,
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders=await self._placeholders(entry, usages),
        )

    async def async_step_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        """Remove the entity from the options, routes and deterrence triggers.

        Before confirming, the form says what else is deleted or turned off.
        """
        entry, usages = self._usages()
        plan = plan_replacement(self.hass, entry, self._entity_id, None)
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = plan.errors
            if not errors:
                await async_replace(self.hass, entry, self._entity_id, None, plan)
                return self.async_create_entry(data={})
        texts = await async_texts(self.hass)
        consequences = [
            text(texts, key, name=name)
            for key, name in removal_consequences(entry, self._entity_id, plan)
        ] or [text(texts, "removes_nothing_else")]
        return self.async_show_form(
            step_id=STEP_REMOVE,
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                **await self._placeholders(entry, usages),
                "consequences": "\n".join(f"- {line}" for line in consequences),
            },
        )
