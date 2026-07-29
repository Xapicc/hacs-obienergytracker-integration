"""Data update coordinator for Obi EnergyTracker."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ObiEnergyTrackerAPI,
    derive_power_w,
    hourly_energy_from_meter,
    sum_values,
)
from .const import DOMAIN
from .statistics import (
    STATISTIC_CONSUMPTION,
    STATISTIC_PRODUCTION,
    async_import_hourly_statistics,
)

_LOGGER = logging.getLogger(__name__)

# The meter register updates roughly every 5 minutes. Polling every 3 minutes
# stays inside one sample period without the request volume getting silly.
SCAN_INTERVAL = timedelta(minutes=3)

# Device status and the daily totals refresh on every cycle, alongside the
# meter. This has to stay below SCAN_INTERVAL: the gate is only ever tested on
# a poll tick, so an interval at or above the poll period is never satisfied in
# time and silently rounds the refresh up to the following tick -- five minutes
# tested every three produced a six-minute cadence, and device fields sat
# visibly stale in between. What remains guards against bursts of manual
# refreshes multiplying the request count, not against the normal schedule.
SLOW_REFRESH_INTERVAL = timedelta(minutes=2)


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

        Every cycle pulls the cumulative meter series, derives power from it,
        tops up long-term statistics, and refreshes device status and the daily
        totals. Each of the underlying values survives a failure of the others.
        """
        now = dt_util.utcnow()
        meter = await self.api.async_get_meter_data("energy")
        meter_export = await self.api.async_get_meter_data("negative_energy")

        if meter is None and meter_export is None and self._slow_fetched_at is not None:
            raise UpdateFailed("No meter data returned from Obi EnergyTracker API")

        await self._async_update_slow_data(now)

        if meter is None and not self._slow_data:
            raise UpdateFailed("No data returned from Obi EnergyTracker API")

        # Passing the clock in matters: a tracker that has dropped off keeps
        # being served its last records, and power has to age out with them.
        power = derive_power_w(meter, now)
        export_power = derive_power_w(meter_export, now)
        _LOGGER.debug("Derived power: %s W (export %s W)", power, export_power)

        # Statistics come from the meter series that was just fetched, so this
        # costs no extra requests.
        await self._async_import_statistics(meter, meter_export)

        return {
            "meter": meter,
            "meter_export": meter_export,
            "power": power,
            "export_power": export_power,
            "daily_energy": sum_values(self._slow_data.get("daily")),
            "daily_export_energy": sum_values(self._slow_data.get("daily_export")),
            "device": self._slow_data.get("device"),
        }

    async def _async_update_slow_data(self, now: datetime) -> None:
        """Refresh device status and the daily aggregates."""
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

        if device is not None:
            device = self._merge_device(device)

        # Kept per key rather than all-or-nothing. These are three independent
        # requests, and one of them failing says nothing about the other two --
        # writing None into the lot would blank sensors that had just been
        # answered correctly. Energy Today is the one that matters: it is
        # TOTAL_INCREASING, so dropping it to unknown puts a hole in a series
        # that is supposed to only ever climb.
        fetched = {"device": device, "daily": daily, "daily_export": daily_export}
        refreshed = {key: value for key, value in fetched.items() if value is not None}

        if not refreshed:
            _LOGGER.debug("Slow refresh returned no data, keeping previous values")
            return

        self._slow_data = {**self._slow_data, **refreshed}
        self._slow_fetched_at = now

    def _merge_device(self, device: dict[str, Any]) -> dict[str, Any]:
        """Carry the last known value into any field the API left null.

        The backend intermittently answers with connectionStrength and
        lastRecordReceivedAt set to null while batteryLevel and isOnline are
        still populated -- the request succeeded, the fields just came back
        empty. Publishing that through blanks those two sensors to `unknown`
        until the next cycle, and the reading from three minutes ago is a
        better answer than no reading at all.

        This cannot hide a tracker that has genuinely stopped reporting:
        online status comes from the same payload, and the power sensors age
        out on their own once readings stop arriving.
        """
        previous = self._slow_data.get("device")
        if not isinstance(previous, dict):
            return device

        # `is not None` rather than truthiness -- isOnline is legitimately
        # False, and a battery can legitimately read 0.
        return {
            key: value if value is not None else previous.get(key)
            for key, value in device.items()
        }

    async def _async_import_statistics(self, meter: Any, meter_export: Any) -> None:
        """Feed hourly energy, derived from the meter register, into statistics.

        Failures here must not take the sensors down with them, so they are
        logged and swallowed.
        """
        for statistic_id, records in (
            (STATISTIC_CONSUMPTION, meter),
            (STATISTIC_PRODUCTION, meter_export),
        ):
            if records is None:
                continue
            try:
                await async_import_hourly_statistics(
                    self.hass, statistic_id, hourly_energy_from_meter(records)
                )
            except (HomeAssistantError, KeyError, TypeError, ValueError) as err:
                _LOGGER.warning(
                    "Failed to import statistics for %s: %s", statistic_id, err
                )
