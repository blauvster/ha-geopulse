"""Config, options and reauth flows for the GeoPulse integration.

Connection details and tokens live in `entry.data`; import/export selections
and settings live in `entry.options` so the options flow can change them.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from yarl import URL

from .api import Friend, GeoPulseApiError, GeoPulseAuthError, GeoPulseClient
from .const import (
    CONF_BASE_URL,
    CONF_EXPORT_ENTITIES,
    CONF_EXPORT_ENTITY_TOKENS,
    CONF_EXPORT_RETRY_QUEUE,
    CONF_IMPORT_AGGREGATE_ACCOUNT,
    CONF_IMPORT_FRIEND_IDS,
    CONF_IMPORT_SOURCE_TYPES,
    CONF_POLL_INTERVAL,
    CONF_READ_TOKEN,
    CONF_RECORDER_EXCLUDE,
    CONF_TIMELINE_USERS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    SOURCE_TYPE_HOME_ASSISTANT,
    SOURCE_TYPE_LABELS,
)

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Required(CONF_READ_TOKEN): PASSWORD_SELECTOR,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_READ_TOKEN): PASSWORD_SELECTOR})


@dataclass
class _Account:
    """What a read token can see, as needed by the import picker."""

    user_id: str | None
    source_types: list[str]
    friends: list[Friend]


async def _async_fetch_account(
    hass: HomeAssistant, base_url: str, token: str
) -> tuple[_Account | None, dict[str, str]]:
    """Validate the read token live and collect import candidates."""
    client = GeoPulseClient(async_get_clientsession(hass), base_url, token)
    try:
        configs = await client.async_get_source_configs()
        friends = await client.async_get_friends()
    except GeoPulseAuthError:
        return None, {"base": "invalid_auth"}
    except GeoPulseApiError:
        return None, {"base": "cannot_connect"}
    except Exception:
        _LOGGER.exception("Unexpected error validating GeoPulse connection")
        return None, {"base": "unknown"}

    # There's no "who am I" endpoint on the API-token surface, but every
    # source config and friendship row carries the token owner's userId.
    user_id = next(
        (
            uid
            for uid in [*(c.user_id for c in configs), *(f.user_id for f in friends)]
            if uid
        ),
        None,
    )
    return (
        _Account(
            user_id=user_id,
            # HOME_ASSISTANT is what this integration exports; importing it
            # back would loop (Plan.md §3).
            source_types=sorted(
                {c.type for c in configs if c.type != SOURCE_TYPE_HOME_ASSISTANT}
            ),
            friends=[f for f in friends if f.shares_live_location],
        ),
        {},
    )


def _normalize_base_url(raw: str) -> str | None:
    """Return the URL without trailing slash/query, or None if unusable."""
    try:
        url = URL(raw.strip())
    except ValueError:
        return None
    if url.scheme not in ("http", "https") or not url.host:
        return None
    return str(url.with_query(None).with_fragment(None)).rstrip("/")


def _import_schema(account: _Account, options: Mapping[str, Any]) -> vol.Schema:
    """Import picker; keeps stale selections visible so they can be removed."""
    selected_types = options.get(CONF_IMPORT_SOURCE_TYPES, [])
    source_types = sorted(set(account.source_types) | set(selected_types))
    friend_labels = {
        f.friend_id: f.full_name or f.email or f.friend_id for f in account.friends
    }
    for friend_id in options.get(CONF_IMPORT_FRIEND_IDS, []):
        friend_labels.setdefault(friend_id, friend_id)

    schema: dict[vol.Marker, Any] = {
        vol.Required(
            CONF_IMPORT_AGGREGATE_ACCOUNT,
            default=options.get(CONF_IMPORT_AGGREGATE_ACCOUNT, True),
        ): BooleanSelector(),
    }
    if source_types:
        schema[vol.Optional(CONF_IMPORT_SOURCE_TYPES)] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=t, label=SOURCE_TYPE_LABELS.get(t, t))
                    for t in source_types
                ],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )
        )
    if friend_labels:
        schema[vol.Optional(CONF_IMPORT_FRIEND_IDS)] = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=fid, label=label)
                    for fid, label in friend_labels.items()
                ],
                multiple=True,
                mode=SelectSelectorMode.LIST,
            )
        )
    return vol.Schema(schema)


def _import_options(user_input: Mapping[str, Any]) -> dict[str, Any]:
    return {
        CONF_IMPORT_AGGREGATE_ACCOUNT: user_input[CONF_IMPORT_AGGREGATE_ACCOUNT],
        CONF_IMPORT_SOURCE_TYPES: list(user_input.get(CONF_IMPORT_SOURCE_TYPES, [])),
        CONF_IMPORT_FRIEND_IDS: list(user_input.get(CONF_IMPORT_FRIEND_IDS, [])),
    }


def _has_import(options: Mapping[str, Any]) -> bool:
    return bool(
        options.get(CONF_IMPORT_AGGREGATE_ACCOUNT)
        or options.get(CONF_IMPORT_SOURCE_TYPES)
        or options.get(CONF_IMPORT_FRIEND_IDS)
    )


def _export_schema(hass: HomeAssistant, options: Mapping[str, Any]) -> vol.Schema:
    """Export picker.

    Every device_tracker this integration created is excluded, which is what
    stops export and import from overlapping (Plan.md §1) - no runtime
    "did this come from GeoPulse?" check needed.
    """
    own_entities = [
        entry.entity_id
        for entry in er.async_get(hass).entities.values()
        if entry.platform == DOMAIN
    ]
    return vol.Schema(
        {
            vol.Optional(CONF_EXPORT_ENTITIES): EntitySelector(
                EntitySelectorConfig(
                    domain=Platform.DEVICE_TRACKER,
                    multiple=True,
                    exclude_entities=own_entities,
                )
            ),
            vol.Required(
                CONF_EXPORT_RETRY_QUEUE,
                default=options.get(CONF_EXPORT_RETRY_QUEUE, True),
            ): BooleanSelector(),
        }
    )


FIELD_DEVICE_ID = "device_id"
FIELD_TOKEN = "token"



class _DeviceIdSteps:
    """Walk the export_device_ids form once per exported tracker.

    One screen per tracker, not one form with a section each: section keys
    would be entity ids, which translations can't target, so the frontend
    would show raw field names. The token is the location-source token of
    the GeoPulse account the tracker belongs to - GeoPulse builds one
    timeline per user and treats device_id as a label only, so each person
    needs their own account. There's deliberately no entry-wide default
    token: it would invite sending several people into one account. The same
    token may still be given to several trackers of one person.
    """

    def __init__(self, entity_ids: list[str]) -> None:
        self.entity_ids = entity_ids
        self.index = 0
        self.device_ids: dict[str, str] = {}
        self.tokens: dict[str, str] = {}
        self._accounts: set[tuple[str, str]] = set()

    @property
    def current(self) -> str:
        return self.entity_ids[self.index]

    @property
    def done(self) -> bool:
        return self.index >= len(self.entity_ids)

    def schema(self, stored_tokens: Mapping[str, str]) -> vol.Schema:
        # Optional only when blank can mean "keep the saved token".
        token_marker = vol.Optional if self.current in stored_tokens else vol.Required
        return vol.Schema(
            {
                vol.Required(FIELD_DEVICE_ID): TextSelector(),
                token_marker(FIELD_TOKEN): PASSWORD_SELECTOR,
            }
        )

    def submit(
        self, user_input: Mapping[str, Any], stored_tokens: Mapping[str, str]
    ) -> dict[str, str]:
        """Record the current tracker and advance; return errors instead if invalid."""
        entity_id = self.current
        device_id = str(user_input.get(FIELD_DEVICE_ID, "")).strip()
        token = (user_input.get(FIELD_TOKEN) or "").strip() or stored_tokens.get(entity_id)
        if not device_id:
            return {FIELD_DEVICE_ID: "device_id_required"}
        if not token:
            return {FIELD_TOKEN: "export_token_required"}
        if (token, device_id) in self._accounts:
            # Labels only need to be unique within one GeoPulse account.
            return {FIELD_DEVICE_ID: "duplicate_device_id"}
        self._accounts.add((token, device_id))
        self.device_ids[entity_id] = device_id
        self.tokens[entity_id] = token
        self.index += 1
        return {}

    def suggested(
        self, user_input: Mapping[str, Any] | None, current_ids: Mapping[str, str]
    ) -> dict[str, Any]:
        # Tokens are never echoed back into the form.
        if user_input is not None:
            return {FIELD_DEVICE_ID: user_input.get(FIELD_DEVICE_ID, "")}
        return {FIELD_DEVICE_ID: current_ids.get(self.current, self.current)}

    def placeholders(self, hass: HomeAssistant) -> dict[str, str]:
        state = hass.states.get(self.current)
        name = state.name if state is not None else self.current
        return {
            "entity": f"{name} ({self.current})" if name != self.current else name,
            "index": str(self.index + 1),
            "total": str(len(self.entity_ids)),
        }


async def _ha_user_options(
    hass: HomeAssistant, selected: list[str]
) -> list[SelectOptionDict]:
    """People who can log in to HA, for the timeline viewer list.

    There's no user selector for config flows, so this lists HA's own user
    accounts. Ids no longer matching a user stay selectable for removal.
    """
    users = [
        user
        for user in await hass.auth.async_get_users()
        if user.is_active and not user.system_generated
    ]
    options = [
        SelectOptionDict(value=user.id, label=user.name or user.id)
        for user in sorted(users, key=lambda u: (u.name or "").lower())
    ]
    known = {user.id for user in users}
    options += [
        SelectOptionDict(value=user_id, label=f"{user_id} (removed user)")
        for user_id in selected
        if user_id not in known
    ]
    return options


def _settings_schema(
    options: Mapping[str, Any], user_options: list[SelectOptionDict]
) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_POLL_INTERVAL,
                default=options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_POLL_INTERVAL_SECONDS,
                    max=MAX_POLL_INTERVAL_SECONDS,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="s",
                )
            ),
            vol.Required(
                CONF_RECORDER_EXCLUDE, default=options.get(CONF_RECORDER_EXCLUDE, True)
            ): BooleanSelector(),
            vol.Optional(
                CONF_TIMELINE_USERS, default=options.get(CONF_TIMELINE_USERS, [])
            ): SelectSelector(
                SelectSelectorConfig(
                    options=user_options, multiple=True, mode=SelectSelectorMode.LIST
                )
            ),
        }
    )


class GeoPulseConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: connection -> import -> export -> export device ids."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._options: dict[str, Any] = {
            CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL_SECONDS,
            CONF_RECORDER_EXCLUDE: True,
            CONF_EXPORT_ENTITIES: {},
        }
        self._account: _Account | None = None
        self._device_steps = _DeviceIdSteps([])

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> GeoPulseOptionsFlow:
        return GeoPulseOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = _normalize_base_url(user_input[CONF_BASE_URL])
            if base_url is None:
                errors[CONF_BASE_URL] = "invalid_url"
            else:
                token = user_input[CONF_READ_TOKEN].strip()
                account, errors = await _async_fetch_account(self.hass, base_url, token)
                if account is not None:
                    # Falls back to the URL for an account with no source
                    # configs or friends yet, where no userId is visible.
                    await self.async_set_unique_id(account.user_id or base_url)
                    self._abort_if_unique_id_configured()
                    self._data = {CONF_BASE_URL: base_url, CONF_READ_TOKEN: token}
                    self._account = account
                    return await self.async_step_import_selection()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_import_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._account is not None
        if user_input is not None:
            self._options.update(_import_options(user_input))
            return await self.async_step_export_selection()

        return self.async_show_form(
            step_id="import_selection",
            data_schema=_import_schema(self._account, self._options),
        )

    async def async_step_export_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            entities = list(user_input.get(CONF_EXPORT_ENTITIES, []))
            if not entities and not _has_import(self._options):
                errors["base"] = "nothing_selected"
            else:
                self._options[CONF_EXPORT_RETRY_QUEUE] = user_input[
                    CONF_EXPORT_RETRY_QUEUE
                ]
                self._device_steps = _DeviceIdSteps(entities)
                if entities:
                    return await self.async_step_export_device_ids()
                return self._async_create()

        return self.async_show_form(
            step_id="export_selection",
            data_schema=self.add_suggested_values_to_schema(
                _export_schema(self.hass, self._options), user_input or {}
            ),
            errors=errors,
        )

    async def async_step_export_device_ids(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        steps = self._device_steps
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = steps.submit(user_input, {})
            if not errors:
                if steps.done:
                    self._options[CONF_EXPORT_ENTITIES] = steps.device_ids
                    self._data[CONF_EXPORT_ENTITY_TOKENS] = steps.tokens
                    return self._async_create()
                user_input = None  # next tracker

        return self.async_show_form(
            step_id="export_device_ids",
            data_schema=self.add_suggested_values_to_schema(
                steps.schema({}), steps.suggested(user_input, {})
            ),
            errors=errors,
            description_placeholders=steps.placeholders(self.hass),
        )

    @callback
    def _async_create(self) -> ConfigFlowResult:
        return self.async_create_entry(
            title=URL(self._data[CONF_BASE_URL]).host or self._data[CONF_BASE_URL],
            data=self._data,
            options=self._options,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        base_url = entry.data[CONF_BASE_URL]
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_READ_TOKEN].strip()
            account, errors = await _async_fetch_account(self.hass, base_url, token)
            if account is not None:
                # An entry created before its account had any visible userId
                # is keyed by URL; adopt the real id rather than rejecting it.
                if account.user_id and entry.unique_id not in (account.user_id, base_url):
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    unique_id=account.user_id or entry.unique_id,
                    data_updates={CONF_READ_TOKEN: token},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"base_url": base_url},
        )


class GeoPulseOptionsFlow(OptionsFlowWithReload):
    """Change import/export selections and settings after setup."""

    def __init__(self) -> None:
        self._account: _Account | None = None
        self._device_steps = _DeviceIdSteps([])
        self._pending_retry: bool = True

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["import_selection", "export_selection", "settings"],
        )

    async def async_step_import_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = dict(self.config_entry.options)
        errors: dict[str, str] = {}
        if user_input is not None:
            options.update(_import_options(user_input))
            if not _has_import(options) and not options.get(CONF_EXPORT_ENTITIES):
                errors["base"] = "nothing_selected"
            else:
                return self.async_create_entry(data=options)

        if self._account is None:
            data = self.config_entry.data
            account, fetch_errors = await _async_fetch_account(
                self.hass, data[CONF_BASE_URL], data[CONF_READ_TOKEN]
            )
            if account is None:
                return self.async_abort(reason=fetch_errors["base"])
            self._account = account

        schema = _import_schema(self._account, options)
        return self.async_show_form(
            step_id="import_selection",
            data_schema=self.add_suggested_values_to_schema(schema, options),
            errors=errors,
        )

    async def async_step_export_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self.config_entry.options
        errors: dict[str, str] = {}
        if user_input is not None:
            entities = list(user_input.get(CONF_EXPORT_ENTITIES, []))
            if not entities and not _has_import(options):
                errors["base"] = "nothing_selected"
            else:
                self._device_steps = _DeviceIdSteps(entities)
                self._pending_retry = user_input[CONF_EXPORT_RETRY_QUEUE]
                if entities:
                    return await self.async_step_export_device_ids()
                return self._async_save_export({}, {})

        suggested = {CONF_EXPORT_ENTITIES: list(options.get(CONF_EXPORT_ENTITIES, {}))}
        if user_input is not None:
            suggested = user_input
        return self.async_show_form(
            step_id="export_selection",
            data_schema=self.add_suggested_values_to_schema(
                _export_schema(self.hass, options), suggested
            ),
            errors=errors,
        )

    async def async_step_export_device_ids(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        steps = self._device_steps
        data = self.config_entry.data
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = steps.submit(user_input, data.get(CONF_EXPORT_ENTITY_TOKENS, {}))
            if not errors:
                if steps.done:
                    return self._async_save_export(steps.device_ids, steps.tokens)
                user_input = None  # next tracker

        return self.async_show_form(
            step_id="export_device_ids",
            data_schema=self.add_suggested_values_to_schema(
                steps.schema(data.get(CONF_EXPORT_ENTITY_TOKENS, {})),
                steps.suggested(user_input, self.config_entry.options.get(CONF_EXPORT_ENTITIES, {})),
            ),
            errors=errors,
            description_placeholders=steps.placeholders(self.hass),
        )

    @callback
    def _async_save_export(
        self, device_ids: dict[str, str], tokens: dict[str, str]
    ) -> ConfigFlowResult:
        entry = self.config_entry
        options = {
            **entry.options,
            CONF_EXPORT_ENTITIES: device_ids,
            CONF_EXPORT_RETRY_QUEUE: self._pending_retry,
        }
        # Tokens for deselected trackers are dropped with them.
        data = {**entry.data, CONF_EXPORT_ENTITY_TOKENS: tokens}
        if data != dict(entry.data):
            self.hass.config_entries.async_update_entry(entry, data=data)
            # OptionsFlowWithReload only reloads when options change; a
            # token-only change still needs the export side to pick it up.
            if options == dict(entry.options):
                self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_create_entry(data=options)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **self.config_entry.options,
                    CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                    CONF_RECORDER_EXCLUDE: user_input[CONF_RECORDER_EXCLUDE],
                    CONF_TIMELINE_USERS: list(user_input.get(CONF_TIMELINE_USERS, [])),
                }
            )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="settings",
            data_schema=_settings_schema(
                options,
                await _ha_user_options(self.hass, options.get(CONF_TIMELINE_USERS, [])),
            ),
        )
