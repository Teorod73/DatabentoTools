"""Session volume profiles from the DatabentoExtract output, with the logic of the NinjaTrader VolumeProfileTool.

Sessions: 18:00-17:00 America/New_York, named by the ET date they end on. The front contract of a session is the
outright with the largest volume in it. Value area, POC, Prominence HVNs and the Upper/Lower HVN classification are
ports of AddOns/GM/Models/ValueArea.cs, WindowClusters.cs (TryCalcProminenceClusters) and
Helpers/VolumeProfileRenderer.cs (CalcHvnKinds) of the Custom repository.
"""
from __future__ import annotations

import glob
import math
import os
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

ET = "America/New_York"
TICK = 0.25


@dataclass
class Settings:
    value_area_percentage: int = 68
    sigma_multiplier: float = 0.25
    min_prominence_percent: float = 5.0
    max_valley_percent: float = 50.0
    classify_hvns: bool = True
    hvn_min_asymmetry: float = 0.8
    hvn_max_overshoot: float = 0.25
    hvn_volume_weighted: bool = True


@dataclass
class Profile:
    session: pd.Timestamp                    # ET date of the session end
    instrument_id: int
    symbol: str
    start: pd.Timestamp                      # first trade (ET)
    end: pd.Timestamp                        # last trade (ET)
    prices: np.ndarray                       # ascending, only the traded prices (as the NinjaTrader window rows)
    volumes: np.ndarray
    buys: np.ndarray
    sells: np.ndarray
    high: float = math.nan
    low: float = math.nan
    poc: float = math.nan
    vah: float = math.nan
    val: float = math.nan
    complete: bool = True                    # traded from the start to the end of the session
    smoothed: np.ndarray | None = None
    hvns: list[dict] = field(default_factory=list)   # high, low, peak, kind
    lvns: list[dict] = field(default_factory=list)   # high, low

    @property
    def volume(self) -> int:
        return int(self.volumes.sum())


# ---------------------------------------------------------------------------------------------- loading

def read_extract(directory: str, kind: str) -> pd.DataFrame:
    files = sorted(glob.glob(os.path.join(directory, kind, "*.csv.gz")))
    if not files:
        raise FileNotFoundError(f"No {kind} files in {directory}")
    return pd.concat((pd.read_csv(f) for f in files), ignore_index=True)


def read_symbols(directory: str) -> dict[int, str]:
    symbols: dict[int, str] = {}
    for f in sorted(glob.glob(os.path.join(directory, "instruments", "*.csv"))):
        df = pd.read_csv(f, usecols=["instrument_id", "symbol"])
        symbols.update(dict(zip(df.instrument_id, df.symbol.fillna(""))))
    return symbols


def add_session(df: pd.DataFrame, utc_column: str) -> pd.DataFrame:
    """ET time and the session (ET date the 18:00-17:00 session ends on)."""
    et = pd.to_datetime(df[utc_column], unit="s", utc=True).dt.tz_convert(ET)
    df = df.assign(et=et)
    # 18:00 ET and later belongs to the next day's session
    df["session"] = (et + pd.Timedelta(hours=6)).dt.tz_localize(None).dt.normalize()
    return df


# ---------------------------------------------------------------------------------------------- calculations

def value_area(prices: np.ndarray, volumes: np.ndarray, percentage: int) -> tuple[float, float, float]:
    """POC: the first row with the largest volume. The value area grows from the POC to the neighbouring row
    (one tick away) with the larger volume, the higher one on equal volumes, until the target volume."""
    index = {p: i for i, p in enumerate(prices)}
    poc_i = int(np.argmax(volumes))
    target = volumes.sum() * percentage / 100.0
    volume = volumes[poc_i]
    hi = lo = poc_i
    while volume < target:
        higher = index.get(prices[hi] + TICK)
        lower = index.get(prices[lo] - TICK)
        if higher is None and lower is None:
            break
        if higher is not None and (lower is None or volumes[higher] >= volumes[lower]):
            hi = higher
            volume += volumes[higher]
        else:
            lo = lower
            volume += volumes[lower]
    return prices[poc_i], prices[hi], prices[lo]


def gauss_kernel(radius: int, sigma: float) -> np.ndarray:
    k = np.arange(-radius, radius + 1)
    kernel = np.exp(-(k * k) / (2 * sigma * sigma))
    return kernel / kernel.sum()


def prominence_clusters(prices: np.ndarray, volumes: np.ndarray, s: Settings):
    count = len(prices)
    if count < 3:
        return np.zeros(count), [], []

    sigma = s.sigma_multiplier * math.sqrt(count)
    radius = min(math.ceil(3.0 * sigma), count // 2)
    kernel = gauss_kernel(radius, sigma)

    smoothed = np.zeros(count)
    for i in range(count):
        lo, hi = max(0, i - radius), min(count, i + radius + 1)
        w = kernel[lo - i + radius: hi - i + radius]
        smoothed[i] = (volumes[lo:hi] * w).sum() / w.sum()

    peak_max = smoothed.max()
    if peak_max <= 0:
        return smoothed, [], []

    # local maxima, the middle of a plateau is the peak
    peaks = []
    i = 0
    while i < count:
        left = smoothed[i - 1] if i > 0 else 0.0
        if smoothed[i] <= left:
            i += 1
            continue
        j = i
        while j + 1 < count and smoothed[j + 1] == smoothed[i]:
            j += 1
        right = smoothed[j + 1] if j + 1 < count else 0.0
        if smoothed[i] > right:
            peaks.append((i + j) // 2)
        i = j + 1

    def prominence(peak: int) -> float:
        height = smoothed[peak]
        left_base = right_base = 0.0
        m = height
        for k in range(peak - 1, -1, -1):
            m = min(m, smoothed[k])
            if smoothed[k] > height:
                left_base = m
                break
        m = height
        for k in range(peak + 1, count):
            m = min(m, smoothed[k])
            if smoothed[k] > height:
                right_base = m
                break
        return height - max(left_base, right_base)

    min_prominence = peak_max * s.min_prominence_percent / 100.0
    hvn_peaks = [p for p in peaks if prominence(p) >= min_prominence]

    def curvature(k: int) -> float:
        return (smoothed[k - 1] if k > 0 else 0.0) - 2 * smoothed[k] + (smoothed[k + 1] if k < count - 1 else 0.0)

    is_hvn = np.zeros(count, dtype=bool)
    hvns = []
    for peak in hvn_peaks:
        frm = peak
        while frm > 0 and curvature(frm - 1) < 0:
            frm -= 1
        to = peak
        while to < count - 1 and curvature(to + 1) < 0:
            to += 1
        is_hvn[frm:to + 1] = True
        hvns.append({"high": prices[to], "low": prices[frm], "peak": prices[peak]})

    lvns = []
    for a, b in zip(hvn_peaks, hvn_peaks[1:]):
        valley = a
        for k in range(a + 1, b):
            if smoothed[k] < smoothed[valley]:
                valley = k
        if valley == a or is_hvn[valley]:
            continue
        lower_peak = min(smoothed[a], smoothed[b])
        if smoothed[valley] > lower_peak * s.max_valley_percent / 100.0:
            continue
        level = smoothed[valley] + (lower_peak - smoothed[valley]) * 0.5
        frm = valley
        while frm - 1 > a and smoothed[frm - 1] <= level and not is_hvn[frm - 1]:
            frm -= 1
        to = valley
        while to + 1 < b and smoothed[to + 1] <= level and not is_hvn[to + 1]:
            to += 1
        lvns.append({"high": prices[to], "low": prices[frm]})

    return smoothed, hvns, lvns


def classify_hvns(hvns: list[dict], closes: np.ndarray, bar_volumes: np.ndarray, s: Settings) -> None:
    """Upper HVN: the bar closes were below the zone and above it only shortly or shallowly, Lower: the opposite."""
    weights = bar_volumes.astype(float) if s.hvn_volume_weighted else np.ones(len(closes))
    total = weights.sum()
    half = TICK / 2
    for h in hvns:
        h["kind"] = "neutral"
        if not s.classify_hvns or total <= 0:
            continue
        above = (weights * np.maximum(0, closes - (h["high"] + half))).sum()
        below = (weights * np.maximum(0, h["low"] - half - closes)).sum()
        total_overshoot = above + below
        height = h["high"] - h["low"] + TICK
        h["asymmetry"] = (below - above) / total_overshoot if total_overshoot > 0 else 0.0
        h["overshoot"] = min(above, below) / (height * total)
        if total_overshoot > 0 and h["overshoot"] <= s.hvn_max_overshoot:
            if h["asymmetry"] >= s.hvn_min_asymmetry:
                h["kind"] = "upper"
            elif h["asymmetry"] <= -s.hvn_min_asymmetry:
                h["kind"] = "lower"


def minute_bars(seconds: pd.DataFrame) -> pd.DataFrame:
    """1 minute bars (close, volume) of one instrument from the second bars, labeled by the minute start."""
    df = seconds.assign(minute=seconds.utc_second - seconds.utc_second % 60)
    g = df.groupby("minute", sort=True)
    return pd.DataFrame({"close": g.close.last(), "volume": g.volume.sum()}).reset_index()


# ---------------------------------------------------------------------------------------------- sessions

def session_profiles(directory: str, s: Settings | None = None) -> list[Profile]:
    s = s or Settings()
    minutes = add_session(read_extract(directory, "minute"), "utc_minute")
    seconds = add_session(read_extract(directory, "seconds"), "utc_second")
    symbols = read_symbols(directory)

    minutes["volume"] = minutes.buy + minutes.sell + minutes.unknown
    profiles = []

    for session, day in minutes.groupby("session", sort=True):
        # only outrights: spread symbols have a dash, without symbols the spread prices are far below the outright ones
        is_spread = day.instrument_id.map(lambda i: "-" in symbols.get(i, "")).astype(bool) | (day.price < 1000)
        outright = day[~is_spread]
        if outright.empty:
            continue
        front = int(outright.groupby("instrument_id").volume.sum().idxmax())
        rows = outright[outright.instrument_id == front].groupby("price").agg(
            volume=("volume", "sum"), buy=("buy", "sum"), sell=("sell", "sum")).sort_index()

        bars = seconds[(seconds.session == session) & (seconds.instrument_id == front)]
        profile = Profile(
            session=session, instrument_id=front, symbol=symbols.get(front, str(front)),
            start=bars.et.min(), end=bars.et.max(),
            prices=rows.index.to_numpy(float), volumes=rows.volume.to_numpy(float),
            buys=rows.buy.to_numpy(float), sells=rows.sell.to_numpy(float))
        profile.complete = bool(
            profile.start.tz_localize(None) <= session - pd.Timedelta(hours=5, minutes=50) and
            profile.end.tz_localize(None) >= session + pd.Timedelta(hours=16, minutes=50))
        profile.high, profile.low = profile.prices.max(), profile.prices.min()
        profile.poc, profile.vah, profile.val = value_area(profile.prices, profile.volumes, s.value_area_percentage)
        profile.smoothed, profile.hvns, profile.lvns = prominence_clusters(profile.prices, profile.volumes, s)

        mb = minute_bars(bars)
        classify_hvns(profile.hvns, mb.close.to_numpy(float), mb.volume.to_numpy(float), s)
        profiles.append(profile)

    return profiles


def profiles_table(profiles: list[Profile]) -> pd.DataFrame:
    rows = []
    for p in profiles:
        rows.append({
            "session": p.session.date(), "symbol": p.symbol, "complete": p.complete, "start": p.start, "end": p.end,
            "volume": p.volume, "rows": len(p.prices), "high": p.high, "low": p.low,
            "poc": p.poc, "vah": p.vah, "val": p.val,
            "hvns": "; ".join(f"{h['low']}-{h['high']} {h['kind']}" for h in reversed(p.hvns)),
        })
    return pd.DataFrame(rows)
