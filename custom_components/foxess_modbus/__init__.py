"""
Custom integration to integrate FoxESS Modbus with Home Assistant.

For more details about this integration, please refer to
https://github.com/nathanmarlor/foxess_modbus
"""
import asyncio
import logging
import uuid

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Config
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.typing import UNDEFINED

from .const import ADAPTER_ID
from .const import CONFIG_SAVE_TIME
from .const import DOMAIN
from .const import FRIENDLY_NAME
from .const import HOST
from .const import INVERTER_CONN
from .const import INVERTERS
from .const import INVETER_ADAPTER_NEEDS_MANUAL_INPUT
from .const import MAX_READ
from .const import MODBUS_SLAVE
from .const import MODBUS_TYPE
from .const import PLATFORMS
from .const import POLL_RATE
from .const import SERIAL
from .const import STARTUP_MESSAGE
from .const import TCP
from .const import UDP
from .inverter_adapters import ADAPTERS
from .inverter_profiles import inverter_connection_type_profile_from_config
from .modbus_client import ModbusClient
from .modbus_controller import ModbusController
from .services import update_charge_period_service
from .services import write_registers_service

_LOGGER: logging.Logger = logging.getLogger(__package__)


async def async_setup(hass: HomeAssistant, config: Config):
    """Set up this integration using YAML is not supported."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""

    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    for platform in PLATFORMS:
        if entry.options.get(platform, True):
            hass.async_add_job(
                hass.config_entries.async_forward_entry_setup(entry, platform)
            )

    def create_controller(hass, client, inverter):
        controller = ModbusController(
            hass,
            client,
            inverter_connection_type_profile_from_config(inverter),
            inverter[MODBUS_SLAVE],
            inverter[POLL_RATE],
            inverter[MAX_READ],
        )
        inverter_controllers.append((inverter, controller))

    inverter_controllers = []

    # {(modbus_type, host): client}
    clients: dict[tuple[str, str], ModbusClient] = {}
    for inverter_id, inverter in entry.data[INVERTERS].items():
        # Merge in adapter options. This lets us tweak the adapters later, and those settings are reflected back to users
        # Handle an adapter in need of manual input to complete migration
        inverter.update(ADAPTERS[inverter[ADAPTER_ID]].inverter_config())

        # Merge in the options, if any. These can override the adapter options set above
        options = entry.options.get(INVERTERS, {}).get(inverter_id)
        if options:
            inverter.update(options)

        client_key = (inverter[MODBUS_TYPE], inverter[HOST])
        client = clients.get(client_key)
        if client is None:
            params = {MODBUS_TYPE: inverter[MODBUS_TYPE]}
            if inverter[MODBUS_TYPE] in [TCP, UDP]:
                host_parts = inverter[HOST].split(":")
                params.update({"host": host_parts[0], "port": int(host_parts[1])})
            else:
                params.update({"port": inverter[HOST], "baudrate": 9600})
            client = ModbusClient(hass, params)
            clients[client_key] = client
        create_controller(hass, client, inverter)

    write_registers_service.register(hass, inverter_controllers)
    update_charge_period_service.register(hass, inverter_controllers)

    hass.data[DOMAIN][entry.entry_id] = {
        INVERTERS: inverter_controllers,
    }

    hass.data[DOMAIN][entry.entry_id]["unload"] = entry.add_update_listener(
        async_reload_entry
    )

    # Do this last, so sensors etc can continue to function in the meantime
    for inverter in entry.data[INVERTERS].values():
        if INVETER_ADAPTER_NEEDS_MANUAL_INPUT in inverter:
            raise ConfigEntryAuthFailed(
                "Configuration needs manual input. Please click 'RECONFIGURE'"
            )

    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        new_data = {
            INVERTERS: {},
            CONFIG_SAVE_TIME: config_entry.data[CONFIG_SAVE_TIME],
        }
        if config_entry.options:
            inverter_options = {
                POLL_RATE: config_entry.options[POLL_RATE],
                MAX_READ: config_entry.options[MAX_READ],
            }
            options = {INVERTERS: {}}
        else:
            inverter_options = {}
            options = UNDEFINED

        for modbus_type, modbus_type_inverters in config_entry.data.items():
            if modbus_type in [TCP, UDP, SERIAL]:
                for host, host_inverters in modbus_type_inverters.items():
                    for friendly_name, inverter in host_inverters.items():
                        if friendly_name == "null":
                            friendly_name = ""
                        inverter[MODBUS_TYPE] = modbus_type
                        inverter[HOST] = host
                        inverter[FRIENDLY_NAME] = friendly_name
                        # We can infer what the adapter type is, ish
                        if modbus_type == TCP:
                            if inverter[INVERTER_CONN] == "LAN":
                                adapter = ADAPTERS["direct"]
                            else:
                                # Go for the worst device, which is the W610
                                adapter = ADAPTERS["usr_w610"]
                        elif modbus_type == SERIAL:
                            adapter = ADAPTERS["serial_other"]
                        inverter[ADAPTER_ID] = adapter.adapter_id

                        # If we need manual input to find the correct adapter type, prompt for this
                        if modbus_type != TCP or inverter[INVERTER_CONN] != "LAN":
                            inverter[INVETER_ADAPTER_NEEDS_MANUAL_INPUT] = True
                        inverter_id = str(uuid.uuid4())
                        new_data[INVERTERS][inverter_id] = inverter
                        if inverter_options:
                            options[INVERTERS][inverter_id] = inverter_options

        config_entry.version = 2
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, options=options
        )

    _LOGGER.info("Migration to version %s successful", config_entry.version)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
            ]
        )
    )

    if unloaded:
        controllers = hass.data[DOMAIN][entry.entry_id][INVERTERS]
        for _, controller in controllers:
            controller.unload()

        hass.data[DOMAIN][entry.entry_id]["unload"]()
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def options_update_listener(hass: HomeAssistant, config_entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)
