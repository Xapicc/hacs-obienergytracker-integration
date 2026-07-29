# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Home Assistant custom integration (installed via HACS) for the OBI Energy Tracker — a
cheap smart-meter reader normally used through the heyOBI phone app. The integration talks
to OBI's cloud backend using API calls reverse-engineered from that app; there is no local
device access. All code lives in `custom_components/obi_energy_tracker/`.

## Commands

There is no test suite, no `pyproject.toml`, and no local build step — the integration is
loaded by Home Assistant directly from source.

```bash
ruff check custom_components scripts     # lint (matches the CI job's intent)
ruff format custom_components scripts

# Exercise the real API outside Home Assistant (read-only: GETs + one login POST)
pip install aiohttp pyjwt
$env:OBI_EMAIL = "you@example.com"; $env:OBI_PASSWORD = "..."   # PowerShell
python scripts/probe_api.py all          # or: probe | params | paths | meter | cadence
python scripts/probe_api.py meter --hours 6
```

CI runs Home Assistant `hassfest` and `hacs/action` validation on push/PR
(`.github/workflows/validate.yml`). Note that `.github/workflows/lint.yml` currently only
checks out the repo — the ruff step is missing, so lint is effectively not enforced in CI.

Releasing to HACS means bumping `version` in `manifest.json` and tagging; HACS installs by
release tag.

## The constraint everything else follows from

`/historical-data/{bridge}/{device}/{resolution}` accepts only `hourly`, `meter`, `daily`,
`monthly` as resolutions and only `energy`, `negative_energy` as measures. Anything else is
HTTP 400. **There is no power measure at any resolution.** Worse, `hourly`/`daily`/`monthly`
return a *single aggregate record* for the whole requested window, not one record per
bucket — a 7-day `hourly` request yields one row, not 168.

Only `meter` returns a real series: the cumulative register at ~5-minute intervals, 1 Wh
granularity. So:

- **Power** is derived by differentiating consecutive meter readings (`derive_power_w`) —
  an *average* over the interval, not instantaneous.
- **Hourly statistics** are derived from the meter register too (`hourly_energy_from_meter`),
  not from the `hourly` resolution.
- `async_get_hourly_data` still exists in `api.py` but is unused for exactly this reason.

Before "fixing" any of this, re-verify with `scripts/probe_api.py` rather than assuming the
API grew a better endpoint. The probe deliberately includes garbage values (`zzz_invalid`)
as controls and captures 4xx bodies, since validation errors tend to enumerate valid input.

## Architecture

**`api.py`** — the cloud client plus a set of *pure* module-level functions
(`parse_records`, `latest_value`, `sum_values`, `derive_power_w`,
`hourly_energy_from_meter`) that hold all the signal-processing logic. These take raw API
payloads and are the right place to change how readings are interpreted. The guard
thresholds at the top of the file (`MIN_SAMPLE_SECONDS`, `MAX_SAMPLE_SECONDS`,
`MAX_READING_AGE_SECONDS`, `MAX_PLAUSIBLE_POWER_W`, `METER_WINDOW_HOURS`) reject duplicate
records, meaningless divisions across an outage, and the one-off backfill jump a
newly-registered tracker emits — each rejection returns `None`/skips rather than guessing.

Two of those deserve care, because an offline tracker is not the same thing as missing data:
the API keeps serving the same 6-hour window for hours afterwards. `derive_power_w` therefore
takes a `now` and ages out on `MAX_READING_AGE_SECONDS`, since stale records still
differentiate cleanly and would otherwise republish pre-outage power as current. Conversely
`hourly_energy_from_meter` *keeps* the energy behind a gap — the register is read from the
physical meter and never stops advancing — and credits it to the hour the tracker came back,
because hours already imported are never revisited and aiming at them would silently lose
the total.

Auth: login POST returns a JWT; `bridge_id`/`device_id` come from decoding the JWT's
`accountId` and reading `/users/{id}`. `_authorized_get` refreshes the token pre-emptively
(60s before `exp`), retries once on 401, and backs off on 429.

**`coordinator.py`** — one `DataUpdateCoordinator` polling every 3 minutes: both meter series
(import + export), derived power, device status, and the daily totals. Statistics import
piggybacks on the meter data already fetched, so it costs no extra requests, and its failures
are logged and swallowed so a recorder problem can't take the sensors down.

`SLOW_REFRESH_INTERVAL` must stay **below** `SCAN_INTERVAL`. It is only ever tested on a poll
tick, so a value at or above the poll period is never satisfied in time and rounds the
refresh up to the following tick — it was 5 minutes tested every 3, which produced a
6-minute cadence and visibly stale device fields. It survives only to stop bursts of manual
refreshes multiplying requests.

Two rules about not blanking good data, both learned from live incidents. `_async_update_slow_data`
keeps values **per key**: three independent requests, and one failing says nothing about the
others — the old all-or-nothing guard would drop `Energy Today` (a `TOTAL_INCREASING` sensor)
to `unknown` whenever the daily call alone failed. And `_merge_device` carries the last known
value into any field the backend returns as null, which it does intermittently for
`connectionStrength` and `lastRecordReceivedAt` while `batteryLevel`/`isOnline` stay
populated. Test falsey values when touching that merge: `isOnline` is legitimately `False`
and a battery legitimately reads `0`, so it keys on `is not None`, never truthiness.

**`statistics.py`** — imports hour-aligned totals as *external* statistics
(`obi_energy_tracker:energy_consumption` / `:energy_production`), which is what makes the
Energy dashboard bucket by real clock hours instead of by poll time. Two invariants: the
current (still-accumulating) hour is never imported, and each import continues the running
`sum` read back via `get_last_statistics` rather than recomputing from the series. Backfill
therefore reaches back only as far as `METER_WINDOW_HOURS`.

This file carries deliberate cross-version shims — `mean_type` vs deprecated `has_mean`,
`unit_class`, and `start` being either a `datetime` or a float timestamp. Keep the
try/except-ImportError pattern when touching it; the integration targets a range of HA cores,
not just the newest.

Note the split in where numbers come from: the *Energy Today* sensors are the API's own
`daily` aggregate (`async_get_daily_data` + `sum_values`), not something this integration
computes. Only the Energy dashboard statistics are derived, and only because the API offers
no usable per-hour series.

**`sensor.py`** — thin `CoordinatorEntity` wrappers reading keys out of `coordinator.data`.
Two pieces of real logic: `ObiMeterSeriesSensorBase` suppresses writes when the register
value is unchanged, so long-term statistics don't flat-line between the tracker's ~5-minute
updates; and `ObiPowerSensorBase` overrides `available`, so power that cannot be derived reads as
unavailable rather than lingering on the last value.

## Conventions

- Home Assistant style: `from __future__ import annotations`, `async_`-prefixed coroutines,
  `_attr_*` class attributes, `_attr_translation_key` with strings in `strings.json` +
  `translations/en.json` (both must be updated together for a new sensor).
- The codebase comments *why*, not *what* — most existing comments explain an API quirk or a
  guard threshold. Match that; don't add narration of obvious code.
- Catch specific exceptions (`OSError`, `ClientError`, `asyncio.TimeoutError`,
  `jwt.DecodeError`), log, and degrade gracefully — never bare `except`.
- `quality_scale.yaml` tracks Bronze-tier compliance and is checked by hassfest. Some of its
  claims are aspirational (it references config-flow tests that don't exist in this repo).
- Users' credentials live in the config entry; `.env` is gitignored and only feeds
  `scripts/probe_api.py`.

## Loose ends worth knowing

- `ObiEnergyTrackerOptionsFlow` exists and `async_supports_options_flow` returns `True`, but
  no `async_get_options_flow` wires it up, and the flow shows an empty form.
- `DEFAULT_SCAN_INTERVAL` and the `ATTR_*` constants in `const.py` are unused; the real
  intervals are `SCAN_INTERVAL`/`SLOW_REFRESH_INTERVAL` in `coordinator.py`.
