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
| `redistributed` | Rewritten around a meter dump, see below |
| `above_dc_rating` | Average power exceeds installed DC capacity, and no reference day was available to correct it |
| `missing` | No reading. Values stay `NaN` |

Gaps are never filled with zero, because a dead inverter and a night-time zero are not the same
reading. Pandas sums skip `NaN` and Excel ignores blank cells, so totals are unaffected.

## Meter dumps

Now and then the meter under-reports for a stretch and then settles up, booking the backlog into
a single slot. When that slot exceeds what the array can physically produce it is wrong beyond
argument, and the intervals it borrowed from give it away: they read far below what the same
clock time delivers on nearby days.

The correction grows a window outwards from the dump while that holds, stopping at the first
interval that reports its expected share or more. Every meter in the window is then rewritten
with the shape of the nearest fully reported days, scaled so the window keeps exactly the energy
it reported. **Daily and annual totals do not move. Only the sub-hourly profile does.**

Production, Consumption and SelfConsumption are reshaped on their own reference shapes, since a
load profile looks nothing like a solar one. FeedIn and Purchased are derived from them, which is
what keeps both meter identities exact on the corrected intervals.

The reference is built in shares of a daily total rather than in kWh, so it follows the weather:
an overcast day is measured against the *shape* of the clear days around it, not their level.
Without that, every cloudy afternoon would look under-reported.

Two limits worth knowing:

- A catch-up burst that stays **under** the nameplate rating is never detected. Apr 22 2026 has
  one at 17:15 (28.6 kWh where ~16 was due) that goes uncorrected, because no physical bound is
  violated. The flag is deliberately anchored on what is provably impossible.
- Where the energy belongs is only well evidenced for the meter that shows the deficit. On
  2026-09-08 production collapses to near zero after the dump, which pins it down; consumption
  reports plausible values throughout, so spreading its share over the same window is an
  assumption, not a measurement. The log makes it auditable.

## Output

Written to `data/`, named `solaredge_<year>_<kWp>kWp_*`:

| File | Purpose |
| --- | --- |
| `_15min.parquet` | Canonical. Keeps the UTC timezone and float dtypes. |
| `_15min.csv` | Excel-friendly: `;` separator, `,` decimals, UTC as ISO-8601 text. |
| `_15min.xlsx` | Same content. Skipped with a warning if OneDrive holds the file open. |
| `_compact.csv` | Production only, non-zero intervals plus one zero row either side of each block. |
| `_corrections.csv` | Audit log of every value a meter-dump correction replaced. Written only when there was one. |

Columns: `datetime_utc` (index), `datetime_local`, `utc_offset`, the five meters in kWh,
`Production_kW`, and `quality`.

The corrections log carries one row per rewritten interval: `window` (which dump it belongs to),
`role` (`dump` or `recovery`), and `<meter>_original_kWh`, `<meter>_corrected_kWh`,
`<meter>_delta_kWh` for all five meters. Summing `_original_kWh` and `_corrected_kWh` per window
is the check that the correction moved energy without creating it.

Each run also prints a data-quality table, what each dump did and how far its correction reached,
the two meter identity checks (`Production = SelfConsumption + FeedIn` and
`Consumption = SelfConsumption + Purchased`) and an annual summary with specific yield and peak
power.

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
