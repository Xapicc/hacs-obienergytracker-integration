# "OBI Energy Tracker" - HACS Integration
This integration allows you to monitor your **OBI Energy Tracker** device directly within Home Assistant. The OBI Energy Tracker is a cost-effective solution for reading smart energy meters, typically accessed via the heyOBI smartphone application.e.

## Installation

Add this repository, via custom repository: https://www.hacs.xyz/docs/faq/custom_repositories/

## OBI Energy Tracker

<img src="https://bilder.obi.de/d9c6b340-b37f-48fd-92f2-72114bad03ad/prZZK/image.jpeg" width="200" alt="Energy Tracker Device">

The "OBI Energy Tracker" is a low cost device to read out smart energy meters. In default you can access the data in the "heyOBI" application on our smartphone.
I extracted the API Calls from the backend of the application, and created this "Home Assistant" Integration.

## Configuration

During setup, you'll need:

- **Email**: Your "OBI" account email address
- **Password**: Your "OBI" account password
- **Country**: Country code (default: DE for Germany)

## Sensors

| Sensor | Unit | Description |
| --- | --- | --- |
| Meter Reading | Wh | Cumulative import register (Zählerstand) |
| Meter Reading (Export) | Wh | Cumulative feed-in register |
| Power | W | Current draw, derived from the meter register |
| Export Power | W | Current feed-in |
| Energy Today | Wh | Consumption since local midnight |
| Energy Exported Today | Wh | Feed-in since local midnight |
| Battery Level | % | Tracker battery |
| Online Status | | Whether the tracker is reachable |
| Connection Strength | | Signal quality reported by the bridge |
| Last Record Received At | | Timestamp of the newest reading in the cloud |

### About the Power sensor

The OBI backend exposes no power measure at any resolution — only cumulative
energy. Power is therefore derived by differentiating consecutive readings of
the meter register, which the tracker updates roughly every 5 minutes. The
value is the *average* power across that interval, not an instantaneous
reading, so short spikes are averaged out. The sensor reports unavailable
rather than guessing when readings are too far apart, when the register goes
backwards, or during the backfill jump that happens when a tracker is first
set up.

It also goes unavailable once the newest reading is more than 30 minutes old,
which is what happens when the tracker drops off the network. The API keeps
serving the same records for hours afterwards, and those records still
differentiate perfectly well — so without the age check the last figure before
the outage would sit there looking like a live measurement.

## Energy Dashboard

The integration writes hour-aligned energy totals into Home Assistant's
long-term statistics, computed from the cumulative meter register. This gives
the Energy dashboard buckets aligned to real clock hours rather than to
whenever Home Assistant happened to poll.

Backfill reaches back only as far as the meter window the integration
requests (`METER_WINDOW_HOURS`, 24 hours by default) — not further. The API's
`hourly` resolution looks like it should provide deeper history but does not;
see below.

Energy used while the tracker was offline is not lost. The register is read
from the physical meter, so it keeps advancing during an outage, and the
catch-up reading afterwards carries the whole missing amount. That amount is
credited to the hour the tracker came back rather than spread across the hours
it really spans — daily and monthly totals come out right, but expect a single
tall bar at the point of recovery. Outages longer than the meter window cannot
be recovered, because both ends of the gap have to be in the same window for
the difference to be visible.

To set it up, go to **Settings → Dashboards → Energy** and add:

- **Grid consumption** → `OBI EnergyTracker Consumption`
- **Return to grid** → `OBI EnergyTracker Production`

These appear under statistics, not entities. Use them *instead of* the
`Meter Reading` sensors — adding both would count the same energy twice.

## API Details

The `/historical-data/{bridge}/{device}/{resolution}` endpoint accepts only
`hourly`, `meter`, `daily` and `monthly` as resolutions, and only `energy` and
`negative_energy` as measures. Everything else returns HTTP 400 — there is no
power measure at any resolution.

Only `meter` returns a usable series: the cumulative register at roughly
5-minute intervals with 1 Wh granularity. The other resolutions return a
**single aggregate record** covering the whole requested window, regardless of
how long that window is, labelled with one bucket start. So `hourly` with a
7-day window yields one record, not 168 — which is why hourly statistics are
derived from the meter register instead.

`scripts/probe_api.py` re-checks all of this against your own account.

---

*Disclaimer: This integration is not affiliated with or endorsed by OBI. Use at your own risk.*
