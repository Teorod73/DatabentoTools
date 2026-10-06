"""ZigZag swings with the logic of the NinjaTrader ZigZagAtr indicator, TimeOfDay reference type.

The threshold of a level is multiplier x reference, the reference of a 1 minute bar is
    daily ATR x usual range of its time of day slot / usual range of the cash session slots,
where the daily ATR is the average 18:00-18:00 true range of the previous ATR days sessions and the usual range of a
slot is its average high-low range in the previous profile days sessions. The slots are counted from 18:00 ET.
A bar first extends the extreme of the current leg and then checks the reversal (the order inside a bar is unknown).

Differences to the indicator: the bars are the front contract of each session, so at a contract change the true range
of that session is its high-low range (no gap to the previous contract) and the swings start again.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
import pandas as pd

ET = "America/New_York"
SESSION_START_MINUTES = 18 * 60
CASH_OPEN, CASH_CLOSE = 9 * 60 + 30, 16 * 60


@dataclass
class ZigZagSettings:
    multipliers: tuple[float, ...] = (0.3, 0.5)
    atr_days: int = 14
    profile_days: int = 10
    slot_minutes: int = 30


def prepare_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """ET time of the bar start and its slot from 18:00."""
    et = pd.to_datetime(bars.minute, unit="s", utc=True).dt.tz_convert(ET)
    minutes = et.dt.hour * 60 + et.dt.minute
    return bars.assign(et=et, minute_of_day=minutes,
                       from_start=(minutes - SESSION_START_MINUTES) % (24 * 60))


def is_cash_slot(slot: int, slot_minutes: int) -> bool:
    start = (SESSION_START_MINUTES + slot * slot_minutes) % (24 * 60)
    return start >= CASH_OPEN and start + slot_minutes <= CASH_CLOSE


def swings(bars: pd.DataFrame, s: ZigZagSettings | None = None) -> pd.DataFrame:
    """Confirmed swing points of every level. bars: bars_1min rows sorted by time."""
    s = s or ZigZagSettings()
    bars = prepare_bars(bars).sort_values("minute").reset_index(drop=True)
    slot_count = -(-24 * 60 // s.slot_minutes)
    cash_slots = [i for i in range(slot_count) if is_cash_slot(i, s.slot_minutes)]

    true_ranges = deque(maxlen=s.atr_days)
    slot_ranges = [deque(maxlen=s.profile_days) for _ in range(slot_count)]
    slot_reference = np.zeros(slot_count)
    atr = 0.0
    completed = 0
    prev_close = None
    prev_symbol = None

    # level state
    levels = [{"m": m, "dir": 0, "hi": -np.inf, "lo": np.inf, "hi_i": -1, "lo_i": -1,
               "ext": 0.0, "ext_i": -1, "sw": 0.0, "sw_i": -1} for m in s.multipliers]
    out = []

    high, low, close = bars.high.to_numpy(float), bars.low.to_numpy(float), bars.close.to_numpy(float)
    slots = (bars.from_start.to_numpy() // s.slot_minutes).astype(int)
    session = bars.session.to_numpy()
    symbol = bars.instrument_id.to_numpy()

    def reset_levels():
        for lv in levels:
            lv.update(dir=0, hi=-np.inf, lo=np.inf, hi_i=-1, lo_i=-1, ext=0.0, ext_i=-1, sw=0.0, sw_i=-1)

    def confirm(lv, i, reference):
        out.append({"multiplier": lv["m"], "index": lv["ext_i"], "price": lv["ext"],
                    "type": "high" if lv["dir"] == 1 else "low",
                    "prev_index": lv["sw_i"], "prev_price": lv["sw"],
                    "confirm_index": i, "reference": reference})
        lv["sw"], lv["sw_i"] = lv["ext"], lv["ext_i"]

    start = 0
    n = len(bars)
    while start < n:
        # one session
        end = start
        while end < n and session[end] == session[start]:
            end += 1

        if prev_symbol is not None and symbol[start] != prev_symbol:
            prev_close = None
            reset_levels()

        ready = completed >= max(s.atr_days, s.profile_days)
        for i in range(start, end):
            if not ready:
                break
            reference = slot_reference[slots[i]]
            for lv in levels:
                threshold = lv["m"] * reference
                if threshold <= 0:
                    continue
                if lv["dir"] == 0:
                    if high[i] > lv["hi"]:
                        lv["hi"], lv["hi_i"] = high[i], i
                    if low[i] < lv["lo"]:
                        lv["lo"], lv["lo_i"] = low[i], i
                    if lv["hi"] - lv["lo"] < threshold:
                        continue
                    if lv["hi_i"] > lv["lo_i"]:
                        lv.update(sw=lv["lo"], sw_i=lv["lo_i"], ext=lv["hi"], ext_i=lv["hi_i"], dir=1)
                    else:
                        lv.update(sw=lv["hi"], sw_i=lv["hi_i"], ext=lv["lo"], ext_i=lv["lo_i"], dir=-1)
                elif lv["dir"] == 1:
                    if high[i] > lv["ext"]:
                        lv["ext"], lv["ext_i"] = high[i], i
                    if lv["ext"] - low[i] >= threshold:
                        confirm(lv, i, reference)
                        lv.update(dir=-1, ext=low[i], ext_i=i)
                else:
                    if low[i] < lv["ext"]:
                        lv["ext"], lv["ext_i"] = low[i], i
                    if high[i] - lv["ext"] >= threshold:
                        confirm(lv, i, reference)
                        lv.update(dir=1, ext=high[i], ext_i=i)

        # the session is complete: true range and slot ranges
        h, l = high[start:end].max(), low[start:end].min()
        true_ranges.append(h - l if prev_close is None else max(h, prev_close) - min(l, prev_close))
        for slot in np.unique(slots[start:end]):
            mask = slots[start:end] == slot
            slot_ranges[slot].append(high[start:end][mask].max() - low[start:end][mask].min())
        prev_close = close[end - 1]
        prev_symbol = symbol[start]
        completed += 1

        atr = float(np.mean(true_ranges))
        usual = np.array([np.mean(r) if r else 0.0 for r in slot_ranges])
        cash_values = [usual[i] for i in cash_slots if usual[i] > 0]
        cash_average = float(np.mean(cash_values)) if cash_values else 0.0
        slot_reference = np.where((usual > 0) & (cash_average > 0), atr * usual / max(cash_average, 1e-12), atr)

        start = end

    df = pd.DataFrame(out)
    if df.empty:
        return df

    df["atr_ref"] = df.reference
    df["time"] = bars.et.to_numpy()[df["index"]]
    df["confirm_time"] = bars.et.to_numpy()[df["confirm_index"]]
    df["session"] = session[df["index"]]
    df["instrument_id"] = symbol[df["index"]]
    df["volume"] = bars.volume.to_numpy()[df["index"]]
    df["leg_before"] = (df.price - df.prev_price).abs()
    # the leg after a swing is the leg before the next swing of the same level
    following = df.groupby("multiplier")
    df["leg_after"] = following.leg_before.shift(-1).where(following.prev_index.shift(-1) == df["index"])
    df["minute_of_day"] = bars.minute_of_day.to_numpy()[df["index"]]
    return df.drop(columns=["atr_ref"])
