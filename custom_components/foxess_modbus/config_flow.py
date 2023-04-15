"""Adds config flow for foxess_modbus."""
import copy
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Mapping

import voluptuous as vol
from custom_components.foxess_modbus import ModbusClient
from homeassistant import config_entries
from homeassistant.components.energy import data
from homeassistant.components.energy.data import BatterySourceType
from homeassistant.components.energy.data import EnergyPreferencesUpdate
from homeassistant.components.energy.data import FlowFromGridSourceType
from homeassistant.components.energy.data import FlowToGridSourceType
from homeassistant.components.energy.data import GridSourceType
from homeassistant.components.energy.data import SolarSourceType
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import selector
from pymodbus.exceptions import ConnectionException

from .common.exceptions import UnsupportedInverterException
from .const import ADAPTER_ID
from .const import CONFIG_SAVE_TIME
from .const import DOMAIN
from .const import ENERGY_DASHBOARD
from .const import FRIENDLY_NAME
from .const import HOST
from .const import INVERTER_BASE
from .const import INVERTER_CONN
from .const import INVERTER_MODEL
from .const import INVERTERS
from .const import INVETER_ADAPTER_NEEDS_MANUAL_INPUT
from .const import MAX_READ
from .const import MODBUS_SLAVE
from .const import MODBUS_TYPE
from .const import POLL_RATE
from .const import SERIAL
from .const import TCP
from .const import UDP
from .inverter_adapters import ADAPTERS
from .inverter_adapters import InverterAdapter
from .inverter_adapters import InverterAdapterType
from .inverter_connection_types import InverterConnectionType
from .modbus_controller import ModbusController

_TITLE = "FoxESS - Modbus"

_DEFAULT_PORT = 502
_DEFAULT_SLAVE = 247

_LOGGER = logging.getLogger(__name__)


@dataclass
class InverterData:
    """Holds data gathered on an inverter as the user went through the flow"""

    adapter_type: InverterAdapterType | None = None
    adapter: InverterAdapter | None = None
    inverter_base_model: str | None = None
    inverter_model: str | None = None
    modbus_slave: int | None = None
    inverter_protocol: str | None = None  # TCP, UDP, SERIAL
    host: str | None = None  # host:port or /dev/serial
    friendly_name: str | None = None


class FlowHandlerMixin:
    async def _with_default_form(
        self,
        body: Callable[[dict[str, Any]], Awaitable[FlowResult | None]],
        user_input: dict[str, Any] | None,
        step_id: str,
        data_schema: vol.Schema,
        description_placeholders: Mapping[str, str | None] | None = None,
    ):
        """
        If user_input is not None, call body() and return the result.
        If body throws a ValidationFailedException, or returns None, or user_input is None,
        show the default form specified by step_id and data_schema
        """

        errors: dict[str, str] | None = None
        if user_input is not None:
            try:
                result = await body(user_input)
                if result is not None:
                    return result
            except ValidationFailedException as ex:
                errors = ex.errors

        schema_with_input = self.add_suggested_values_to_schema(data_schema, user_input)
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema_with_input,
            errors=errors,
            description_placeholders=description_placeholders,
        )

    def _create_label_for_inverter(self, inverter: dict[str, Any]) -> str:
        result = ""
        if inverter[FRIENDLY_NAME]:
            result = f"{inverter[FRIENDLY_NAME]} - "
        result += f"{inverter[HOST]} ({inverter[MODBUS_SLAVE]})"
        return result


class ModbusFlowHandler(FlowHandlerMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for foxess_modbus."""

    VERSION = 2
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        """Initialize."""
        self._inverter_data = InverterData()
        self._all_inverters: list[InverterData] = []

        self._adapter_type_to_method = {
            InverterAdapterType.DIRECT: self.async_step_tcp_adapter,
            InverterAdapterType.SERIAL: self.async_step_serial_adapter,
            InverterAdapterType.NETWORK: self.async_step_tcp_adapter,
        }

        self._config_entry_due_to_migration: config_entries.ConfigEntry | None = None
        self._remaining_inverters_due_to_migration: list[str] | None = None

    async def async_step_user(self, _user_input: dict[str, Any] = None):
        """Handle a flow initialized by the user."""

        return await self.async_step_select_adapter_type()

    async def async_step_reauth(self, _user_input: dict[str, Any] = None) -> FlowResult:
        """
        We use re-authentication as a hack to get users migrating from version 1 who are using
        a TCP or SERIAL adapter to select their inverter type
        """

        self._config_entry_due_to_migration = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._remaining_inverters_due_to_migration = [
            k
            for k, x in self._config_entry_due_to_migration.data[INVERTERS].items()
            if INVETER_ADAPTER_NEEDS_MANUAL_INPUT in x
        ]
        assert len(self._remaining_inverters_due_to_migration) > 0

        return await self.async_step_select_adapter_model_due_to_migration()

    async def async_step_select_adapter_type(
        self, user_input: dict[str, Any] = None
    ) -> FlowResult:
        """Let the user select their adapter type"""

        async def body(user_input):
            adapter_type = InverterAdapterType(user_input["adapter_type"])
            self._inverter_data.adapter_type = adapter_type

            adapters = [x for x in ADAPTERS.values() if x.type == adapter_type]

            assert len(adapters) > 0
            if len(adapters) == 1:
                self._inverter_data.adapter = adapters[0]
                return await self._adapter_type_to_method[adapter_type]()

            return await self.async_step_select_adapter_model()

        schema = vol.Schema(
            {
                vol.Required("adapter_type"): selector(
                    {
                        "select": {
                            "options": [x.value for x in InverterAdapterType],
                            "translation_key": "inverter_adapter_types",
                        }
                    }
                )
            }
        )

        return await self._with_default_form(
            body, user_input, "select_adapter_type", schema
        )

    async def async_step_select_adapter_model(
        self, user_input: dict[str, Any] = None
    ) -> FlowResult:
        """Let the user select their adapter model"""

        async def complete_callback(adapter: InverterAdapter):
            self._inverter_data.adapter = adapter
            return await self._adapter_type_to_method[
                self._inverter_data.adapter_type
            ]()

        return await self._select_adapter_model_helper(
            "select_adapter_model",
            user_input=user_input,
            adapter_type=self._inverter_data.adapter_type,
            complete_callback=complete_callback,
        )

    async def async_step_select_adapter_model_due_to_migration(
        self, user_input: dict[str, Any] = None
    ) -> FlowResult:
        """
        Called from async_step_reauth when we've done a migration, and need the user to select their adapter.
        Loops through all inverters in _remaining_inverters_due_to_migration and prompts for the adapter for each.
        """

        async def step(user_input):
            inverter_id = self._remaining_inverters_due_to_migration[0]
            inverter = self._config_entry_due_to_migration.data[INVERTERS][inverter_id]
            protocol = inverter[MODBUS_TYPE]  # TCP, UDP, SERIAL
            if protocol in [TCP, UDP]:
                assert (
                    inverter[INVERTER_CONN] == "AUX"
                )  # We don't expect to be called for LAN
                adapter_type = InverterAdapterType.NETWORK
            elif protocol == SERIAL:
                adapter_type = InverterAdapterType.SERIAL
            else:
                assert False

            description_placeholders = {
                "inverter": self._create_label_for_inverter(inverter),
            }

            return await self._select_adapter_model_helper(
                "select_adapter_model_due_to_migration",
                user_input=user_input,
                adapter_type=adapter_type,
                complete_callback=complete_callback,
                description_placeholders=description_placeholders,
            )

        async def complete_callback(adapter: InverterAdapter):
            inverter_id = self._remaining_inverters_due_to_migration.pop(0)
            inverter = self._config_entry_due_to_migration.data[INVERTERS][inverter_id]
            inverter[ADAPTER_ID] = adapter.adapter_id
            del inverter[INVETER_ADAPTER_NEEDS_MANUAL_INPUT]

            if len(self._remaining_inverters_due_to_migration) > 0:
                return await step(None)

            self.hass.config_entries.async_update_entry(
                self._config_entry_due_to_migration,
                data=self._config_entry_due_to_migration.data,
            )
            # https://github.com/home-assistant/core/blob/208a44e437e836fdc36292203fd4348f9fa7c331/homeassistant/components/esphome/config_flow.py#L245
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(
                    self._config_entry_due_to_migration.entry_id
                )
            )
            return self.async_abort(reason="reconfigure_successful")

        return await step(user_input)

    async def _select_adapter_model_helper(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        adapter_type: InverterAdapterType,
        complete_callback: Callable[[InverterAdapter], Awaitable[FlowResult]],
        description_placeholders: Mapping[str, str | None] | None = None,
    ) -> FlowResult:
        async def body(user_input):
            return await complete_callback(ADAPTERS[user_input["adapter_model"]])

        adapters = [x for x in ADAPTERS.values() if x.type == adapter_type]

        schema = vol.Schema(
            {
                vol.Required("adapter_model"): selector(
                    {
                        "select": {
                            "options": [x.adapter_id for x in adapters],
                            "translation_key": "inverter_adapter_models",
                        }
                    }
                )
            }
        )

        return await self._with_default_form(
            body,
            user_input,
            step_id,
            schema,
            description_placeholders=description_placeholders,
        )

    async def async_step_tcp_adapter(
        self, user_input: dict[str, Any] = None
    ) -> FlowResult:
        """Let the user enter connection details for their TCP/UDP adapter"""

        adapter = self._inverter_data.adapter
        assert adapter is not None

        async def body(user_input):
            protocol = user_input.get(
                "protocol",
                user_input.get(
                    "protocol_with_recommendation", adapter.network_protocols[0]
                ),
            )
            host = user_input.get("adapter_host", user_input.get("lan_connection_host"))
            assert host is not None
            port = user_input.get("adapter_port", _DEFAULT_PORT)
            host_and_port = f"{host}:{port}"
            slave = user_input.get("modbus_slave", _DEFAULT_SLAVE)
            await self._autodetect_modbus_and_save_to_inverter_data(
                protocol, adapter.connection_type, host_and_port, slave
            )
            return await self.async_step_friendly_name()

        schema_parts = {}
        description_placeholders = {"setup_link": adapter.setup_link}

        if len(adapter.network_protocols) > 1:
            # Prompt for TCP vs UDP if that's relevant
            # If we provide a recommendation, show that
            key = (
                "protocol_with_recommendation"
                if adapter.recommended_protocol is not None
                else "protocol"
            )
            schema_parts[vol.Required(key)] = selector(
                {"select": {"options": adapter.network_protocols}}
            )
            description_placeholders[
                "recommended_protocol"
            ] = adapter.recommended_protocol

        if adapter.connection_type.key == "AUX":
            schema_parts[vol.Required("adapter_host")] = cv.string
            schema_parts[
                vol.Required(
                    "adapter_port",
                    default=_DEFAULT_PORT,
                )
            ] = int
        else:
            # If it's a direct connection we know what the port is
            schema_parts[vol.Required("lan_connection_host")] = cv.string

        schema_parts[
            vol.Required(
                "modbus_slave",
                default=_DEFAULT_SLAVE,
            )
        ] = int

        schema = vol.Schema(schema_parts)

        return await self._with_default_form(
            body, user_input, "tcp_adapter", schema, description_placeholders
        )

    async def async_step_serial_adapter(
        self, user_input: dict[str, Any] = None
    ) -> FlowResult:
        """Let the user enter connection details for their serial adapter"""

        adapter = self._inverter_data.adapter
        assert adapter is not None

        async def body(user_input):
            device = user_input["serial_device"]
            slave = user_input.get("modbus_slave", _DEFAULT_SLAVE)
            # TODO: Check for duplicate host/port/slave/protocol combinations
            await self._autodetect_modbus_and_save_to_inverter_data(
                SERIAL, adapter.connection_type, device, slave
            )
            return await self.async_step_friendly_name()

        # TODO: Look at self._data.get(MODBUS_SERIAL_HOST etc)
        schema = vol.Schema(
            {
                vol.Required(
                    "serial_device",
                    default="/dev/ttyUSB0",
                ): cv.string,
                vol.Required("modbus_slave", default=_DEFAULT_SLAVE): int,
            }
        )
        description_placeholders = {"setup_link": adapter.setup_link}

        return await self._with_default_form(
            body, user_input, "serial_adapter", schema, description_placeholders
        )

    async def async_step_friendly_name(
        self, user_input: dict[str, Any] = None
    ) -> FlowResult:
        """Let the user enter a friendly name for their inverter"""

        async def body(user_input):
            friendly_name = user_input.get("friendly_name", "")
            if friendly_name and not re.fullmatch(r"\w+", friendly_name):
                raise ValidationFailedException(
                    {"friendly_name": "invalid_friendly_name"}
                )
            if any(x for x in self._all_inverters if x.friendly_name == friendly_name):
                raise ValidationFailedException(
                    {"friendly_name": "duplicate_friendly_name"}
                )

            self._inverter_data.friendly_name = friendly_name
            self._all_inverters.append(self._inverter_data)
            self._inverter_data = InverterData()
            return await self.async_step_add_another_inverter()

        schema = vol.Schema({vol.Optional("friendly_name"): cv.string})

        return await self._with_default_form(body, user_input, "friendly_name", schema)

    async def async_step_add_another_inverter(
        self, _user_input: dict[str, Any] = None
    ) -> FlowResult:
        """Let the user choose whether to add another inverter"""

        options = ["select_adapter", "energy"]
        return self.async_show_menu(
            step_id="add_another_inverter", menu_options=options
        )

    async def async_step_energy(self, user_input: dict[str, Any] = None):
        """Let the user choose whether to set up the energy dashboard"""

        async def body(user_input):
            if user_input[ENERGY_DASHBOARD]:
                await self._setup_energy_dashboard()
            return self.async_create_entry(title=_TITLE, data=self._create_entry_data())

        schema = vol.Schema(
            {
                vol.Required(ENERGY_DASHBOARD, default=False): bool,
            }
        )

        return await self._with_default_form(body, user_input, "energy", schema)

    def _create_entry_data(self) -> dict[str, Any]:
        """Create the config entry for all inverters in self._all_inverters"""

        entry = {INVERTERS: {}}
        for inverter in self._all_inverters:
            inverter = {
                INVERTER_BASE: inverter.inverter_base_model,
                INVERTER_MODEL: inverter.inverter_model,
                INVERTER_CONN: inverter.adapter.connection_type.key,
                MODBUS_SLAVE: inverter.modbus_slave,
                FRIENDLY_NAME: inverter.friendly_name,
                MODBUS_TYPE: inverter.inverter_protocol,
                HOST: inverter.host,
                ADAPTER_ID: inverter.adapter.adapter_id,
            }
            entry[INVERTERS][str(uuid.uuid4())] = inverter
        entry[CONFIG_SAVE_TIME] = datetime.now()
        return entry

    async def _autodetect_modbus_and_save_to_inverter_data(
        self, protocol: str, conn_type: InverterConnectionType, host: str, slave: int
    ) -> tuple[str, str]:
        """Check that connection details are unique, then connect to the inverter and add its details to self._inverter_data"""
        if any(
            x
            for x in self._all_inverters
            if x.inverter_protocol == protocol
            and x.host == host
            and x.modbus_slave == slave
        ):
            raise ValidationFailedException({"base": "duplicate_connection_details"})

        try:
            params = {MODBUS_TYPE: protocol}
            if protocol in [TCP, UDP]:
                params.update(
                    {"host": host.split(":")[0], "port": int(host.split(":")[1])}
                )
            elif protocol == SERIAL:
                params.update({"port": host, "baudrate": 9600})
            else:
                assert False
            client = ModbusClient(self.hass, params)
            base_model, full_model = await ModbusController.autodetect(
                client, conn_type, slave
            )

            self._inverter_data.inverter_base_model = base_model
            self._inverter_data.inverter_model = full_model
            self._inverter_data.inverter_protocol = protocol
            self._inverter_data.modbus_slave = slave
            self._inverter_data.host = host
        except UnsupportedInverterException as ex:
            _LOGGER.warning(f"{ex}")
            raise ValidationFailedException({"base": "modbus_model_not_supported"})
        except ConnectionException as ex:
            _LOGGER.warning(f"{ex}")
            raise ValidationFailedException({"base": "modbus_error"})

    async def _setup_energy_dashboard(self):
        """Setup Energy Dashboard"""

        manager = await data.async_get_manager(self.hass)

        friendly_names = [x.friendly_name for x in self._all_inverters]

        def _prefix_name(name):
            if name != "":
                return f"sensor.{name}_"
            else:
                return "sensor."

        energy_prefs = EnergyPreferencesUpdate(energy_sources=[])
        for name in friendly_names:
            name_prefix = _prefix_name(name)
            energy_prefs["energy_sources"].extend(
                [
                    SolarSourceType(
                        type="solar", stat_energy_from=f"{name_prefix}pv1_energy_total"
                    ),
                    SolarSourceType(
                        type="solar", stat_energy_from=f"{name_prefix}pv2_energy_total"
                    ),
                    BatterySourceType(
                        type="battery",
                        stat_energy_to=f"{name_prefix}battery_charge_total",
                        stat_energy_from=f"{name_prefix}battery_discharge_total",
                    ),
                ]
            )

        grid_source = GridSourceType(
            type="grid", flow_from=[], flow_to=[], cost_adjustment_day=0
        )
        for name in friendly_names:
            name_prefix = _prefix_name(name)
            grid_source["flow_from"].append(
                FlowFromGridSourceType(
                    stat_energy_from=f"{name_prefix}grid_consumption_energy_total"
                )
            )
            grid_source["flow_to"].append(
                FlowToGridSourceType(
                    stat_energy_to=f"{name_prefix}feed_in_energy_total"
                )
            )
        energy_prefs["energy_sources"].append(grid_source)

        await manager.async_update(energy_prefs)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return ModbusOptionsHandler(config_entry)


class ModbusOptionsHandler(FlowHandlerMixin, config_entries.OptionsFlow):
    """Options flow handler"""

    def __init__(self, config: config_entries.ConfigEntry) -> None:
        self._config = config
        self._selected_inverter_id: str | None = None

    async def async_step_init(self, _user_input=None):
        """Start the config flow"""

        if len(self._config.data[INVERTERS]) == 1:
            self._selected_inverter_id = next(iter(self._config.data[INVERTERS]))
            return await self.async_step_inverter_options()

        return await self.async_step_select_inverter()

    async def async_step_select_inverter(
        self, user_input: dict[str, Any] = None
    ) -> FlowResult:
        """Let the user select their inverter, if they have multiple inverters"""

        async def body(user_input):
            self._selected_inverter_id = user_input["inverter"]
            return await self.async_step_inverter_options()

        schema = vol.Schema(
            {
                vol.Required("inverter"): selector(
                    {
                        "select": {
                            "options": [
                                {
                                    "label": self._create_label_for_inverter(inverter),
                                    "value": inverter_id,
                                }
                                for inverter_id, inverter in self._config.data[
                                    INVERTERS
                                ].items()
                            ]
                        }
                    }
                )
            }
        )

        return await self._with_default_form(
            body, user_input, "select_inverter", schema
        )

    async def async_step_inverter_options(
        self, user_input: dict[str, Any] = None
    ) -> FlowResult:
        """Let the user set the inverter's settings"""

        async def body(user_input):
            inverter_options = {}
            poll_rate = user_input.get("poll_rate")
            if poll_rate is not None:
                inverter_options[POLL_RATE] = poll_rate
            max_read = user_input.get("max_read")
            if max_read is not None:
                inverter_options[MAX_READ] = max_read

            # We must not mutate any part of self._config.options, otherwise HA thinks we haven't changed the options
            options = copy.deepcopy(dict(self._config.options))
            options.setdefault(INVERTERS, {})[
                self._selected_inverter_id
            ] = inverter_options

            return self.async_create_entry(title=_TITLE, data=options)

        existing = self._config.options.get(INVERTERS, {}).get(
            self._selected_inverter_id, {}
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    "poll_rate",
                    description={"suggested_value": existing.get(POLL_RATE)},
                ): vol.Any(None, int),
                vol.Optional(
                    "max_read", description={"suggested_value": existing.get(MAX_READ)}
                ): vol.Any(None, int),
            }
        )
        adapter = ADAPTERS[
            self._config.data[INVERTERS][self._selected_inverter_id][ADAPTER_ID]
        ]
        description_placeholders = {
            "default_poll_rate": adapter.poll_rate,
            "default_max_read": adapter.max_read,
        }

        return await self._with_default_form(
            body,
            user_input,
            "inverter_options",
            schema,
            description_placeholders=description_placeholders,
        )


class ValidationFailedException(Exception):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
