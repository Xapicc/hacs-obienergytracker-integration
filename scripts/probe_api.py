#!/usr/bin/env python3
"""Standalone probe for the OBI EnergyTracker cloud API.

Run this OUTSIDE of Home Assistant, with your own account credentials.

Subcommands:
    probe    Map the resolution and measures enums, capturing 4xx bodies
             (validation errors often enumerate the valid values).
    params   Omit required params to make the server describe them, and test
             whether bucket size is a query param rather than a path segment.
    paths    Try sibling path shapes for a possible live/realtime endpoint.
    meter    Dump the full meter series and derive watts by differentiating.
    cadence  Poll lastRecordReceivedAt to find the bridge -> cloud cadence.
    all      probe + params + paths + meter (skips the slow cadence poll).

Usage:
    pip install aiohttp pyjwt
    # PowerShell:
    $env:OBI_EMAIL = "you@example.com"
    $env:OBI_PASSWORD = "..."
    python scripts/probe_api.py all
    python scripts/probe_api.py cadence --count 20 --interval 60

Credentials are read from environment variables (or prompted for) so they
never end up in shell history. Everything here is read-only: GETs plus the
one login POST, against your own account.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from getpass import getpass
import os
import statistics
import sys
from typing import Any

import aiohttp
import jwt

LOGIN_URL = "https://www.obi.de/regi/auth/api/public/login"
ENERGY_TRACKING_URL = "https://energy-tracking-backend.prod-eks.dbs.obi.solutions"

HISTORICAL_ACCEPT = (
    "application/vnd.obi.companion.energy-tracking.historical-record.v1+json"
)

# Known-good, used as controls. "zzz_invalid" is a deliberate garbage value:
# comparing its 400 body against a plausible-but-rejected one tells us whether
# the server distinguishes "unknown resolution" from "bad request otherwise".
RESOLUTION_CANDIDATES = [
    "hourly",
    "meter",
    "daily",
    "monthly",
    "zzz_invalid",
    # sub-hourly, every casing/spelling convention the backend might use
    "fiveMinutely",
    "five-minutely",
    "five_minutely",
    "fiveMinute",
    "fiveMinutes",
    "5minutely",
    "5-minutely",
    "5min",
    "5m",
    "PT5M",
    "minutely",
    "MINUTELY",
    "quarterHourly",
    "fifteenMinutely",
    "halfHourly",
    "thirtyMinutely",
    "secondly",
    "detailed",
    "detail",
    "fine",
    "interval",
    # completing the calendar family tells us the enum's naming convention
    "weekly",
    "yearly",
    "annually",
    "HOURLY",
    "DAILY",
]

MEASURE_CANDIDATES = [
    "energy",
    "negative_energy",
    "zzz_invalid",
    "power",
    "negative_power",
    "active_power",
    "activePower",
    "apparent_power",
    "instantaneous_power",
    "power_consumption",
    "powerConsumption",
    "consumption",
    "load",
    "demand",
    "watt",
    "watts",
    "voltage",
    "current",
    "frequency",
    "meter_reading",
    "meterReading",
    "total_energy",
    "totalEnergy",
]

BODY_PREVIEW = 400


def parse_ts(value: str) -> datetime:
    """Parse the API's ISO timestamps (which use a literal Z suffix)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def login(session: aiohttp.ClientSession, email: str, password: str) -> str:
    payload = {"email": email, "password": password, "country": "DE"}
    headers = {
        "Accept-Encoding": "gzip",
        "Connection": "Keep-Alive",
        "Content-Type": "application/json",
        "x-app-type": "b2c",
        "x-obi-locale": "de-DE",
        "User-Agent": "heyOBI APP / Android Phone 30",
    }
    async with session.post(LOGIN_URL, json=payload, headers=headers) as resp:
        resp.raise_for_status()
        data = await resp.json()
        token = data.get("token")
        if not token:
            raise RuntimeError(f"Login succeeded but no token in response: {data}")
        return token


async def get_bridge_and_device(
    session: aiohttp.ClientSession, token: str
) -> tuple[str, str, str]:
    decoded = jwt.decode(token, options={"verify_signature": False})
    user_id = decoded["accountId"]

    headers = {
        "Accept": "application/vnd.obi.companion.energy-tracking.user.v1+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "app_client",
    }
    url = f"{ENERGY_TRACKING_URL}/users/{user_id}"
    async with session.get(url, headers=headers) as resp:
        resp.raise_for_status()
        data = await resp.json()
        bridge = data["bridge"]
        sensor = bridge["sensors"][0]
        return bridge["id"], sensor["id"], user_id


async def fetch(
    session: aiohttp.ClientSession,
    token: str,
    url: str,
    params: dict[str, str] | None = None,
    accept: str = HISTORICAL_ACCEPT,
) -> tuple[int, str, str]:
    """GET and always return (status, content_type, body_text).

    Capturing the body on 4xx is the whole point: validation errors on an
    enum-typed path segment usually name the values they would have accepted.
    """
    headers = {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "User-Agent": "app_client",
    }
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            body = await resp.text()
            return resp.status, resp.headers.get("Content-Type", ""), body
    except aiohttp.ClientError as err:
        return -1, "", f"<ClientError: {err}>"


async def cmd_probe(
    session: aiohttp.ClientSession, token: str, bridge_id: str, device_id: str
) -> None:
    now = datetime.now(timezone.utc)
    duration = f"{(now - timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%S')}Z/PT6H"
    base = f"{ENERGY_TRACKING_URL}/historical-data/{bridge_id}/{device_id}"

    # Axis 1: vary resolution, hold measures at the known-good "energy".
    print("\n=== Resolution axis (measures=energy) ===")
    for resolution in RESOLUTION_CANDIDATES:
        status, ctype, body = await fetch(
            session,
            token,
            f"{base}/{resolution}",
            {"duration": duration, "measures": "energy"},
        )
        print(f"\n[{status}] resolution={resolution}   {ctype}")
        print(f"    {body[:BODY_PREVIEW]}")
        await asyncio.sleep(0.15)

    # Axis 2: vary measures against both known-good resolutions.
    for resolution in ("meter", "hourly"):
        print(f"\n=== Measures axis (resolution={resolution}) ===")
        for measure in MEASURE_CANDIDATES:
            status, ctype, body = await fetch(
                session,
                token,
                f"{base}/{resolution}",
                {"duration": duration, "measures": measure},
            )
            print(f"\n[{status}] measures={measure}   {ctype}")
            print(f"    {body[:BODY_PREVIEW]}")
            await asyncio.sleep(0.15)


async def cmd_params(
    session: aiohttp.ClientSession, token: str, bridge_id: str, device_id: str
) -> None:
    """Probe the query-param axis.

    Two hypotheses here. First, omitting a required param usually produces a
    validation error that enumerates the accepted values. Second, the bucket
    size may be a query param rather than the path segment -- which would
    explain 5-minute data existing without a 5-minute resolution.
    """
    now = datetime.now(timezone.utc)
    duration = f"{(now - timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%S')}Z/PT6H"
    base = f"{ENERGY_TRACKING_URL}/historical-data/{bridge_id}/{device_id}"

    print("\n=== Omitted / malformed params (error bodies enumerate valid input) ===")
    omissions: list[tuple[str, dict[str, str]]] = [
        ("no params at all", {}),
        ("no measures", {"duration": duration}),
        ("no duration", {"measures": "energy"}),
        ("empty measures", {"duration": duration, "measures": ""}),
        ("bad duration format", {"duration": "nonsense", "measures": "energy"}),
        ("duration PT5M", {"duration": f"{(now - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%S')}Z/PT5M", "measures": "energy"}),
    ]
    for label, params in omissions:
        for resolution in ("hourly", "meter"):
            status, ctype, body = await fetch(
                session, token, f"{base}/{resolution}", params
            )
            print(f"\n[{status}] {resolution}: {label}   {ctype}")
            print(f"    {body[:BODY_PREVIEW]}")
            await asyncio.sleep(0.15)

    print("\n=== Extra granularity-style params (on resolution=hourly) ===")
    param_names = [
        "granularity",
        "interval",
        "resolution",
        "bucket",
        "bucketSize",
        "step",
        "period",
        "aggregation",
        "groupBy",
        "sampling",
    ]
    for name in param_names:
        for value in ("PT5M", "5m", "300"):
            params = {"duration": duration, "measures": "energy", name: value}
            status, ctype, body = await fetch(
                session, token, f"{base}/hourly", params
            )
            print(f"\n[{status}] hourly &{name}={value}   {ctype}")
            print(f"    {body[:BODY_PREVIEW]}")
            await asyncio.sleep(0.15)


async def cmd_paths(
    session: aiohttp.ClientSession,
    token: str,
    bridge_id: str,
    device_id: str,
    user_id: str,
) -> None:
    """Look for a live/realtime sibling of /historical-data."""
    print("\n=== Sibling path shapes ===")
    candidates = [
        f"/live-data/{bridge_id}/{device_id}",
        f"/realtime-data/{bridge_id}/{device_id}",
        f"/current-data/{bridge_id}/{device_id}",
        f"/latest-data/{bridge_id}/{device_id}",
        f"/historical-data/{bridge_id}/{device_id}",
        f"/live/{bridge_id}/{device_id}",
        f"/bridges/{bridge_id}",
        f"/bridges/{bridge_id}/sensors/{device_id}",
        f"/bridges/{bridge_id}/sensors/{device_id}/live",
        f"/bridges/{bridge_id}/sensors/{device_id}/latest",
        f"/sensors/{device_id}",
        f"/sensors/{device_id}/live",
        f"/devices/{device_id}",
        f"/users/{user_id}/live",
        "/",
        "/swagger-ui.html",
        "/v3/api-docs",
        "/openapi.json",
        "/actuator",
    ]
    for path in candidates:
        # Accept: */* so a valid resource can't hide behind content negotiation.
        status, ctype, body = await fetch(
            session, token, f"{ENERGY_TRACKING_URL}{path}", accept="*/*"
        )
        print(f"\n[{status}] {path}   {ctype}")
        print(f"    {body[:BODY_PREVIEW]}")
        await asyncio.sleep(0.15)


async def cmd_meter(
    session: aiohttp.ClientSession,
    token: str,
    bridge_id: str,
    device_id: str,
    hours: int,
) -> None:
    """Dump the full meter series and derive watts from consecutive readings."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    duration = f"{start.strftime('%Y-%m-%dT%H:%M:%S')}Z/PT{hours}H"
    url = f"{ENERGY_TRACKING_URL}/historical-data/{bridge_id}/{device_id}/meter"

    for measure in ("energy", "negative_energy"):
        status, _ctype, body = await fetch(
            session, token, url, {"duration": duration, "measures": measure}
        )
        if status != 200:
            print(f"\n=== meter/{measure} -> {status} ===\n{body[:BODY_PREVIEW]}")
            continue

        import json

        records: list[dict[str, Any]] = json.loads(body)
        records.sort(key=lambda r: r["time"])
        print(f"\n=== meter/{measure}: {len(records)} records over {hours}h ===")

        if not records:
            continue

        gaps: list[float] = []
        print(f"{'timestamp':<26} {'value':>14} {'dt_s':>7} {'d_value':>10} "
              f"{'W (raw=Wh)':>12} {'W (raw=mWh)':>12}")
        prev_t: datetime | None = None
        prev_v: float | None = None
        for rec in records:
            t = parse_ts(rec["time"])
            v = float(rec["value"])
            if prev_t is None or prev_v is None:
                print(f"{rec['time']:<26} {v:>14.0f} {'-':>7} {'-':>10} "
                      f"{'-':>12} {'-':>12}")
            else:
                dt_s = (t - prev_t).total_seconds()
                dv = v - prev_v
                gaps.append(dt_s)
                if dt_s > 0:
                    w_wh = dv / (dt_s / 3600.0)
                    w_mwh = (dv / 1000.0) / (dt_s / 3600.0)
                    print(f"{rec['time']:<26} {v:>14.0f} {dt_s:>7.0f} {dv:>10.0f} "
                          f"{w_wh:>12.1f} {w_mwh:>12.1f}")
            prev_t, prev_v = t, v

        if gaps:
            print(
                f"\n  sample gap seconds: min={min(gaps):.0f} "
                f"median={statistics.median(gaps):.0f} max={max(gaps):.0f}"
            )
            print(
                f"  total delta over window: {float(records[-1]['value']) - float(records[0]['value']):.0f} "
                f"raw units across {(parse_ts(records[-1]['time']) - parse_ts(records[0]['time'])).total_seconds() / 3600:.2f}h"
            )
        await asyncio.sleep(0.15)


async def cmd_cadence(
    session: aiohttp.ClientSession, token: str, user_id: str, count: int, interval: float
) -> None:
    """Log lastRecordReceivedAt across polls to find the real update cadence."""
    url = f"{ENERGY_TRACKING_URL}/users/{user_id}"
    headers = {
        "Accept": "application/vnd.obi.companion.energy-tracking.user.v1+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "app_client",
    }

    print(f"\n=== Polling lastRecordReceivedAt x{count} every {interval:.0f}s ===")
    prev: str | None = None
    prev_change: datetime | None = None
    for i in range(count):
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            sensor = data["bridge"]["sensors"][0]
            last = sensor.get("lastRecordReceivedAt")

        note = ""
        if prev is not None and last != prev:
            changed_at = parse_ts(last)
            if prev_change is not None:
                note = f"  <-- CHANGED (+{(changed_at - prev_change).total_seconds():.0f}s since last change)"
            else:
                note = "  <-- CHANGED"
            prev_change = changed_at
        elif prev is None and last:
            prev_change = parse_ts(last)

        age = (datetime.now(timezone.utc) - parse_ts(last)).total_seconds() if last else -1
        print(
            f"[{i:>2}] {datetime.now(timezone.utc).strftime('%H:%M:%S')} "
            f"lastRecordReceivedAt={last} (age {age:.0f}s){note}"
        )
        prev = last
        if i < count - 1:
            await asyncio.sleep(interval)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["probe", "params", "paths", "meter", "cadence", "all"],
    )
    parser.add_argument("--count", type=int, default=20, help="cadence: poll count")
    parser.add_argument(
        "--interval", type=float, default=60.0, help="cadence: seconds between polls"
    )
    parser.add_argument("--hours", type=int, default=6, help="meter: window size")
    args = parser.parse_args()

    email = os.environ.get("OBI_EMAIL") or input("OBI email: ")
    password = os.environ.get("OBI_PASSWORD") or getpass("OBI password: ")

    async with aiohttp.ClientSession() as session:
        print("Logging in...")
        token = await login(session, email, password)
        bridge_id, device_id, user_id = await get_bridge_and_device(session, token)
        print(f"bridge_id={bridge_id} device_id={device_id}")

        if args.command in ("probe", "all"):
            await cmd_probe(session, token, bridge_id, device_id)
        if args.command in ("params", "all"):
            await cmd_params(session, token, bridge_id, device_id)
        if args.command in ("paths", "all"):
            await cmd_paths(session, token, bridge_id, device_id, user_id)
        if args.command in ("meter", "all"):
            await cmd_meter(session, token, bridge_id, device_id, args.hours)
        if args.command == "cadence":
            await cmd_cadence(session, token, user_id, args.count, args.interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
