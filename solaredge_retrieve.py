#!/usr/bin/env python3
"""
SolarEdge API retrieval: all meters, 15-min resolution, whole calendar years.

Uses the energyDetails endpoint to get Production, Consumption, SelfConsumption,
FeedIn (grid export) and Purchased (grid import) in one call per month.

The site timezone and peak power are read from the site details endpoint, so
nothing about the installation is hardcoded.

Timestamps
----------
SolarEdge reports a naive local clock and always emits 96 slots per day, which
makes its output ambiguous twice a year:

  * Spring forward: the hour that does not exist locally comes back as `null`.
    Those slots are dropped.
  * Fall back: the hour that happens twice is returned **summed into a single
    slot**. Each such slot is split 50/50 over the two real occurrences, so the
    annual total is preserved. Those rows are marked `dst_ambiguous_split` in
    the `quality` column.

The canonical index is therefore tz-aware UTC. Local time is carried alongside
as a convenience column for Excel.

Usage:
    python solaredge_retrieve.py                      # current year
    python solaredge_retrieve.py --years 2025
    python solaredge_retrieve.py --years 2019-2025
    python solaredge_retrieve.py --years 2023 2024 --refresh
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

# ── Configuration ────────────────────────────────────────────────────────────

BASE_URL = "https://monitoringapi.solaredge.com"

METERS = ("Production", "Consumption", "SelfConsumption", "FeedIn", "Purchased")

#: Canonical column order for output, independent of the order the API replies in.
METER_COLUMNS = tuple(f"{m}_kWh" for m in METERS)

#: Intervals per hour at the resolution we request.
INTERVALS_PER_HOUR = 4
TIME_UNIT = "QUARTER_OF_AN_HOUR"
FREQ = "15min"

#: Decimals kept in the stored energy values. Wh/1000 is exact to 3 decimals;
#: halving an odd Wh value at the ambiguous hour needs a 4th.
ENERGY_DECIMALS = 4
POWER_DECIMALS = 3

#: SolarEdge rejects QUARTER_OF_AN_HOUR windows longer than one month.
MAX_RETRIES = 4
REQUEST_TIMEOUT = 30

#: Meter identities never close exactly: the meters round independently every
#: interval. Judged on the annual residual as a share of the reference total,
#: which is what would actually move if a meter were reconfigured.
METER_IDENTITY_TOLERANCE = 0.005  # 0.5 %

QUALITY_OK = "ok"
QUALITY_SPLIT = "dst_ambiguous_split"
QUALITY_MISSING = "missing"
QUALITY_IMPLAUSIBLE = "above_dc_rating"

QUALITY_FLAGS = (QUALITY_OK, QUALITY_SPLIT, QUALITY_IMPLAUSIBLE, QUALITY_MISSING)

QUALITY_STYLES = {
    QUALITY_OK: "green",
    QUALITY_SPLIT: "yellow",
    QUALITY_IMPLAUSIBLE: "red",
    QUALITY_MISSING: "red",
}

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
CACHE_DIR = DATA_DIR / ".raw_cache"


# ── Formatting helpers ───────────────────────────────────────────────────────


def fmt_number(value: float, decimals: int = 0) -> str:
    """House number style: plain below 5 digits, dot thousands separator from 10000."""
    text = f"{value:,.{decimals}f}"
    if abs(value) >= 10000:
        # ',' groups, '.' decimals -> '.' groups, ',' decimals
        return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    return text.replace(",", "")


def redact(text: str, *secrets: str | None) -> str:
    """Strip API keys out of anything that might reach a log or a traceback."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


# ── API client ───────────────────────────────────────────────────────────────


class SolarEdgeError(RuntimeError):
    """Raised for any SolarEdge API failure, with credentials redacted."""


@dataclass(frozen=True)
class SiteInfo:
    """The parts of the site details response this script depends on."""

    site_id: str
    name: str
    peak_power_kwp: float
    timezone: str
    country: str
    installation_date: date | None

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class SolarEdgeClient:
    """Thin SolarEdge monitoring API client with retries and an on-disk cache."""

    def __init__(
        self,
        api_key: str,
        site_id: str,
        cache_dir: Path | None = None,
        console: Console | None = None,
    ) -> None:
        self._api_key = api_key
        self.site_id = site_id
        self.cache_dir = cache_dir
        self._console = console or Console(quiet=True)
        self._session = requests.Session()

    # -- low level ----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str]) -> dict:
        """GET with backoff on rate limits and server errors. Never leaks the key."""
        url = f"{BASE_URL}{path}"
        full_params = {"api_key": self._api_key, **params}
        last_error = ""

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self._session.get(url, params=full_params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = redact(f"{type(exc).__name__}: {exc}", self._api_key)
                self._backoff(attempt, last_error)
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise SolarEdgeError(
                        redact(f"{url} returned invalid JSON: {exc}", self._api_key)
                    ) from None

            body = redact(response.text[:200], self._api_key)

            if response.status_code == 429:
                last_error = f"rate limited (429): {body}"
                self._backoff(attempt, last_error)
                continue

            if response.status_code >= 500:
                last_error = f"server error {response.status_code}: {body}"
                self._backoff(attempt, last_error)
                continue

            # 4xx other than 429: retrying cannot help.
            raise SolarEdgeError(
                f"{path} failed with HTTP {response.status_code}: {body}"
            )

        raise SolarEdgeError(f"{path} failed after {MAX_RETRIES} attempts. Last error: {last_error}")

    def _backoff(self, attempt: int, reason: str) -> None:
        if attempt >= MAX_RETRIES:
            return
        wait = 2**attempt
        self._console.print(f"  [yellow]{reason}. Retrying in {wait}s ({attempt}/{MAX_RETRIES})[/]")
        # Imported lazily so the tests can run without sleeping.
        import time

        time.sleep(wait)

    # -- endpoints ----------------------------------------------------------

    def site_info(self) -> SiteInfo:
        details = self._get(f"/site/{self.site_id}/details.json", {}).get("details", {})
        if not details:
            raise SolarEdgeError(f"Site {self.site_id} returned no details. Check the site ID.")

        location = details.get("location", {})
        timezone = location.get("timeZone")
        if not timezone:
            raise SolarEdgeError(
                f"Site {self.site_id} details carry no timeZone, so timestamps cannot be anchored."
            )

        raw_install = details.get("installationDate")
        installation = (
            datetime.strptime(raw_install, "%Y-%m-%d").date() if raw_install else None
        )

        return SiteInfo(
            site_id=str(self.site_id),
            name=details.get("name", "unknown"),
            peak_power_kwp=float(details.get("peakPower", 0.0)),
            timezone=timezone,
            country=location.get("country", "unknown"),
            installation_date=installation,
        )

    def energy_details(self, start: date, end: date, refresh: bool = False) -> dict:
        """One energyDetails window, served from the cache when already downloaded."""
        cache_path = self._cache_path(start, end)

        if cache_path is not None and cache_path.exists() and not refresh:
            return json.loads(cache_path.read_text())

        payload = self._get(
            f"/site/{self.site_id}/energyDetails.json",
            {
                "timeUnit": TIME_UNIT,
                "startTime": f"{start:%Y-%m-%d} 00:00:00",
                "endTime": f"{end:%Y-%m-%d} 23:59:59",
                "meters": ",".join(METERS),
            },
        )

        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(payload))

        return payload

    def _cache_path(self, start: date, end: date) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / str(start.year) / f"{self.site_id}_{start:%Y-%m-%d}_{end:%Y-%m-%d}.json"


# ── Parsing ──────────────────────────────────────────────────────────────────


def parse_meter_response(payload: dict) -> pd.DataFrame:
    """
    Turn one energyDetails payload into a frame indexed by naive local time.

    Values arrive in Wh and are converted to kWh. A missing or null value stays
    NaN: "the inverter reported nothing" is not the same as "it produced zero".
    """
    meters = payload.get("energyDetails", {}).get("meters", [])
    series: dict[str, pd.Series] = {}

    for meter in meters:
        column = f"{meter['type']}_kWh"
        values = meter.get("values", [])
        if not values:
            continue

        index = pd.to_datetime([entry["date"] for entry in values])
        raw = pd.Series(
            [entry.get("value") for entry in values], index=index, dtype="float64"
        )
        if not raw.index.is_unique:
            duplicates = raw.index[raw.index.duplicated()].unique()
            raise SolarEdgeError(
                f"{column} returned duplicate timestamps, e.g. {list(duplicates[:3])}. "
                "The API contract changed; splitting the ambiguous hour is no longer safe."
            )
        series[column] = (raw / 1000).round(ENERGY_DECIMALS)

    if not series:
        return pd.DataFrame()

    frame = pd.DataFrame(series).sort_index()
    frame.index.name = "datetime_local"
    return frame


# ── Timezone handling ────────────────────────────────────────────────────────


def split_dst_masks(index: pd.DatetimeIndex, tz: str) -> tuple[pd.Series, pd.Series]:
    """
    Classify a naive local index into the two DST trouble spots.

    Returns boolean masks for (nonexistent, ambiguous) local times. They are
    disjoint: a timestamp is skipped by the clock, repeated by it, or neither.
    """
    nonexistent = pd.Series(
        index.tz_localize(tz, ambiguous=True, nonexistent="NaT").isna(), index=index
    )
    ambiguous = pd.Series(
        index.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward").isna(),
        index=index,
    )
    return nonexistent, ambiguous


def localize_to_utc(df: pd.DataFrame, tz: str, console: Console) -> pd.DataFrame:
    """
    Move a naive local-clock frame onto a tz-aware UTC index.

    Nonexistent local times are dropped. Ambiguous ones hold the sum of both
    occurrences, so each is split 50/50 across them and flagged in `quality`.
    """
    if df.empty:
        return df.assign(quality=pd.Series(dtype="object"))

    nonexistent, ambiguous = split_dst_masks(pd.DatetimeIndex(df.index), tz)
    meter_columns = [c for c in df.columns if c in METER_COLUMNS]

    if nonexistent.any():
        skipped = df.loc[nonexistent.to_numpy()]
        carried = skipped[meter_columns].notna().to_numpy().sum()
        if carried:
            console.print(
                f"  [red]{carried} value(s) reported at local times that the clock skips. "
                "Dropping them; check the site timezone.[/]"
            )
        df = df.loc[~nonexistent.to_numpy()]
        ambiguous = ambiguous.loc[~nonexistent.to_numpy()]

    plain = df.loc[~ambiguous.to_numpy()].copy()
    plain.index = pd.DatetimeIndex(plain.index).tz_localize(
        tz, ambiguous=True, nonexistent="raise"
    )
    plain["quality"] = QUALITY_OK

    repeated = df.loc[ambiguous.to_numpy()]
    if repeated.empty:
        combined = plain
    else:
        halved = repeated.copy()
        halved[meter_columns] = (halved[meter_columns] / 2).round(ENERGY_DECIMALS)

        first = halved.copy()  # daylight-saving occurrence
        first.index = pd.DatetimeIndex(repeated.index).tz_localize(tz, ambiguous=True)
        second = halved.copy()  # standard-time occurrence
        second.index = pd.DatetimeIndex(repeated.index).tz_localize(tz, ambiguous=False)

        split = pd.concat([first, second])
        split["quality"] = QUALITY_SPLIT
        combined = pd.concat([plain, split])

        console.print(
            f"  [yellow]{len(repeated)} interval(s) at the repeated hour split 50/50 "
            "across both occurrences.[/]"
        )

    combined.index = combined.index.tz_convert("UTC")
    combined.index.name = "datetime_utc"
    return combined.sort_index()


def year_bounds_utc(year: int, tz: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """UTC instants of local midnight on 1 January of `year` and of the next year."""
    start = pd.Timestamp(f"{year}-01-01", tz=tz).tz_convert("UTC")
    end = pd.Timestamp(f"{year + 1}-01-01", tz=tz).tz_convert("UTC")
    return start, end


# ── Retrieval ────────────────────────────────────────────────────────────────


def month_windows(year: int, site: SiteInfo, today: date | None = None) -> list[tuple[date, date]]:
    """
    Calendar-month windows for `year`, clipped to the site's lifetime.

    Months before commissioning and months in the future are never requested:
    the SolarEdge account is capped at 300 calls per day.
    """
    today = today or date.today()
    windows: list[tuple[date, date]] = []

    for month_start in pd.date_range(f"{year}-01-01", periods=12, freq="MS"):
        start = month_start.date()
        end = (month_start + pd.offsets.MonthEnd(1)).date()

        if site.installation_date and end < site.installation_date:
            continue
        if start > today:
            continue

        windows.append((max(start, site.installation_date or start), min(end, today)))

    return windows


def retrieve_year(
    client: SolarEdgeClient,
    site: SiteInfo,
    year: int,
    console: Console,
    refresh: bool = False,
) -> pd.DataFrame:
    """Retrieve a calendar year in monthly chunks and return it on a UTC index."""
    windows = month_windows(year, site)
    if not windows:
        return pd.DataFrame()

    chunks: list[pd.DataFrame] = []
    progress_columns = (
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
    )

    with Progress(*progress_columns, console=console, transient=True) as progress:
        task = progress.add_task(f"Downloading {year}", total=len(windows))
        for start, end in windows:
            payload = client.energy_details(start, end, refresh=refresh)
            chunk = parse_meter_response(payload)
            if not chunk.empty:
                chunks.append(chunk)
            progress.advance(task)

    if not chunks:
        return pd.DataFrame()

    raw = pd.concat(chunks).sort_index()
    if raw.index.has_duplicates:
        raw = raw[~raw.index.duplicated(keep="first")]

    return localize_to_utc(raw, site.timezone, console)


# ── Shaping ──────────────────────────────────────────────────────────────────


def build_full_timeseries(df: pd.DataFrame, year: int, tz: str) -> pd.DataFrame:
    """
    Reindex onto the complete 15-minute UTC grid covering the local calendar year.

    Gaps stay NaN rather than becoming zero, and are labelled in `quality`, so a
    dead inverter is never mistaken for a night-time zero.
    """
    start, end = year_bounds_utc(year, tz)
    grid = pd.date_range(start, end, freq=FREQ, inclusive="left", name="datetime_utc")

    out = df.reindex(grid)
    for column in METER_COLUMNS:
        if column not in out.columns:
            out[column] = float("nan")
        out[column] = out[column].astype("float64")

    blank = out[list(METER_COLUMNS)].isna().all(axis=1)
    out["quality"] = (
        out["quality"].astype("object").where(~blank, QUALITY_MISSING).fillna(QUALITY_MISSING)
    )

    return out[[*METER_COLUMNS, "quality"]]


def local_time_columns(index: pd.DatetimeIndex, tz: str) -> tuple[list[str], list[str]]:
    """Local wall-clock text and the UTC offset that disambiguates it."""
    local = index.tz_convert(tz)
    stamps = local.strftime("%Y-%m-%d %H:%M:%S").tolist()
    offsets = [f"{raw[:3]}:{raw[3:]}" for raw in local.strftime("%z")]
    return stamps, offsets


def add_derived_columns(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    """Add average power over each interval and the local clock reading."""
    out = df.copy()
    out["Production_kW"] = (out["Production_kWh"] * INTERVALS_PER_HOUR).round(POWER_DECIMALS)

    stamps, offsets = local_time_columns(out.index, tz)
    out.insert(0, "datetime_local", stamps)
    out.insert(1, "utc_offset", offsets)

    return out


# ── Validation ───────────────────────────────────────────────────────────────


def flag_implausible_production(df: pd.DataFrame, site: SiteInfo) -> pd.DataFrame:
    """
    Mark intervals whose average power exceeds the DC nameplate rating.

    A 15-minute average above installed DC capacity cannot happen physically. In
    practice it is the inverter booking a communication backlog into one slot.
    The values are left untouched and only labelled, because deciding what the
    real profile was is the analyst's call, not this script's.
    """
    if site.peak_power_kwp <= 0:
        return df

    out = df.copy()
    over = (out["Production_kW"] > site.peak_power_kwp) & (out["quality"] == QUALITY_OK)
    out.loc[over, "quality"] = QUALITY_IMPLAUSIBLE
    return out


def check_completeness(df: pd.DataFrame, site: SiteInfo, console: Console) -> None:
    counts = df["quality"].value_counts()
    total = len(df)
    present = total - int(counts.get(QUALITY_MISSING, 0))

    table = Table(title="Data quality", title_justify="left", header_style="bold")
    table.add_column("Flag")
    table.add_column("Intervals", justify="right")
    table.add_column("Share", justify="right")

    for flag in QUALITY_FLAGS:
        count = int(counts.get(flag, 0))
        if count == 0 and flag != QUALITY_OK:
            continue
        table.add_row(
            f"[{QUALITY_STYLES[flag]}]{flag}[/]",
            fmt_number(count),
            f"{count / total * 100:.2f} %",
        )

    console.print(table)
    console.print(
        f"Coverage: {fmt_number(present)} / {fmt_number(total)} intervals "
        f"({present / total * 100:.2f} %)"
    )

    missing = df.index[df["quality"] == QUALITY_MISSING]
    if len(missing):
        local_months = missing.tz_convert(site.timezone).tz_localize(None).to_period("M")
        console.print(f"[red]Months with gaps:[/] {', '.join(sorted({str(m) for m in local_months}))}")

    spikes = df[df["quality"] == QUALITY_IMPLAUSIBLE]
    if not spikes.empty:
        phantom = (spikes["Production_kWh"] - site.peak_power_kwp / INTERVALS_PER_HOUR).sum()
        console.print(
            f"[red]{len(spikes)} interval(s) above the {site.peak_power_kwp:g} kW DC rating[/], "
            f"carrying roughly {phantom:.0f} kWh more than the array can physically make:"
        )
        for stamp, row in spikes.iterrows():
            console.print(
                f"    {row['datetime_local']} {row['utc_offset']}  "
                f"{row['Production_kW']:.0f} kW  ({row['Production_kWh']:.1f} kWh)"
            )
        console.print(
            "  [dim]Values left as reported. Filter on quality to exclude them.[/]"
        )


def check_meter_identities(df: pd.DataFrame, console: Console) -> None:
    """
    Two identities must hold on every interval:
        Production  = SelfConsumption + FeedIn
        Consumption = SelfConsumption + Purchased
    A drift here means the meters are misconfigured, not that the code is wrong.
    """
    identities = {
        "Production - (SelfConsumption + FeedIn)": (
            df["Production_kWh"] - df["SelfConsumption_kWh"] - df["FeedIn_kWh"],
            "Production_kWh",
        ),
        "Consumption - (SelfConsumption + Purchased)": (
            df["Consumption_kWh"] - df["SelfConsumption_kWh"] - df["Purchased_kWh"],
            "Consumption_kWh",
        ),
    }

    table = Table(title="Meter identities", title_justify="left", header_style="bold")
    table.add_column("Residual")
    table.add_column("Annual (kWh)", justify="right")
    table.add_column("Of total", justify="right")
    table.add_column("Verdict", justify="right")

    for label, (residual, reference) in identities.items():
        annual = float(residual.sum(skipna=True))
        total = float(df[reference].sum(skipna=True))
        share = abs(annual) / total if total else 0.0
        ok = share <= METER_IDENTITY_TOLERANCE
        table.add_row(
            label,
            fmt_number(annual, 1),
            f"{share * 100:.2f} %",
            "[green]consistent[/]" if ok else "[yellow]drifting[/]",
        )

    console.print(table)
    if any(
        abs(float(r.sum(skipna=True))) / (float(df[ref].sum(skipna=True)) or 1)
        > METER_IDENTITY_TOLERANCE
        for r, ref in identities.values()
    ):
        console.print(
            "[yellow]A residual above "
            f"{METER_IDENTITY_TOLERANCE * 100:g} % points at a meter configuration "
            "problem on the site, not at this script.[/]"
        )


def summarise(df: pd.DataFrame, site: SiteInfo, year: int, console: Console) -> None:
    totals = {column: float(df[column].sum(skipna=True)) for column in METER_COLUMNS}
    production = totals["Production_kWh"]

    table = Table(title=f"Annual summary {year}", title_justify="left", header_style="bold")
    table.add_column("Quantity")
    table.add_column("kWh", justify="right")

    labels = {
        "Production_kWh": "PV production",
        "Consumption_kWh": "Consumption",
        "SelfConsumption_kWh": "Self-consumption",
        "FeedIn_kWh": "Grid export",
        "Purchased_kWh": "Grid import",
    }
    for column, label in labels.items():
        table.add_row(label, fmt_number(totals[column]))

    console.print(table)

    if production > 0 and site.peak_power_kwp > 0:
        console.print(
            f"Self-consumption ratio: {totals['SelfConsumption_kWh'] / production * 100:.1f} %"
        )
        console.print(
            f"Specific yield: {production / site.peak_power_kwp:.0f} kWh/kWp "
            f"({site.peak_power_kwp:g} kWp installed)"
        )
        plausible = df.loc[df["quality"] != QUALITY_IMPLAUSIBLE, "Production_kW"]
        peak = float(plausible.max(skipna=True))
        excluded = int((df["quality"] == QUALITY_IMPLAUSIBLE).sum())
        suffix = f", ignoring {excluded} implausible interval(s)" if excluded else ""
        console.print(
            f"Peak 15-min power: {peak:.1f} kW "
            f"({peak / site.peak_power_kwp * 100:.0f} % of DC rating{suffix})"
        )


# ── Output ───────────────────────────────────────────────────────────────────


def output_basename(site: SiteInfo, year: int) -> str:
    return f"solaredge_{year}_{site.peak_power_kwp:g}kWp"


def build_compact(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    """
    Production-only view: every interval with output, plus one zero row either
    side of each block, so a plotted or imported profile starts and ends at zero.
    """
    production = df[["Production_kWh", "Production_kW"]]
    active = production.index[df["Production_kWh"].fillna(0) > 0]
    if len(active) == 0:
        return production.iloc[:0].copy()

    step = pd.Timedelta(FREQ)
    keep = active.union(active - step).union(active + step)
    keep = keep[(keep >= production.index.min()) & (keep <= production.index.max())]

    out = production.reindex(keep.sort_values()).fillna(0.0)
    stamps, offsets = local_time_columns(out.index, tz)
    out.insert(0, "datetime_local", stamps)
    out.insert(1, "utc_offset", offsets)
    return out


def save_outputs(
    df: pd.DataFrame, site: SiteInfo, year: int, console: Console
) -> list[Path]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    base = output_basename(site, year)
    written: list[Path] = []

    def note(path: Path, rows: int) -> None:
        written.append(path)
        console.print(f"  [green]saved[/] {path.name} ({fmt_number(rows)} rows)")

    # Parquet is canonical: it keeps the UTC timezone and the float dtypes.
    parquet_path = DATA_DIR / f"{base}_15min.parquet"
    df.to_parquet(parquet_path)
    note(parquet_path, len(df))

    # CSV for Excel: ISO-8601 UTC as text, semicolon separator, comma decimals.
    csv_frame = df.copy()
    csv_frame.index = csv_frame.index.strftime("%Y-%m-%dT%H:%M:%SZ")
    csv_path = DATA_DIR / f"{base}_15min.csv"
    csv_frame.to_csv(csv_path, sep=";", decimal=",")
    note(csv_path, len(csv_frame))

    excel_path = DATA_DIR / f"{base}_15min.xlsx"
    try:
        csv_frame.to_excel(excel_path)
        note(excel_path, len(csv_frame))
    except Exception as exc:  # OneDrive holds a lock on open workbooks
        console.print(f"  [yellow]Excel write skipped ({exc}). CSV and parquet are current.[/]")

    compact = build_compact(df, site.timezone)
    compact_frame = compact.copy()
    compact_frame.index = compact_frame.index.strftime("%Y-%m-%dT%H:%M:%SZ")
    compact_path = DATA_DIR / f"{base}_compact.csv"
    compact_frame.to_csv(compact_path, sep=";", decimal=",")
    note(compact_path, len(compact_frame))

    return written


# ── Entry point ──────────────────────────────────────────────────────────────


def parse_years(tokens: list[str]) -> list[int]:
    """Accept `2025`, `2023 2024`, and `2019-2025`."""
    years: set[int] = set()
    for token in tokens:
        if "-" in token:
            first, last = token.split("-", 1)
            years.update(range(int(first), int(last) + 1))
        else:
            years.add(int(token))
    return sorted(years)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--years",
        nargs="+",
        default=[str(date.today().year)],
        metavar="YEAR",
        help="Years to retrieve: 2025, '2023 2024', or a 2019-2025 range (default: current year)",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Re-download months already in the cache",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the raw JSON cache",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable coloured output")
    return parser


def make_console(no_color: bool) -> Console:
    return Console(
        no_color=no_color,
        force_terminal=None if sys.stdout.isatty() else False,
        width=None if sys.stdout.isatty() else 100,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = make_console(args.no_color)

    load_dotenv(Path.home() / ".env")
    load_dotenv(PROJECT_DIR / ".env", override=True)

    api_key = os.getenv("SOLAREDGE_API_KEY")
    site_id = os.getenv("SOLAREDGE_SITE_ID")
    if not api_key or not site_id:
        console.print(
            "[red]Missing credentials.[/] Set SOLAREDGE_API_KEY and SOLAREDGE_SITE_ID "
            "in .env (see .env.example)."
        )
        return 1

    client = SolarEdgeClient(
        api_key=api_key,
        site_id=site_id,
        cache_dir=None if args.no_cache else CACHE_DIR,
        console=console,
    )

    try:
        site = client.site_info()
    except SolarEdgeError as exc:
        console.print(f"[red]Could not read site details:[/] {exc}")
        return 1

    console.print(
        Panel.fit(
            f"[bold]{site.name}[/] (site {site.site_id})\n"
            f"{site.peak_power_kwp:g} kWp DC, {site.country}, {site.timezone}\n"
            f"Commissioned {site.installation_date or 'unknown'}",
            title="SolarEdge",
            border_style="blue",
        )
    )

    years = parse_years(args.years)
    failures = 0

    for year in years:
        console.rule(f"[bold]{year}")
        try:
            raw = retrieve_year(client, site, year, console, refresh=args.refresh)
        except SolarEdgeError as exc:
            console.print(f"[red]{year} failed:[/] {exc}")
            failures += 1
            continue

        if raw.empty:
            console.print(f"[yellow]No data returned for {year}. Skipping.[/]")
            continue

        df = build_full_timeseries(raw, year, site.timezone)
        df = add_derived_columns(df, site.timezone)
        df = flag_implausible_production(df, site)

        check_completeness(df, site, console)
        check_meter_identities(df, console)
        summarise(df, site, year, console)
        save_outputs(df, site, year, console)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
