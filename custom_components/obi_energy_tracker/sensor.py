"""Sensor platform for Obi EnergyTracker."""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ObiEnergyTrackerConfigEntry
from .api import latest_value
from .const import DOMAIN
from .coordinator import ObiEnergyTrackerCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ObiEnergyTrackerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    coordinator = config_entry.runtime_data

    sensors = [
        ObiMeterReadingSensor(coordinator),
        ObiMeterExportSensor(coordinator),
        ObiPowerSensor(coordinator),
        ObiExportPowerSensor(coordinator),
        ObiDailyEnergySensor(coordinator),
        ObiDailyExportEnergySensor(coordinator),
        ObiBatteryLevelSensor(coordinator),
        ObiIsOnlineSensor(coordinator),
        ObiConnectionStrengthSensor(coordinator),
        ObiLastRecordReceivedAtSensor(coordinator),
    ]

    async_add_entities(sensors)


class ObiEnergySensorBase(CoordinatorEntity[ObiEnergyTrackerCoordinator], SensorEntity):
    """Base class for Obi EnergyTracker sensors."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_device_info = {
            "identifiers": {(DOMAIN, "obi_energy_tracker")},
            "name": "Obi EnergyTracker",
            "manufacturer": "Obi",
        }


class ObiMeterSeriesSensorBase(ObiEnergySensorBase):
    """Base for cumulative meter registers, reported in Wh.

    The register only advances when the tracker reports a new reading, so
    duplicate values are suppressed to avoid flat-lining long-term statistics.
    """

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR

    _data_key: str

    def __init__(self, coordinator: ObiEnergyTrackerCoordinator) -> None:
        """Initialize the meter reading sensor."""
        super().__init__(coordinator)
        self._last_native_value: float | None = None
        self._last_native_value_set = False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator updates and suppress duplicate readings."""
        new_value = self.native_value

        if not self._last_native_value_set or new_value != self._last_native_value:
            self._last_native_value_set = True
            self._last_native_value = new_value
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        """Return the newest reading of the cumulative register."""
        if not self.coordinator.data:
            return None
        return latest_value(self.coordinator.data.get(self._data_key))


class ObiMeterReadingSensor(ObiMeterSeriesSensorBase):
    """Sensor for total imported energy (Zählerstand)."""

    _attr_unique_id = "obi_meter_reading"
    _attr_translation_key = "meter_reading"
    _data_key = "meter"


class ObiMeterExportSensor(ObiMeterSeriesSensorBase):
    """Sensor for total exported energy (feed-in register)."""

    _attr_unique_id = "obi_meter_export"
    _attr_translation_key = "meter_export"
    _data_key = "meter_export"


class ObiPowerSensorBase(ObiEnergySensorBase):
    """Base for power derived by differentiating the meter register.

    The API exposes no power measure at any resolution, so this is the average
    power across the two newest meter samples (~5 minutes apart) rather than an
    instantaneous reading.
    """

    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_suggested_display_precision = 0

    _data_key: str

    @property
    def available(self) -> bool:
        """Report unavailable whenever power could not be derived.

        A tracker that has gone offline is the case that matters: the entity
        has to stop reporting, because leaving the last figure on show reads as
        a live measurement of a house that is no longer being measured.
        """
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        """Return the derived power in watts."""
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get(self._data_key)
        return value if isinstance(value, (int, float)) else None


class ObiPowerSensor(ObiPowerSensorBase):
    """Sensor for current power draw."""

    _attr_unique_id = "obi_power"
    _attr_translation_key = "power"
    _data_key = "power"


class ObiExportPowerSensor(ObiPowerSensorBase):
    """Sensor for current power fed back into the grid."""

    _attr_unique_id = "obi_export_power"
    _attr_translation_key = "export_power"
    _data_key = "export_power"


class ObiDailyEnergySensorBase(ObiEnergySensorBase):
    """Base for energy totals accumulated since local midnight."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR

    _data_key: str

    @property
    def native_value(self) -> float | None:
        """Return today's energy total."""
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get(self._data_key)
        return value if isinstance(value, (int, float)) else None


class ObiDailyEnergySensor(ObiDailyEnergySensorBase):
    """Sensor for energy consumed today."""

    _attr_unique_id = "obi_daily_energy"
    _attr_translation_key = "daily_energy"
    _data_key = "daily_energy"


class ObiDailyExportEnergySensor(ObiDailyEnergySensorBase):
    """Sensor for energy exported today."""

    _attr_unique_id = "obi_daily_export_energy"
    _attr_translation_key = "daily_export_energy"
    _data_key = "daily_export_energy"


class ObiDeviceValueSensorBase(ObiEnergySensorBase):
    """Base sensor for values sourced from coordinator device data."""

    _device_key: str

    @property
    def native_value(self) -> Any:
        """Return value for the configured device key."""
        if not self.coordinator.data:
            return None

        device_data = self.coordinator.data.get("device")
        if not isinstance(device_data, dict):
            return None

        return device_data.get(self._device_key)


class ObiBatteryLevelSensor(ObiDeviceValueSensorBase):
    """Sensor for battery level."""

    _attr_unique_id = "obi_battery_level"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "battery_level"
    _attr_native_unit_of_measurement = "%"
    _device_key = "batteryLevel"


class ObiIsOnlineSensor(ObiDeviceValueSensorBase):
    """Sensor for current online state."""

    _attr_unique_id = "obi_is_online"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_translation_key = "is_online"
    _attr_options = ["online", "offline"]
    _device_key = "isOnline"

    @property
    def native_value(self) -> str | None:
        """Return the online status as enum value."""
        value = super().native_value
        if value is None:
            return None
        return "online" if bool(value) else "offline"


class ObiConnectionStrengthSensor(ObiDeviceValueSensorBase):
    """Sensor for connection strength reported by API."""

    _attr_unique_id = "obi_connection_strength"
    _attr_translation_key = "connection_strength"
    _device_key = "connectionStrength"


class ObiLastRecordReceivedAtSensor(ObiDeviceValueSensorBase):
    """Sensor for timestamp of the last received record."""

    _attr_unique_id = "obi_last_record_received_at"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_record_received_at"
    _device_key = "lastRecordReceivedAt"

    @property
    def native_value(self) -> datetime | None:
        """Return parsed timestamp value."""
        value = super().native_value
        if not isinstance(value, str):
            return None

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
