# solaredge

Retrieve 15-minute SolarEdge meter data for whole calendar years via the monitoring API.

One script, [solaredge_retrieve.py](solaredge_retrieve.py). It pulls Production, Consumption,
SelfConsumption, FeedIn (grid export) and Purchased (grid import) from the `energyDetails`
endpoint, one call per month, and writes a year at a time.

## Setup

Credentials go in `.env` next to the script (see [.env.example](.env.example)):

```ini
SOLAREDGE_API_KEY=...
SOLAREDGE_SITE_ID=...
```

Requires pandas, requests, python-dotenv, rich and pyarrow. `openpyxl` is optional and only
needed for the `.xlsx` output.

## Usage

```bash
python solaredge_retrieve.py                  # current year
python solaredge_retrieve.py --years 2025
python solaredge_retrieve.py --years 2019-2025
python solaredge_retrieve.py --years 2023 2024 --refresh
```

| Flag | Effect |
| --- | --- |
| `--years` | One year, several years, or a `2019-2025` range. Defaults to the current year. |
| `--refresh` | Re-download months already held in the cache. |
| `--no-cache` | Neither read nor write the raw JSON cache. |
| `--no-color` | Plain output, no ANSI escapes. |

Site name, peak power, country and timezone are read from the API, so nothing about the
installation is hardcoded. Months before commissioning and months in the future are never
requested, and every raw response is cached under `data/.raw_cache/`, which keeps repeat runs
well inside the SolarEdge limit of 300 API calls per day.

## Timestamps

This is the part worth understanding, because SolarEdge reports a naive local clock and always
emits 96 slots per day. That makes its output ambiguous twice a year.

| Transition | What the API returns | What the script does |
| --- | --- | --- |
| Spring forward | Four `null` slots for an hour that never happened locally | Drops them |
| Fall back | The repeated hour **summed into a single slot** | Splits it 50/50 across both occurrences and flags them |

The stored index is therefore tz-aware UTC and gapless: exactly 35040 quarter hours in a normal
year, every step 15 minutes, no duplicates. Local time travels alongside as `datetime_local`
plus `utc_offset`, which is what tells the two 02:00 readings apart on the fall-back night.

A 50/50 split preserves the annual total exactly but approximates the shape within that one
hour. The eight affected rows carry `dst_ambiguous_split` in the `quality` column, so they can
be dropped or reweighted downstream. If the true sub-hourly shape ever matters more than the
total, that is the knob to revisit.

## Data quality

Every row carries a `quality` flag.

| Flag | Meaning |
| --- | --- |
| `ok` | Reported normally |
| `dst_ambiguous_split` | Half of a fall-back interval that the API summed |
| `above_dc_rating` | Average power exceeds installed DC capacity, so physically impossible |
| `missing` | No reading. Values stay `NaN` |

Gaps are never filled with zero, because a dead inverter and a night-time zero are not the same
reading. Pandas sums skip `NaN` and Excel ignores blank cells, so totals are unaffected.

`above_dc_rating` catches the inverter booking a communication backlog into a single slot. On
this site that is 2 intervals across 2019, 2025 and 2026, carrying about 69 kWh of production
the array cannot physically make. Both clean years peak around 115 to 122 kW against a 155.5 kW
nameplate, so the threshold has room and does not fire on real summer peaks. Flagged values are
left exactly as reported: deciding what the true profile was is an analyst's call, not the
script's. The annual summary reports peak power over unflagged intervals only.

## Output

Written to `data/`, named `solaredge_<year>_<kWp>kWp_*`:

| File | Purpose |
| --- | --- |
| `_15min.parquet` | Canonical. Keeps the UTC timezone and float dtypes. |
| `_15min.csv` | Excel-friendly: `;` separator, `,` decimals, UTC as ISO-8601 text. |
| `_15min.xlsx` | Same content. Skipped with a warning if OneDrive holds the file open. |
| `_compact.csv` | Production only, non-zero intervals plus one zero row either side of each block. |

Columns: `datetime_utc` (index), `datetime_local`, `utc_offset`, the five meters in kWh,
`Production_kW`, and `quality`.

Each run also prints a data-quality table, the two meter identity checks
(`Production = SelfConsumption + FeedIn` and `Consumption = SelfConsumption + Purchased`)
and an annual summary with specific yield and peak power.

## Tests

```bash
python -m pytest tests/ -q
```

48 tests, all offline against synthetic payloads. The daylight-saving cases are covered
explicitly, including energy conservation across the fall-back split.

## History

Earlier versions used the `solaredge` package, dropped after
[EnergieID/solaredge#1](https://github.com/EnergieID/solaredge/issues/1). The original
inspiration was [sfirke/solaredge](https://github.com/sfirke/solaredge/blob/main/solaredge_retrieval.py).

Output before 2026-09-22 used a naive local-time index. Annual totals in those files are
correct, but timestamps drift an hour against UTC for half the year, and the spring-forward
hour was zero-filled. Re-run any year you intend to join against market data.
