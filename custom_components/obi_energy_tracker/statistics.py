"""Long-term statistics import for Obi EnergyTracker.

The Energy dashboard works in hourly buckets. Publishing sensor states alone
leaves those buckets aligned to whenever Home Assistant happened to poll, and
gives no history from before the integration was installed. The hourly
resolution of /historical-data already returns hour-aligned energy totals, so
importing them as external statistics gives the dashboard correct buckets and
backfilled history for free.
"""

from __future__ import annotations

from datetime import datetime
import logging
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)

try:  # Home Assistant 2025.2+
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_TYPE_NONE: Any | None = StatisticMeanType.NONE
except ImportError:  # older cores only understand has_mean
    _MEAN_TYPE_NONE = None
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import parse_records
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STATISTIC_CONSUMPTION = f"{DOMAIN}:energy_consumption"
STATISTIC_PRODUCTION = f"{DOMAIN}:energy_production"

STATISTIC_NAMES = {
    STATISTIC_CONSUMPTION: "OBI EnergyTracker Consumption",
    STATISTIC_PRODUCTION: "OBI EnergyTracker Production",
}


def _last_imported_hour(row: dict[str, Any]) -> datetime | None:
    """Read the start time of an existing statistics row.

    Home Assistant switched this field from a datetime to a float timestamp,
    so accept either.
    """
    raw_start = row.get("start")
    if isinstance(raw_start, datetime):
        return dt_util.as_utc(raw_start)
    if isinstance(raw_start, (int, float)):
        return dt_util.utc_from_timestamp(raw_start)
    return None


async def async_import_hourly_statistics(
    hass: HomeAssistant,
    statistic_id: str,
    records: Any,
) -> None:
    """Import hour-aligned energy records as external statistics.

    Statistics carry a running sum, so each new hour is added on top of the
    total already stored rather than recomputed from the whole series.
    """
    parsed = parse_records(records)
    if not parsed:
        return

    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )

    total = 0.0
    last_start: datetime | None = None
    if last_stats and last_stats.get(statistic_id):
        row = last_stats[statistic_id][0]
        total = row.get("sum") or 0.0
        last_start = _last_imported_hour(row)

    # The current hour is still accumulating; importing it now would store a
    # partial value that later hours would be summed on top of.
    cutoff = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)

    statistics: list[StatisticData] = []
    for timestamp, value in parsed:
        start = timestamp.replace(minute=0, second=0, microsecond=0)
        if start >= cutoff:
            continue
        if last_start is not None and start <= last_start:
            continue
        total += value
        statistics.append(StatisticData(start=start, state=value, sum=total))

    if not statistics:
        return

    metadata = StatisticMetaData(
        has_sum=True,
        name=STATISTIC_NAMES.get(statistic_id, statistic_id),
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfEnergy.WATT_HOUR,
    )
    # has_mean is deprecated and stops working in 2026.11; mean_type replaces
    # it. Energy totals have no meaningful mean, hence NONE.
    if _MEAN_TYPE_NONE is not None:
        metadata["mean_type"] = _MEAN_TYPE_NONE
    else:
        metadata["has_mean"] = False

    async_add_external_statistics(hass, metadata, statistics)
    _LOGGER.debug(
        "Imported %d hourly statistics rows for %s (running total %.0f Wh)",
        len(statistics),
        statistic_id,
        total,
    )
