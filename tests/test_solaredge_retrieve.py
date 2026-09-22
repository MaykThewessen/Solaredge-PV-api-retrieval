"""
Tests for solaredge_retrieve.

Everything here runs offline against synthetic payloads shaped exactly like the
ones the live API returns, including the two daylight-saving edge cases that the
previous version of the script got wrong.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import solaredge_retrieve as se  # noqa: E402

TZ = "Europe/Amsterdam"
QUIET = Console(quiet=True)


# ── Fixtures ─────────────────────────────────────────────────────────────────


def make_payload(values: dict[str, list[tuple[str, float | None]]]) -> dict:
    """Build an energyDetails payload from {meter: [(local timestamp, Wh)]}."""
    return {
        "energyDetails": {
            "timeUnit": "QUARTER_OF_AN_HOUR",
            "unit": "Wh",
            "meters": [
                {
                    "type": meter,
                    "values": [
                        {"date": stamp} if wh is None else {"date": stamp, "value": wh}
                        for stamp, wh in rows
                    ],
                }
                for meter, rows in values.items()
            ],
        }
    }


def naive_local_day(day: str) -> pd.DatetimeIndex:
    """The 96-slot naive local grid SolarEdge returns for any day, DST or not."""
    return pd.date_range(f"{day} 00:00", periods=96, freq="15min")


@pytest.fixture
def site() -> se.SiteInfo:
    return se.SiteInfo(
        site_id="123",
        name="Test site",
        peak_power_kwp=155.5,
        timezone=TZ,
        country="Netherlands",
        installation_date=date(2019, 3, 21),
    )


# ── Parsing ──────────────────────────────────────────────────────────────────


def test_parse_converts_wh_to_kwh():
    payload = make_payload({"Production": [("2025-06-01 12:00:00", 12594.0)]})
    df = se.parse_meter_response(payload)
    assert df.loc[pd.Timestamp("2025-06-01 12:00"), "Production_kWh"] == pytest.approx(12.594)


def test_parse_keeps_missing_values_as_nan_not_zero():
    """A null reading means the inverter said nothing. That is not a zero."""
    payload = make_payload(
        {"Production": [("2025-06-01 12:00:00", None), ("2025-06-01 12:15:00", 1000.0)]}
    )
    df = se.parse_meter_response(payload)
    assert pd.isna(df.iloc[0]["Production_kWh"])
    assert df.iloc[1]["Production_kWh"] == pytest.approx(1.0)


def test_parse_orders_columns_and_handles_all_meters():
    payload = make_payload({m: [("2025-06-01 12:00:00", 1000.0)] for m in se.METERS})
    df = se.parse_meter_response(payload)
    assert set(df.columns) == set(se.METER_COLUMNS)


def test_parse_rejects_duplicate_timestamps():
    """Silently collapsing duplicates is what hid the DST bug before."""
    payload = make_payload(
        {"Production": [("2025-06-01 12:00:00", 1.0), ("2025-06-01 12:00:00", 2.0)]}
    )
    with pytest.raises(se.SolarEdgeError, match="duplicate timestamps"):
        se.parse_meter_response(payload)


def test_parse_empty_payload_returns_empty_frame():
    assert se.parse_meter_response({"energyDetails": {"meters": []}}).empty


# ── DST classification ───────────────────────────────────────────────────────


def test_spring_forward_hour_is_classified_nonexistent():
    nonexistent, ambiguous = se.split_dst_masks(naive_local_day("2025-03-30"), TZ)
    assert nonexistent.sum() == 4
    assert not ambiguous.any()
    assert list(nonexistent[nonexistent].index.hour) == [2, 2, 2, 2]


def test_fall_back_hour_is_classified_ambiguous():
    nonexistent, ambiguous = se.split_dst_masks(naive_local_day("2025-10-26"), TZ)
    assert ambiguous.sum() == 4
    assert not nonexistent.any()
    assert list(ambiguous[ambiguous].index.hour) == [2, 2, 2, 2]


def test_ordinary_day_has_neither():
    nonexistent, ambiguous = se.split_dst_masks(naive_local_day("2025-06-01"), TZ)
    assert not nonexistent.any()
    assert not ambiguous.any()


# ── Localization ─────────────────────────────────────────────────────────────


def test_nonexistent_hour_is_dropped_not_zero_filled():
    """The old script invented four zero rows for an hour that never happened."""
    index = naive_local_day("2025-03-30")
    df = pd.DataFrame({"Production_kWh": 1.0}, index=index)
    df.loc[df.index.hour == 2, "Production_kWh"] = float("nan")

    out = se.localize_to_utc(df, TZ, QUIET)

    assert len(out) == 92
    assert str(out.index.tz) == "UTC"
    local_hours = out.index.tz_convert(TZ).hour
    assert 2 not in set(local_hours)


def test_ambiguous_hour_is_split_across_both_occurrences():
    index = naive_local_day("2025-10-26")
    df = pd.DataFrame({"Production_kWh": 1.0}, index=index)
    df.loc[df.index.hour == 2, "Production_kWh"] = 10.0  # API sums the two hours

    out = se.localize_to_utc(df, TZ, QUIET)

    assert len(out) == 100  # 96 slots, four of them doubled
    split = out[out["quality"] == se.QUALITY_SPLIT]
    assert len(split) == 8
    assert (split["Production_kWh"] == 5.0).all()


def test_ambiguous_split_conserves_energy():
    index = naive_local_day("2025-10-26")
    df = pd.DataFrame({"Production_kWh": 1.0}, index=index)
    df.loc[df.index.hour == 2, "Production_kWh"] = 10.0

    out = se.localize_to_utc(df, TZ, QUIET)

    assert out["Production_kWh"].sum() == pytest.approx(df["Production_kWh"].sum())


def test_localized_index_is_strictly_increasing_and_unique():
    frames = [
        pd.DataFrame({"Production_kWh": 1.0}, index=naive_local_day(day))
        for day in ("2025-03-30", "2025-10-26", "2025-06-01")
    ]
    for frame in frames:
        out = se.localize_to_utc(frame.dropna(), TZ, QUIET)
        assert out.index.is_monotonic_increasing
        assert out.index.is_unique


def test_local_noon_maps_to_the_right_utc_instant():
    """Summer is UTC+2, winter UTC+1. Getting this wrong is a silent hour of drift."""
    summer = pd.DataFrame({"Production_kWh": [1.0]}, index=[pd.Timestamp("2025-07-01 12:00")])
    winter = pd.DataFrame({"Production_kWh": [1.0]}, index=[pd.Timestamp("2025-01-01 12:00")])

    assert se.localize_to_utc(summer, TZ, QUIET).index[0] == pd.Timestamp("2025-07-01 10:00", tz="UTC")
    assert se.localize_to_utc(winter, TZ, QUIET).index[0] == pd.Timestamp("2025-01-01 11:00", tz="UTC")


# ── Year grid ────────────────────────────────────────────────────────────────


def test_year_bounds_follow_local_midnight():
    start, end = se.year_bounds_utc(2025, TZ)
    assert start == pd.Timestamp("2024-12-31 23:00", tz="UTC")
    assert end == pd.Timestamp("2025-12-31 23:00", tz="UTC")


@pytest.mark.parametrize("year,expected", [(2025, 35040), (2024, 35136)])
def test_full_grid_length_matches_calendar(year: int, expected: int):
    """DST shifts cancel over a full year, so a normal year is exactly 365 x 96."""
    empty = pd.DataFrame(
        {c: pd.Series(dtype="float64") for c in se.METER_COLUMNS},
        index=pd.DatetimeIndex([], tz="UTC"),
    ).assign(quality=pd.Series(dtype="object"))

    out = se.build_full_timeseries(empty, year, TZ)
    assert len(out) == expected


def test_gaps_are_flagged_missing_and_stay_nan():
    """Zero-filling a gap turns a dead inverter into a night-time reading."""
    index = pd.date_range(
        pd.Timestamp("2024-12-31 23:00", tz="UTC"), periods=4, freq="15min"
    )
    df = pd.DataFrame({c: 1.0 for c in se.METER_COLUMNS}, index=index).assign(
        quality=se.QUALITY_OK
    )

    out = se.build_full_timeseries(df, 2025, TZ)

    assert (out["quality"] == se.QUALITY_MISSING).sum() == 35036
    assert out["Production_kWh"].isna().sum() == 35036
    assert out["Production_kWh"].sum() == pytest.approx(4.0)


# ── Derived columns ──────────────────────────────────────────────────────────


def test_power_is_energy_times_four():
    index = pd.date_range(pd.Timestamp("2025-06-01 10:00", tz="UTC"), periods=2, freq="15min")
    df = pd.DataFrame({c: 2.5 for c in se.METER_COLUMNS}, index=index).assign(
        quality=se.QUALITY_OK
    )
    out = se.add_derived_columns(df, TZ)
    assert (out["Production_kW"] == 10.0).all()


def test_local_columns_carry_the_offset():
    index = pd.DatetimeIndex(
        [pd.Timestamp("2025-01-01 11:00", tz="UTC"), pd.Timestamp("2025-07-01 10:00", tz="UTC")]
    )
    df = pd.DataFrame({c: 1.0 for c in se.METER_COLUMNS}, index=index).assign(
        quality=se.QUALITY_OK
    )
    out = se.add_derived_columns(df, TZ)

    assert out["datetime_local"].tolist() == ["2025-01-01 12:00:00", "2025-07-01 12:00:00"]
    assert out["utc_offset"].tolist() == ["+01:00", "+02:00"]


def test_both_fall_back_occurrences_are_distinguishable():
    """Same local clock reading, different offset: this is the point of the column."""
    index = naive_local_day("2025-10-26")
    df = pd.DataFrame({c: 1.0 for c in se.METER_COLUMNS}, index=index)
    out = se.add_derived_columns(se.localize_to_utc(df, TZ, QUIET), TZ)

    two_am = out[out["datetime_local"] == "2025-10-26 02:00:00"]
    assert two_am["utc_offset"].tolist() == ["+02:00", "+01:00"]


# ── Compact output ───────────────────────────────────────────────────────────


def test_compact_pads_one_zero_row_each_side():
    index = pd.date_range(pd.Timestamp("2025-06-01 00:00", tz="UTC"), periods=10, freq="15min")
    df = pd.DataFrame({c: 0.0 for c in se.METER_COLUMNS}, index=index).assign(
        quality=se.QUALITY_OK
    )
    df.iloc[4:6, df.columns.get_loc("Production_kWh")] = 3.0
    df = se.add_derived_columns(df, TZ)

    compact = se.build_compact(df, TZ)

    assert len(compact) == 4
    assert compact["Production_kWh"].tolist() == [0.0, 3.0, 3.0, 0.0]


# ── Physical plausibility ────────────────────────────────────────────────────


def frame_with_power(kilowatts: list[float]) -> pd.DataFrame:
    index = pd.date_range(
        pd.Timestamp("2026-06-01 10:00", tz="UTC"), periods=len(kilowatts), freq="15min"
    )
    df = pd.DataFrame({c: 0.0 for c in se.METER_COLUMNS}, index=index)
    df["Production_kWh"] = [kw / se.INTERVALS_PER_HOUR for kw in kilowatts]
    df["quality"] = se.QUALITY_OK
    return se.add_derived_columns(df, TZ)


def test_power_above_dc_rating_is_flagged(site: se.SiteInfo):
    """155 kWp cannot average 390 kW over a quarter hour. The API reports it anyway."""
    df = se.flag_implausible_production(frame_with_power([110.0, 390.6, 95.0]), site)
    assert df["quality"].tolist() == [
        se.QUALITY_OK,
        se.QUALITY_IMPLAUSIBLE,
        se.QUALITY_OK,
    ]


def test_plausible_peaks_are_left_alone(site: se.SiteInfo):
    """Two clean years peak near 120 kW, so the nameplate must not flag those."""
    df = se.flag_implausible_production(frame_with_power([114.6, 121.5, 155.4]), site)
    assert (df["quality"] == se.QUALITY_OK).all()


def test_flagging_never_changes_the_values(site: se.SiteInfo):
    before = frame_with_power([110.0, 390.6])
    after = se.flag_implausible_production(before, site)
    pd.testing.assert_series_equal(before["Production_kWh"], after["Production_kWh"])


def test_flagging_does_not_overwrite_other_flags(site: se.SiteInfo):
    df = frame_with_power([390.6, 390.6])
    df.loc[df.index[0], "quality"] = se.QUALITY_SPLIT
    out = se.flag_implausible_production(df, site)
    assert out["quality"].tolist() == [se.QUALITY_SPLIT, se.QUALITY_IMPLAUSIBLE]


def test_unknown_peak_power_disables_the_check():
    unknown = se.SiteInfo("1", "x", 0.0, TZ, "NL", None)
    df = se.flag_implausible_production(frame_with_power([390.6]), unknown)
    assert (df["quality"] == se.QUALITY_OK).all()


def test_compact_is_empty_when_nothing_was_produced():
    index = pd.date_range(pd.Timestamp("2025-06-01 00:00", tz="UTC"), periods=4, freq="15min")
    df = pd.DataFrame({c: 0.0 for c in se.METER_COLUMNS}, index=index).assign(
        quality=se.QUALITY_OK
    )
    assert se.build_compact(se.add_derived_columns(df, TZ), TZ).empty


# ── Request windows ──────────────────────────────────────────────────────────


def test_months_before_commissioning_are_not_requested(site: se.SiteInfo):
    windows = se.month_windows(2019, site, today=date(2026, 1, 1))
    assert len(windows) == 10  # March to December
    assert windows[0][0] == date(2019, 3, 21)


def test_future_months_are_not_requested(site: se.SiteInfo):
    windows = se.month_windows(2026, site, today=date(2026, 3, 15))
    assert len(windows) == 3
    assert windows[-1] == (date(2026, 3, 1), date(2026, 3, 15))


def test_complete_past_year_requests_twelve_months(site: se.SiteInfo):
    windows = se.month_windows(2025, site, today=date(2026, 1, 1))
    assert len(windows) == 12
    assert windows[0] == (date(2025, 1, 1), date(2025, 1, 31))
    assert windows[-1] == (date(2025, 12, 1), date(2025, 12, 31))


def test_year_entirely_before_commissioning_is_empty(site: se.SiteInfo):
    assert se.month_windows(2015, site, today=date(2026, 1, 1)) == []


# ── CLI helpers ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tokens,expected",
    [
        (["2025"], [2025]),
        (["2023", "2024"], [2023, 2024]),
        (["2019-2022"], [2019, 2020, 2021, 2022]),
        (["2019-2021", "2025"], [2019, 2020, 2021, 2025]),
        (["2024", "2024"], [2024]),
    ],
)
def test_parse_years(tokens: list[str], expected: list[int]):
    assert se.parse_years(tokens) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(0, "0"), (999, "999"), (9999, "9999"), (10000, "10.000"), (155430, "155.430")],
)
def test_number_formatting_follows_house_style(value: float, expected: str):
    assert se.fmt_number(value) == expected


def test_api_key_is_redacted():
    message = "GET https://monitoringapi.solaredge.com/x?api_key=SECRET123 failed"
    assert "SECRET123" not in se.redact(message, "SECRET123")
    assert "***" in se.redact(message, "SECRET123")


def test_output_basename_uses_the_reported_peak_power(site: se.SiteInfo):
    assert se.output_basename(site, 2025) == "solaredge_2025_155.5kWp"


# ── Client behaviour ─────────────────────────────────────────────────────────


def test_client_errors_never_carry_the_api_key(monkeypatch):
    class Response:
        status_code = 403
        text = "Forbidden for api_key=SECRET123"

    client = se.SolarEdgeClient(api_key="SECRET123", site_id="1", cache_dir=None)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: Response())

    with pytest.raises(se.SolarEdgeError) as excinfo:
        client.site_info()

    assert "SECRET123" not in str(excinfo.value)


def test_cache_prevents_a_second_request(tmp_path: Path, monkeypatch):
    payload = make_payload({"Production": [("2025-06-01 12:00:00", 1000.0)]})
    calls = {"n": 0}

    class Response:
        status_code = 200

        def json(self):
            calls["n"] += 1
            return payload

    client = se.SolarEdgeClient(api_key="k", site_id="1", cache_dir=tmp_path)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: Response())

    first = client.energy_details(date(2025, 6, 1), date(2025, 6, 30))
    second = client.energy_details(date(2025, 6, 1), date(2025, 6, 30))

    assert calls["n"] == 1
    assert first == second == payload


def test_refresh_bypasses_the_cache(tmp_path: Path, monkeypatch):
    payload = make_payload({"Production": [("2025-06-01 12:00:00", 1000.0)]})
    calls = {"n": 0}

    class Response:
        status_code = 200

        def json(self):
            calls["n"] += 1
            return payload

    client = se.SolarEdgeClient(api_key="k", site_id="1", cache_dir=tmp_path)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: Response())

    client.energy_details(date(2025, 6, 1), date(2025, 6, 30))
    client.energy_details(date(2025, 6, 1), date(2025, 6, 30), refresh=True)

    assert calls["n"] == 2


def test_site_without_timezone_is_rejected(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"details": {"name": "x", "peakPower": 10, "location": {}}}

    client = se.SolarEdgeClient(api_key="k", site_id="1", cache_dir=None)
    monkeypatch.setattr(client._session, "get", lambda *a, **k: Response())

    with pytest.raises(se.SolarEdgeError, match="timeZone"):
        client.site_info()


# ── Meter dumps ──────────────────────────────────────────────────────────────

SLOTS_PER_DAY = 96


def clean_days(start: str, count: int, peak_kwh: float = 30.0) -> pd.DataFrame:
    """`count` identical local days: a triangular solar arc over a flat load."""
    index = pd.date_range(
        pd.Timestamp(f"{start} 00:00", tz=TZ), periods=SLOTS_PER_DAY * count, freq="15min"
    ).tz_convert("UTC")
    slot = np.arange(len(index)) % SLOTS_PER_DAY
    arc = np.clip(1.0 - np.abs(slot - 48) / 24.0, 0.0, None) * peak_kwh

    df = pd.DataFrame(index=index)
    df.index.name = "datetime_utc"
    df["Production_kWh"] = arc
    df["Consumption_kWh"] = 4.0
    df["SelfConsumption_kWh"] = np.minimum(arc, 4.0)
    df["FeedIn_kWh"] = df["Production_kWh"] - df["SelfConsumption_kWh"]
    df["Purchased_kWh"] = df["Consumption_kWh"] - df["SelfConsumption_kWh"]
    df["quality"] = se.QUALITY_OK
    return df[[*se.METER_COLUMNS, "quality"]]


def inject_dump(df: pd.DataFrame, starved: list[int], dump: int) -> pd.DataFrame:
    """Move the energy of the `starved` intervals into `dump`, as a settling meter does."""
    out = df.copy()
    columns = [out.columns.get_loc(m) for m in se.METER_COLUMNS]
    moved = out.iloc[starved, columns].sum().to_numpy()
    out.iloc[starved, columns] = 0.0
    out.iloc[dump, columns] = out.iloc[dump, columns].to_numpy() + moved
    return out


def run_correction(df: pd.DataFrame, site: se.SiteInfo) -> tuple[pd.DataFrame, pd.DataFrame]:
    derived = se.add_derived_columns(df, TZ)
    flagged = se.flag_implausible_production(derived, site)
    return se.redistribute_dumps(flagged, site, QUIET)


def test_dump_is_recognised_before_anything_is_corrected(site: se.SiteInfo):
    """Without the nameplate flag there is nothing to anchor a correction on."""
    raw = inject_dump(clean_days("2026-06-01", 5), starved=list(range(232, 240)), dump=240)
    flagged = se.flag_implausible_production(se.add_derived_columns(raw, TZ), site)
    assert (flagged["quality"] == se.QUALITY_IMPLAUSIBLE).sum() == 1


def test_redistribution_conserves_the_day(site: se.SiteInfo):
    """A dump moves energy inside a day. Putting it back must not change the day."""
    clean = clean_days("2026-06-01", 5)
    raw = inject_dump(clean, starved=list(range(232, 240)), dump=240)
    fixed, _ = run_correction(raw, site)

    day = slice(SLOTS_PER_DAY * 2, SLOTS_PER_DAY * 3)
    for meter in se.MEASURED_METERS:
        assert fixed[meter].iloc[day].sum() == pytest.approx(clean[meter].iloc[day].sum(), abs=0.01)


def test_redistribution_leaves_no_interval_above_the_rating(site: se.SiteInfo):
    raw = inject_dump(clean_days("2026-06-01", 5), starved=list(range(232, 240)), dump=240)
    fixed, _ = run_correction(raw, site)

    assert (fixed["Production_kW"] <= site.peak_power_kwp).all()
    assert not (fixed["quality"] == se.QUALITY_IMPLAUSIBLE).any()


def test_starved_intervals_recover_their_shape(site: se.SiteInfo):
    """The whole point: the flat-zero run either side of the dump comes back."""
    clean = clean_days("2026-06-01", 5)
    raw = inject_dump(clean, starved=list(range(232, 240)), dump=240)
    fixed, _ = run_correction(raw, site)

    restored = fixed["Production_kWh"].iloc[232:241]
    expected = clean["Production_kWh"].iloc[232:241]
    assert restored.to_numpy() == pytest.approx(expected.to_numpy(), abs=0.01)


def test_clean_days_are_left_untouched(site: se.SiteInfo):
    raw = inject_dump(clean_days("2026-06-01", 5), starved=list(range(232, 240)), dump=240)
    fixed, _ = run_correction(raw, site)

    untouched = fixed.index < fixed.index[SLOTS_PER_DAY * 2]
    assert (fixed.loc[untouched, "quality"] == se.QUALITY_OK).all()
    pd.testing.assert_series_equal(
        fixed.loc[untouched, "Production_kWh"], raw.loc[untouched, "Production_kWh"]
    )


def test_meter_identities_stay_exact_on_corrected_intervals(site: se.SiteInfo):
    raw = inject_dump(clean_days("2026-06-01", 5), starved=list(range(232, 240)), dump=240)
    fixed, _ = run_correction(raw, site)

    touched = fixed[fixed["quality"] == se.QUALITY_REDISTRIBUTED]
    assert len(touched) > 1
    production = touched["Production_kWh"] - touched["SelfConsumption_kWh"] - touched["FeedIn_kWh"]
    consumption = touched["Consumption_kWh"] - touched["SelfConsumption_kWh"] - touched["Purchased_kWh"]
    assert production.abs().max() == pytest.approx(0.0, abs=1e-9)
    assert consumption.abs().max() == pytest.approx(0.0, abs=1e-9)
    assert (touched[list(se.METER_COLUMNS)] >= 0).all().all()


def test_correction_log_pairs_every_replaced_value(site: se.SiteInfo):
    raw = inject_dump(clean_days("2026-06-01", 5), starved=list(range(232, 240)), dump=240)
    fixed, log = run_correction(raw, site)

    assert len(log) == (fixed["quality"] == se.QUALITY_REDISTRIBUTED).sum()
    assert (log["role"] == "dump").sum() == 1
    assert log.index.equals(fixed.index[fixed["quality"] == se.QUALITY_REDISTRIBUTED])

    for meter in se.METER_COLUMNS:
        name = meter.removesuffix("_kWh")
        assert log[f"{name}_original_kWh"].to_numpy() == pytest.approx(
            raw.loc[log.index, meter].to_numpy()
        )
        assert log[f"{name}_corrected_kWh"].to_numpy() == pytest.approx(
            fixed.loc[log.index, meter].to_numpy()
        )
        delta = log[f"{name}_corrected_kWh"] - log[f"{name}_original_kWh"]
        assert log[f"{name}_delta_kWh"].to_numpy() == pytest.approx(delta.to_numpy(), abs=1e-4)


def test_nothing_to_correct_means_no_log(site: se.SiteInfo):
    fixed, log = run_correction(clean_days("2026-06-01", 3), site)
    assert log.empty
    assert (fixed["quality"] == se.QUALITY_OK).all()


def test_dump_without_a_reference_day_keeps_its_flag(site: se.SiteInfo):
    """One lone day gives nothing to build a shape from, so the values stand."""
    raw = inject_dump(clean_days("2026-06-01", 1), starved=list(range(40, 48)), dump=48)
    fixed, log = run_correction(raw, site)

    assert log.empty
    assert (fixed["quality"] == se.QUALITY_IMPLAUSIBLE).sum() == 1
    pd.testing.assert_series_equal(fixed["Production_kWh"], raw["Production_kWh"])


def test_recovery_window_stops_at_a_healthy_interval():
    observed = np.array([10.0, 10.0, 1.0, 99.0, 1.0, 10.0, 10.0])
    expected = np.full(7, 10.0)
    assert se.recovery_window(observed, expected, dump=3) == (2, 4)


def test_recovery_window_stops_at_the_next_burst():
    """A catch-up burst reads above its reference, so it closes the window."""
    observed = np.array([1.0, 30.0, 99.0, 1.0, 1.0])
    expected = np.full(5, 10.0)
    assert se.recovery_window(observed, expected, dump=2) == (2, 4)


def test_recovery_window_never_grows_past_the_bound():
    observed = np.full(200, 0.0)
    expected = np.full(200, 10.0)
    lo, hi = se.recovery_window(observed, expected, dump=100)
    assert (100 - lo, hi - 100) == (se.RECOVERY_SPAN_MAX, se.RECOVERY_SPAN_MAX)


def test_recovery_window_ignores_nan_neighbours():
    observed = np.array([0.0, np.nan, 0.0, 99.0, 0.0, np.nan, 0.0])
    expected = np.full(7, 10.0)
    assert se.recovery_window(observed, expected, dump=3) == (2, 4)


def test_reshape_window_conserves_the_total():
    values = pd.Series([50.0, 0.0, 0.0, 0.0])
    weights = pd.Series([1.0, 2.0, 1.0, 1.0])
    out = se.reshape_window(values, weights)
    assert out.sum() == pytest.approx(50.0)
    assert out.tolist() == [10.0, 20.0, 10.0, 10.0]


def test_reshape_window_falls_back_to_a_flat_spread():
    """A meter that is dark all window has no shape to borrow, so split it evenly."""
    out = se.reshape_window(pd.Series([8.0, 0.0]), pd.Series([0.0, 0.0]))
    assert out.tolist() == [4.0, 4.0]


# ── End to end, offline ──────────────────────────────────────────────────────


def test_full_pipeline_over_a_dst_day_conserves_energy():
    """
    The whole point: a fall-back day must keep its energy and land on a clean,
    gapless UTC axis with both occurrences of 02:00 present.
    """
    index = naive_local_day("2025-10-26")
    payload = make_payload(
        {
            meter: [
                (stamp.strftime("%Y-%m-%d %H:%M:%S"), 2000.0 if stamp.hour == 2 else 1000.0)
                for stamp in index
            ]
            for meter in se.METERS
        }
    )

    parsed = se.parse_meter_response(payload)
    localized = se.localize_to_utc(parsed, TZ, QUIET)
    derived = se.add_derived_columns(localized, TZ)

    assert len(derived) == 100
    assert derived["Production_kWh"].sum() == pytest.approx(parsed["Production_kWh"].sum())
    assert derived.index.is_unique
    assert derived.index.is_monotonic_increasing
    # 100 contiguous quarter hours, no holes and no overlaps
    assert (derived.index.to_series().diff().dropna() == pd.Timedelta("15min")).all()
