"""API client for Obi EnergyTracker."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Any

from aiohttp import ClientError, ClientSession
import jwt

_LOGGER = logging.getLogger(__name__)

# API endpoints
LOGIN_URL = "https://www.obi.de/regi/auth/api/public/login"
ENERGY_TRACKING_URL = "https://energy-tracking-backend.prod-eks.dbs.obi.solutions"

# The meter register is sampled roughly every 5 minutes. A 6-hour window keeps
# a useful history without the payload getting large (~70 records).
METER_WINDOW_HOURS = 6

# Pairs closer together than this are duplicate records (the API occasionally
# emits two a second apart); dividing by such a tiny interval turns 1 Wh of
# rounding into a nonsense power figure.
MIN_SAMPLE_SECONDS = 30
# Past this the sensor has gone quiet, and an "average power" spanning the
# outage would be meaningless.
MAX_SAMPLE_SECONDS = 1800
# Above this it is a backfill artifact rather than consumption -- when a
# tracker is first set up it replays its accumulated total in one jump.
MAX_PLAUSIBLE_POWER_W = 50000


def parse_records(records: Any) -> list[tuple[datetime, float]]:
    """Normalise the API's [{"time": ..., "value": ...}] payload.

    Returns (timestamp, value) pairs sorted oldest first, skipping anything
    malformed.
    """
    if not isinstance(records, list):
        return []

    parsed: list[tuple[datetime, float]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        time_str = record.get("time")
        value = record.get("value")
        if not isinstance(time_str, str) or not isinstance(value, (int, float)):
            continue
        try:
            timestamp = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        parsed.append((timestamp, float(value)))

    parsed.sort(key=lambda item: item[0])
    return parsed


def latest_value(records: Any) -> float | None:
    """Return the newest value in a record series."""
    parsed = parse_records(records)
    return parsed[-1][1] if parsed else None


def sum_values(records: Any) -> float | None:
    """Return the total across a bucketed series."""
    parsed = parse_records(records)
    if not parsed:
        return None
    return sum(value for _timestamp, value in parsed)


def derive_power_w(records: Any) -> float | None:
    """Derive average power in W from a cumulative Wh meter series.

    The API has no power measure at any resolution, so power comes from
    differentiating consecutive readings of the cumulative register. Walks
    backwards from the newest record until it finds a sample far enough apart
    to divide by safely.
    """
    parsed = parse_records(records)
    if len(parsed) < 2:
        return None

    newest_time, newest_value = parsed[-1]
    for timestamp, value in reversed(parsed[:-1]):
        elapsed = (newest_time - timestamp).total_seconds()
        if elapsed < MIN_SAMPLE_SECONDS:
            continue
        if elapsed > MAX_SAMPLE_SECONDS:
            return None

        delta = newest_value - value
        if delta < 0:
            # Register went backwards: a reset or a corrected reading.
            return None

        power = delta / (elapsed / 3600)
        if power > MAX_PLAUSIBLE_POWER_W:
            return None
        return round(power, 1)

    return None


class ObiEnergyTrackerAPI:
    """API client for Obi EnergyTracker."""

    def __init__(
        self,
        session: ClientSession,
        email: str,
        password: str,
        country: str = "DE",
        bridge_id: str | None = None,
        device_id: str | None = None,
    ) -> None:
        """Initialize the API client."""
        self.session = session
        self.email = email
        self.password = password
        self.country = country
        self.token: str | None = None
        self.bridge_id = bridge_id
        self.device_id = device_id

    async def async_login(self) -> bool:
        """Authenticate with the Obi EnergyTracker API."""
        try:
            payload = {
                "email": self.email,
                "password": self.password,
                "country": self.country,
            }

            headers = {
                "Accept-Encoding": "gzip",
                "Connection": "Keep-Alive",
                "Content-Type": "application/json",
                "x-app-type": "b2c",
                "x-obi-locale": "de-DE",
                "User-Agent": "heyOBI APP / Android Phone 30",
            }

            async with self.session.post(
                LOGIN_URL, json=payload, headers=headers
            ) as response:
                if response.status != 200:
                    _LOGGER.error(
                        "Login failed with status %d",
                        response.status,
                    )
                    return False

                data = await response.json()
                self.token = data.get("token")

                if not self.token:
                    _LOGGER.error("No token received from login response")
                    return False

                _LOGGER.debug("Successfully authenticated with Obi EnergyTracker")
                return True
        except (OSError, ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("Login error: %s", err)
            return False

    async def async_get_bridge_info(self) -> dict[str, Any] | None:
        """Get bridge/device IDs and current device details from user profile."""
        if not self.token:
            return None

        try:
            # Decode JWT to get userId
            decoded_token = jwt.decode(self.token, options={"verify_signature": False})
            user_id = decoded_token.get("accountId")

            if not user_id:
                _LOGGER.error("No accountId found in token")
                return None

            url = f"{ENERGY_TRACKING_URL}/users/{user_id}"
            headers = {
                "Accept": "application/vnd.obi.companion.energy-tracking.user.v1+json",
                "Accept-Encoding": "gzip",
                "User-Agent": "app_client",
                "Authorization": f"Bearer {self.token}",
                "Connection": "Keep-Alive",
            }

            async with self.session.get(url, headers=headers) as response:
                if response.status != 200:
                    _LOGGER.error("Failed to get user info: %d", response.status)
                    return None

                data = await response.json()
                bridge = data.get("bridge")
                if not bridge:
                    _LOGGER.error("No bridge found in user info")
                    return None

                self.bridge_id = bridge.get("id")
                sensors = bridge.get("sensors", [])
                selected_sensor: dict[str, Any] | None = None

                if self.device_id:
                    selected_sensor = next(
                        (
                            sensor
                            for sensor in sensors
                            if isinstance(sensor, dict)
                            and sensor.get("id") == self.device_id
                        ),
                        None,
                    )

                if selected_sensor is None and sensors:
                    first_sensor = sensors[0]
                    selected_sensor = (
                        first_sensor if isinstance(first_sensor, dict) else None
                    )

                if selected_sensor:
                    self.device_id = selected_sensor.get("id")

                if not self.bridge_id or not self.device_id:
                    _LOGGER.error("Could not find bridge_id or device_id")
                    return None

                return {
                    "bridge_id": self.bridge_id,
                    "device_id": self.device_id,
                    "batteryLevel": selected_sensor.get("batteryLevel")
                    if selected_sensor
                    else None,
                    "isOnline": selected_sensor.get("isOnline")
                    if selected_sensor
                    else None,
                    "connectionStrength": selected_sensor.get("connectionStrength")
                    if selected_sensor
                    else None,
                    "lastRecordReceivedAt": selected_sensor.get("lastRecordReceivedAt")
                    if selected_sensor
                    else None,
                }
        except (jwt.DecodeError, OSError, ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("Error getting bridge info: %s", err)
            return None

    async def async_get_device_info(self) -> dict[str, Any] | None:
        """Get current device details from the bridge info response."""
        bridge_info = await self.async_get_bridge_info()
        if not bridge_info:
            return None

        return {
            "batteryLevel": bridge_info.get("batteryLevel"),
            "isOnline": bridge_info.get("isOnline"),
            "connectionStrength": bridge_info.get("connectionStrength"),
            "lastRecordReceivedAt": bridge_info.get("lastRecordReceivedAt"),
        }

    async def async_get_historical_data(
        self,
        resolution: str,
        measure: str,
        start: datetime,
        hours: int,
    ) -> Any | None:
        """Fetch one measure from /historical-data at the given resolution.

        The endpoint takes an ISO 8601 interval ("<start>Z/PT<n>H") and returns
        a list of {"time": ..., "value": ...} records. Only one measure per
        request: the records carry no per-measure key, so a combined request
        would leave no way to tell the series apart.

        Probing established that the only valid resolutions are hourly, meter,
        daily and monthly, and the only valid measures are energy and
        negative_energy. Anything else returns 400.
        """
        if not self.bridge_id or not self.device_id:
            return None

        try:
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            start_utc = start.astimezone(timezone.utc)
            duration_str = f"{start_utc.strftime('%Y-%m-%dT%H:%M:%S')}Z/PT{hours}H"

            url = (
                f"{ENERGY_TRACKING_URL}/historical-data/"
                f"{self.bridge_id}/{self.device_id}/{resolution}"
            )
            params = {"duration": duration_str, "measures": measure}

            status, data, _content_type = await self._authorized_get(url, params)
            if status == 200:
                return data
            _LOGGER.error("Failed to get %s/%s data: %d", resolution, measure, status)
            return None
        except (OSError, ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("Error getting %s/%s data: %s", resolution, measure, err)
            return None

    async def async_get_hourly_data(
        self,
        start_date: datetime | None = None,
        num_days: int = 1,
        measure: str = "energy",
    ) -> Any | None:
        """Get hourly energy data for multiple days.

        Args:
            start_date: Start date for data retrieval (defaults to now, UTC)
            num_days: Number of days to fetch (default 1)
            measure: Either "energy" (import) or "negative_energy" (export)
        """
        if start_date is None:
            start_date = datetime.now(timezone.utc)
        elif start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)

        # 23:00 UTC is local midnight in CET/CEST. Step back num_days so the
        # window *ends* tonight rather than running num_days into the future.
        start = (start_date - timedelta(days=num_days)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        return await self.async_get_historical_data(
            "hourly", measure, start, num_days * 24
        )

    async def async_get_meter_data(
        self,
        measure: str = "energy",
        hours: int = METER_WINDOW_HOURS,
    ) -> Any | None:
        """Get the cumulative meter register (Zählerstand) series."""
        start = datetime.now(timezone.utc) - timedelta(hours=hours)
        return await self.async_get_historical_data("meter", measure, start, hours)

    async def async_get_daily_data(
        self,
        start_of_day: datetime,
        measure: str = "energy",
    ) -> Any | None:
        """Get energy bucketed by day, for the 24h from local midnight.

        Records are clipped to the requested window, so a window spanning a
        bucket boundary returns one record per bucket and the caller sums them.
        """
        return await self.async_get_historical_data("daily", measure, start_of_day, 24)

    async def _ensure_valid_token(self) -> bool:
        """Make sure self.token is present and not about to expire."""
        if not self.token:
            return await self.async_login()

        try:
            decoded = jwt.decode(self.token, options={"verify_signature": False})
        except jwt.DecodeError:
            return await self.async_login()

        exp = decoded.get("exp")
        if exp is not None and exp - time.time() < 60:
            return await self.async_login()

        return True

    async def _authorized_get(
        self, url: str, params: dict[str, Any]
    ) -> tuple[int, Any, str | None]:
        """GET an authorized endpoint.

        Refreshes the token and retries once on 401, and backs off on 429.
        Returns (status_code, json_body_or_none, content_type_or_none).
        """
        max_attempts = 3
        for attempt in range(max_attempts):
            if not await self._ensure_valid_token():
                return 401, None, None

            headers = self._get_auth_headers()
            async with self.session.get(
                url, params=params, headers=headers
            ) as response:
                if response.status == 401 and attempt < max_attempts - 1:
                    _LOGGER.debug("Token rejected (401), forcing re-login")
                    self.token = None
                    continue

                if response.status == 429 and attempt < max_attempts - 1:
                    retry_after = float(response.headers.get("Retry-After", 2**attempt))
                    _LOGGER.debug("Rate limited (429), backing off %.1fs", retry_after)
                    await asyncio.sleep(retry_after)
                    continue

                content_type = response.headers.get("Content-Type")
                if response.status == 200:
                    return response.status, await response.json(), content_type
                return response.status, None, content_type

        return 429, None, None

    def _get_auth_headers(self) -> dict[str, str]:
        """Get headers with authorization token."""
        accept_header = (
            "application/vnd.obi.companion.energy-tracking.historical-record.v1+json"
        )
        return {
            "Accept": accept_header,
            "Accept-Encoding": "gzip",
            "User-Agent": "app_client",
            "Authorization": f"Bearer {self.token}",
            "Connection": "Keep-Alive",
        }
