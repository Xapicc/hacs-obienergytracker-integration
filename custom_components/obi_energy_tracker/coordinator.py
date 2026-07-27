"""Data update coordinator for Obi EnergyTracker."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ObiEnergyTrackerAPI, derive_power_w, sum_values
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The meter register updates roughly every 5 minutes. Polling every 3 minutes
# stays inside one sample period without the request volume getting silly.
SCAN_INTERVAL = timedelta(minutes=3)

# Device status and the daily/hourly aggregates cannot change faster than the
# underlying meter samples, so they ride a slower cycle than the power
# calculation.
SLOW_REFRESH_INTERVAL = timedelta(minutes=5)

DAYS_OF_HISTORY = 7


class ObiEnergyTrackerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Data update coordinator for Obi EnergyTracker."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: ObiEnergyTrackerAPI,
        config_entry: Any,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
            config_entry=config_entry,
        )
        self.api = api
        self._slow_data: dict[str, Any] = {}
        self._slow_fetched_at: datetime | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the API.

        Every cycle pulls the cumulative meter series and derives power from
        it. Device status and the daily/hourly aggregates refresh on a slower
        cycle.
        """
        meter = await self.api.async_get_meter_data("energy")
        meter_export = await self.api.async_get_meter_data("negative_energy")

        if meter is None and meter_export is None and self._slow_fetched_at is not None:
            raise UpdateFailed("No meter data returned from Obi EnergyTracker API")

        await self._async_update_slow_data()

        if meter is None and not self._slow_data:
            raise UpdateFailed("No data returned from Obi EnergyTracker API")

        power = derive_power_w(meter)
        export_power = derive_power_w(meter_export)
        _LOGGER.debug("Derived power: %s W (export %s W)", power, export_power)

        return {
            "meter": meter,
            "meter_export": meter_export,
            "power": power,
            "export_power": export_power,
            "daily_energy": sum_values(self._slow_data.get("daily")),
            "daily_export_energy": sum_values(self._slow_data.get("daily_export")),
            "hourly": self._slow_data.get("hourly"),
            "device": self._slow_data.get("device"),
        }

    async def _async_update_slow_data(self) -> None:
        """Refresh device status and the daily/hourly aggregates if due."""
        now = dt_util.utcnow()
        if (
            self._slow_fetched_at is not None
            and now - self._slow_fetched_at < SLOW_REFRESH_INTERVAL
        ):
            return

        device = await self.api.async_get_device_info()
        start_of_day = dt_util.start_of_local_day()
        daily = await self.api.async_get_daily_data(start_of_day, "energy")
        daily_export = await self.api.async_get_daily_data(
            start_of_day, "negative_energy"
        )
        hourly = await self.api.async_get_hourly_data(num_days=DAYS_OF_HISTORY)

        # Keep the previous values on a failed refresh rather than blanking the
        # sensors; the next cycle retries.
        if device is None and daily is None and hourly is None:
            _LOGGER.debug("Slow refresh returned no data, keeping previous values")
            return

        self._slow_data = {
            "device": device,
            "daily": daily,
            "daily_export": daily_export,
            "hourly": hourly,
        }
        self._slow_fetched_at = now
