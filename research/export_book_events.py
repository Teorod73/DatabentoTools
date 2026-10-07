"""Events file for the second C# pass (DatabentoExtract book) from the event study.

    python export_book_events.py <resultsDir> <events.csv.gz> <book_events.csv>

One row per distinct touch (the twin HVN rows of the two classifications are the same touch): the event_id, the
front contract, the UTC start of the touch minute, the zone, the touch tolerance (band) and the trade side
(+1 long, -1 short). The event_id is session_minuteOfDay_zoneLow_zoneHigh_side_baseline, the event study rows
are joined back on it.
"""
import os
import sys

import pandas as pd

ET = "America/New_York"


def event_ids(df: pd.DataFrame) -> pd.Series:
    side = df.trade_side.map({"long": 1, "short": -1})
    return (df.session.dt.strftime("%Y%m%d") + "_" + df.minute_of_day.astype(str) + "_" +
            df.zone_low.round(4).astype(str) + "_" + df.zone_high.round(4).astype(str) + "_" +
            side.astype(str) + "_" + df.baseline.astype(int).astype(str))


def main(results: str, events_path: str, out: str) -> None:
    sessions = pd.read_csv(os.path.join(results, "sessions.csv"), parse_dates=["session"]).set_index("session")
    df = pd.read_csv(events_path, parse_dates=["session"])
    df["event_id"] = event_ids(df)
    df = df.drop_duplicates("event_id")

    # ET wall time of the touch minute: 18:00 and later belongs to the previous calendar day
    day = df.session - pd.to_timedelta((df.minute_of_day >= 18 * 60).astype(int), unit="D")
    local = day + pd.to_timedelta(df.minute_of_day, unit="m")
    utc = local.dt.tz_localize(ET, ambiguous=True, nonexistent="shift_forward").dt.tz_convert("UTC")

    out_df = pd.DataFrame({
        "event_id": df.event_id,
        "instrument_id": df.session.map(sessions.instrument_id).astype(int),
        "t0_utc_seconds": ((utc - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(seconds=1)).astype(int),
        "zone_low": df.zone_low.round(6),
        "zone_high": df.zone_high.round(6),
        "band": (0.03 * df.atr).round(6),
        "side": df.trade_side.map({"long": 1, "short": -1}),
    }).sort_values("t0_utc_seconds")
    out_df.to_csv(out, index=False)
    print(f"{len(out_df)} events ({df.baseline.sum()} on shifted zones) -> {out}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
