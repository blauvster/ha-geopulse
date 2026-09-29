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
from homeassistant.data_entry_flow import section
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
    CONF_EXPORT_TOKEN,
    CONF_IMPORT_AGGREGATE_ACCOUNT,
    CONF_IMPORT_FRIEND_IDS,
    CONF_IMPORT_SOURCE_TYPES,
    CONF_POLL_INTERVAL,
    CONF_READ_TOKEN,
    CONF_RECORDER_EXCLUDE,
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
            vol.Optional(CONF_EXPORT_TOKEN): PASSWORD_SELECTOR,
            vol.Required(
                CONF_EXPORT_RETRY_QUEUE,
                default=options.get(CONF_EXPORT_RETRY_QUEUE, True),
            ): BooleanSelector(),
        }
    )


FIELD_DEVICE_ID = "device_id"
FIELD_TOKEN = "token"


def _device_ids_schema(entity_ids: list[str]) -> vol.Schema:
    """One section per exported tracker: device_id plus an optional token.

    The token is the location-source token of the GeoPulse account this
    tracker belongs to. GeoPulse builds one timeline per user and treats
    device_id as a label only, so each person needs their own account;
    blank falls back to the entry-level export token.
    """
    return vol.Schema(
        {
            vol.Required(entity_id): section(
                vol.Schema(
                    {
                        vol.Required(FIELD_DEVICE_ID): TextSelector(),
                        vol.Optional(FIELD_TOKEN): PASSWORD_SELECTOR,
                    }
                ),
                {"collapsed": False},
            )
            for entity_id in entity_ids
        }
    )


def _device_ids_suggested(
    entity_ids: list[str], device_ids: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    # Tokens are never echoed back into the form.
    return {eid: {FIELD_DEVICE_ID: device_ids.get(eid, eid)} for eid in entity_ids}


def _validate_device_ids(
    entity_ids: list[str],
    user_input: Mapping[str, Any],
    default_token: str | None,
    stored_tokens: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str], dict[str, str], str]:
    """Return ({entity_id: device_id}, {entity_id: token}, errors, entities).

    A blank token keeps the tracker's stored token, else uses the default.
    Errors are form-level with the affected trackers as a placeholder: every
    section has the same field names, so a field-keyed error would be
    ambiguous.
    """
    device_ids: dict[str, str] = {}
    tokens: dict[str, str] = {}
    problems: dict[str, list[str]] = {}
    seen: set[tuple[str, str]] = set()
    for entity_id in entity_ids:
        values = user_input.get(entity_id) or {}
        device_id = str(values.get(FIELD_DEVICE_ID, "")).strip()
        token = (values.get(FIELD_TOKEN) or "").strip() or stored_tokens.get(entity_id)
        device_ids[entity_id] = device_id
        if token:
            tokens[entity_id] = token
        account = token or default_token
        if not device_id:
            problems.setdefault("device_id_required", []).append(entity_id)
        elif not account:
            problems.setdefault("export_token_required", []).append(entity_id)
        elif (account, device_id) in seen:
            # Labels only need to be unique within one GeoPulse account.
            problems.setdefault("duplicate_device_id", []).append(entity_id)
        seen.add((account or "", device_id))
    for code in ("device_id_required", "export_token_required", "duplicate_device_id"):
        if code in problems:
            return device_ids, tokens, {"base": code}, ", ".join(problems[code])
    return device_ids, tokens, {}, ""


def _settings_schema(options: Mapping[str, Any]) -> vol.Schema:
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
        self._pending_export: list[str] = []

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
            token = (user_input.get(CONF_EXPORT_TOKEN) or "").strip()
            # Missing tokens are checked per tracker in the next step.
            if not entities and not _has_import(self._options):
                errors["base"] = "nothing_selected"
            else:
                if token:
                    self._data[CONF_EXPORT_TOKEN] = token
                self._options[CONF_EXPORT_RETRY_QUEUE] = user_input[
                    CONF_EXPORT_RETRY_QUEUE
                ]
                self._pending_export = entities
                if entities:
                    return await self.async_step_export_device_ids()
                return self._async_create()

        suggested = {k: v for k, v in (user_input or {}).items() if k != CONF_EXPORT_TOKEN}
        return self.async_show_form(
            step_id="export_selection",
            data_schema=self.add_suggested_values_to_schema(
                _export_schema(self.hass, self._options), suggested
            ),
            errors=errors,
        )

    async def async_step_export_device_ids(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entities = ""
        if user_input is not None:
            device_ids, tokens, errors, entities = _validate_device_ids(
                self._pending_export, user_input, self._data.get(CONF_EXPORT_TOKEN), {}
            )
            if not errors:
                self._options[CONF_EXPORT_ENTITIES] = device_ids
                if tokens:
                    self._data[CONF_EXPORT_ENTITY_TOKENS] = tokens
                return self._async_create()

        return self.async_show_form(
            step_id="export_device_ids",
            data_schema=self.add_suggested_values_to_schema(
                _device_ids_schema(self._pending_export),
                user_input or _device_ids_suggested(self._pending_export, {}),
            ),
            errors=errors,
            description_placeholders={"entities": entities},
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
        self._pending_export: list[str] = []
        self._pending_token: str | None = None
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
            # Blank keeps the stored token; the field never echoes it back.
            token = (user_input.get(CONF_EXPORT_TOKEN) or "").strip() or None
            # Missing tokens are checked per tracker in the next step.
            if not entities and not _has_import(options):
                errors["base"] = "nothing_selected"
            else:
                self._pending_export = entities
                self._pending_token = token
                self._pending_retry = user_input[CONF_EXPORT_RETRY_QUEUE]
                if entities:
                    return await self.async_step_export_device_ids()
                return self._async_save_export({}, {})

        suggested = {CONF_EXPORT_ENTITIES: list(options.get(CONF_EXPORT_ENTITIES, {}))}
        if user_input is not None:
            suggested = {k: v for k, v in user_input.items() if k != CONF_EXPORT_TOKEN}
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
        errors: dict[str, str] = {}
        entities = ""
        data = self.config_entry.data
        if user_input is not None:
            device_ids, tokens, errors, entities = _validate_device_ids(
                self._pending_export,
                user_input,
                self._pending_token or data.get(CONF_EXPORT_TOKEN),
                data.get(CONF_EXPORT_ENTITY_TOKENS, {}),
            )
            if not errors:
                return self._async_save_export(device_ids, tokens)

        current = self.config_entry.options.get(CONF_EXPORT_ENTITIES, {})
        return self.async_show_form(
            step_id="export_device_ids",
            data_schema=self.add_suggested_values_to_schema(
                _device_ids_schema(self._pending_export),
                user_input or _device_ids_suggested(self._pending_export, current),
            ),
            errors=errors,
            description_placeholders={"entities": entities},
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
        if self._pending_token:
            data[CONF_EXPORT_TOKEN] = self._pending_token
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
                }
            )

        return self.async_show_form(
            step_id="settings", data_schema=_settings_schema(self.config_entry.options)
        )
