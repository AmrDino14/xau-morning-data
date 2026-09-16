#!/usr/bin/env python3
"""Spot XAUUSD candles from Dukascopy's public datafeed -> JSON facts for a morning brief.

Builds 1h, 4h (aligned to 17:00 New York) and daily (New York trading day, 17:00-17:00)
bars from BID prices, then prints key levels, trend facts and trendline projections.
Standard library only. Usage: python3 xau_data.py > xau.json
"""
import datetime as dt
import json
import lzma
import struct
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
BASE = "https://datafeed.dukascopy.com/datafeed/XAUUSD/"
SCALE = 1000.0
FAILED = []


def fetch(path):
    """Decompressed bytes; b"" for an empty or missing file; None when every attempt failed."""
    for attempt in range(5):
        try:
            req = urllib.request.Request(BASE + path, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=45).read()
            return lzma.decompress(raw) if raw else b""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return b""
        except Exception:
            pass
        time.sleep(2 + attempt * 3)
    FAILED.append(path)
    return None


def candles(raw, start):
    out = []
    for i in range(len(raw or b"") // 24):
        sec, o, c, lo, hi, vol = struct.unpack(">5if", raw[i * 24:(i + 1) * 24])
        if vol > 0:
            out.append((start + dt.timedelta(seconds=sec), o / SCALE, hi / SCALE, lo / SCALE, c / SCALE))
    return out


def ticks_to_minutes(raw, hour_start):
    mins = {}
    for i in range(len(raw or b"") // 20):
        ms, _ask, bid, _av, _bv = struct.unpack(">3i2f", raw[i * 20:(i + 1) * 20])
        t = hour_start + dt.timedelta(milliseconds=ms)
        key = t.replace(second=0, microsecond=0)
        p = bid / SCALE
        if key in mins:
            o, h, l, _c = mins[key]
            mins[key] = (o, max(h, p), min(l, p), p)
        else:
            mins[key] = (p, p, p, p)
    return [(k,) + v for k, v in sorted(mins.items())]


def month_path(y, m):
    return f"{y}/{m - 1:02d}"


def resample(bars, key_fn):
    groups = {}
    for t, o, h, l, c in bars:
        k = key_fn(t)
        if k in groups:
            g = groups[k]
            groups[k] = [g[0], g[1], max(g[2], h), min(g[3], l), c]
        else:
            groups[k] = [t, o, h, l, c]
    return [tuple(v) for _, v in sorted(groups.items(), key=lambda kv: kv[1][0])]


def trading_day(t):
    """New York trading day: bars from 17:00 NY belong to the next calendar day."""
    return (t.astimezone(NY) + dt.timedelta(hours=7)).date()


def h4_key(t):
    ny = t.astimezone(NY)
    return (trading_day(t), ((ny.hour - 17) % 24) // 4)


def ema(values, n):
    if len(values) < n:
        return None
    k = 2 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return round(e, 2)


def fractals(bars, k):
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        h, l = bars[i][2], bars[i][3]
        if all(h > bars[j][2] for j in range(i - k, i + k + 1) if j != i):
            highs.append((i, bars[i][0], h))
        if all(l < bars[j][3] for j in range(i - k, i + k + 1) if j != i):
            lows.append((i, bars[i][0], l))
    return highs, lows


def structure(bars, k):
    highs, lows = fractals(bars, k)
    closes = [b[4] for b in bars]
    info = {"close": round(closes[-1], 2), "ema20": ema(closes, 20), "ema50": ema(closes, 50)}
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][2] > highs[-2][2]
        hl = lows[-1][2] > lows[-2][2]
        info["last_two_swing_highs"] = [round(highs[-2][2], 2), round(highs[-1][2], 2)]
        info["last_two_swing_lows"] = [round(lows[-2][2], 2), round(lows[-1][2], 2)]
        info["swings"] = ("higher highs and higher lows" if hh and hl else
                          "lower highs and lower lows" if not hh and not hl else "mixed swings")
    e20, e50, c = info["ema20"], info["ema50"], info["close"]
    if e20 and e50:
        info["ema_stack"] = ("price > EMA20 > EMA50" if c > e20 > e50 else
                             "price < EMA20 < EMA50" if c < e20 < e50 else "mixed")
    sw, st = info.get("swings", ""), info.get("ema_stack", "")
    info["label"] = ("Uptrend" if sw.startswith("higher") and st.startswith("price >") else
                     "Downtrend" if sw.startswith("lower") and st.startswith("price <") else "Range")
    return info


def trendlines(bars, k, targets, name):
    """Most recent line through two swing points that no candle has closed through since the first anchor.

    Rising support uses swing lows, falling resistance uses swing highs. None when no clean,
    unbroken line exists - a broken line projected forward is not a level.
    """
    highs, lows = fractals(bars, k)
    out = {}
    for kind, pts, want_up in (("rising_support", lows, True), ("falling_resistance", highs, False)):
        line = None
        for b in range(len(pts) - 1, 0, -1):
            for a in range(b - 1, max(-1, b - 6), -1):
                (i1, t1, p1), (i2, t2, p2) = pts[a], pts[b]
                if (p2 > p1) != want_up:
                    continue
                slope = (p2 - p1) / (t2 - t1).total_seconds()
                at = lambda t, p1=p1, t1=t1, slope=slope: p1 + slope * (t - t1).total_seconds()
                respected = all((bar[4] >= at(bar[0])) if want_up else (bar[4] <= at(bar[0])) for bar in bars[i1 + 1:])
                if not respected:
                    continue
                now_px = bars[-1][4]
                line = {
                    "timeframe": name,
                    "anchor_1": {"time_ny": t1.astimezone(NY).strftime("%a %b %d %H:%M"), "price": round(p1, 2)},
                    "anchor_2": {"time_ny": t2.astimezone(NY).strftime("%a %b %d %H:%M"), "price": round(p2, 2)},
                    "line_now": round(at(bars[-1][0]), 2),
                    "projected": {label: round(at(t), 2) for label, t in targets.items()},
                    "price_vs_line_now": "above" if now_px > at(bars[-1][0]) else "below",
                    "distance_now": round(now_px - at(bars[-1][0]), 2),
                }
                break
            if line:
                break
        out[kind] = line
    return out


def main():
    now = dt.datetime.now(UTC)
    ny_now = now.astimezone(NY)
    this_month = (now.year, now.month)

    # Completed months: hourly month files for the last 12 months.
    months = []
    y, m = this_month
    for _ in range(12):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        months.append((y, m))
    # Current month: one 1-minute file per completed UTC day.
    days = [dt.date(now.year, now.month, d) for d in range(1, now.day)]
    # Today (UTC): tick files for each completed hour.
    today = now.date()
    hours = list(range(0, now.hour))

    jobs = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for (yy, mm) in months:
            jobs[("M", yy, mm)] = ex.submit(fetch, f"{month_path(yy, mm)}/BID_candles_hour_1.bi5")
        for d in days:
            jobs[("D", d)] = ex.submit(fetch, f"{month_path(d.year, d.month)}/{d.day:02d}/BID_candles_min_1.bi5")
        for h in hours:
            jobs[("T", h)] = ex.submit(fetch, f"{month_path(today.year, today.month)}/{today.day:02d}/{h:02d}h_ticks.bi5")
        # Most recent completed month may not be published yet: fall back to its daily minute files.
        ly, lm = months[0]
        month_missing = False
        raw_last = jobs[("M", ly, lm)].result()
        if not raw_last:
            month_missing = True
            first = dt.date(ly, lm, 1)
            d = first
            while d.month == lm:
                jobs[("D", d)] = ex.submit(fetch, f"{month_path(d.year, d.month)}/{d.day:02d}/BID_candles_min_1.bi5")
                d += dt.timedelta(days=1)
        results = {k: f.result() for k, f in jobs.items()}

    hourly, minutes = [], []
    for (yy, mm) in months:
        start = dt.datetime(yy, mm, 1, tzinfo=UTC)
        hourly += candles(results[("M", yy, mm)], start)
    for key, raw in results.items():
        if key[0] == "D":
            d = key[1]
            if month_missing is False and (d.year, d.month) != this_month:
                continue
            start = dt.datetime(d.year, d.month, d.day, tzinfo=UTC)
            minutes += candles(raw, start)
    for h in hours:
        minutes += ticks_to_minutes(results[("T", h)], dt.datetime(today.year, today.month, today.day, h, tzinfo=UTC))
    minutes.sort()
    hourly += resample(minutes, lambda t: t.replace(minute=0))
    hourly = sorted({b[0]: b for b in hourly}.values())

    if len(hourly) < 200:
        print(json.dumps({"error": "not enough price data", "hourly_bars": len(hourly), "failed_files": FAILED}))
        sys.exit(1)

    last_t = minutes[-1][0] if minutes else hourly[-1][0]
    spot = minutes[-1][4] if minutes else hourly[-1][4]
    tday_now = trading_day(now)

    daily = resample(hourly, lambda t: trading_day(t))
    h4 = resample(hourly, h4_key)
    h1 = [b for b in hourly if b[0] >= now - dt.timedelta(days=60)]

    completed = [b for b in daily if trading_day(b[0]) < tday_now]
    prior = completed[-1]
    trs = []
    for i in range(1, len(completed)):
        _, _, h, l, _ = completed[i]
        pc = completed[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr14 = sum(trs[-14:]) / 14 if len(trs) >= 14 else None

    # Overnight: 18:00 NY on the previous calendar day to 07:00 NY on the trading day (or now if earlier).
    ny_start = dt.datetime.combine(tday_now - dt.timedelta(days=1), dt.time(18), NY)
    ny_end = min(dt.datetime.combine(tday_now, dt.time(7), NY), now.astimezone(NY))
    src = minutes if minutes else hourly
    on = [b for b in src if ny_start <= b[0].astimezone(NY) < ny_end]
    overnight = None
    if on:
        oh, ol = max(b[2] for b in on), min(b[3] for b in on)
        overnight = {"from_ny": ny_start.strftime("%a %H:%M"), "to_ny": ny_end.strftime("%a %H:%M"),
                     "high": round(oh, 2), "low": round(ol, 2), "range": round(oh - ol, 2),
                     "pct_of_atr14": round((oh - ol) / atr14 * 100) if atr14 else None}

    iso = lambda d: d.isocalendar()[:2]
    week_bars = [b for b in daily if iso(trading_day(b[0])) == iso(tday_now)]
    prev_week = [b for b in completed if iso(trading_day(b[0])) == iso(tday_now - dt.timedelta(days=7))]

    # Level candidates.
    band = max(80.0, 2 * atr14) if atr14 else 80.0
    cands = [("Prior day high", prior[2]), ("Prior day low", prior[3]), ("Prior day close", prior[4])]
    if overnight:
        cands += [("Overnight high", overnight["high"]), ("Overnight low", overnight["low"])]
    if week_bars:
        cands.append(("Week open", week_bars[0][1]))
    if prev_week:
        cands += [("Prior week high", max(b[2] for b in prev_week)), ("Prior week low", min(b[3] for b in prev_week))]
    dh, dl = fractals(daily[-120:], 2)
    hh4, hl4 = fractals([b for b in h4 if b[0] >= now - dt.timedelta(days=30)], 2)
    cands += [("Daily swing high", p) for _, _, p in dh] + [("Daily swing low", p) for _, _, p in dl]
    cands += [("4H swing high", p) for _, _, p in hh4] + [("4H swing low", p) for _, _, p in hl4]
    cands = [(n, p) for n, p in cands if abs(p - spot) <= band]
    cands.sort(key=lambda x: x[1])
    zones = []
    for n, p in cands:
        if zones and p - zones[-1]["prices"][-1] <= 4.0:
            zones[-1]["prices"].append(p)
            zones[-1]["sources"].append(n)
        else:
            zones.append({"prices": [p], "sources": [n]})
    levels = []
    for z in zones:
        levels.append({"price": round(sum(z["prices"]) / len(z["prices"]), 2),
                       "zone": [round(min(z["prices"]), 2), round(max(z["prices"]), 2)],
                       "sources": sorted(set(z["sources"])), "touches": len(z["prices"])})
    res = sorted([l for l in levels if l["price"] > spot], key=lambda l: l["price"])[:6]
    sup = sorted([l for l in levels if l["price"] < spot], key=lambda l: -l["price"])[:6]

    targets = {label: dt.datetime.combine(tday_now, dt.time(hh), NY).astimezone(UTC)
               for label, hh in (("08:00_ny", 8), ("12:00_ny", 12))}
    h4_recent = [b for b in h4 if b[0] >= now - dt.timedelta(days=30)]
    h1_recent = [b for b in h1 if b[0] >= now - dt.timedelta(days=7)]

    def fmt(bars, tf):
        label = (lambda t: trading_day(t).strftime("%a %b %d")) if tf == "1d" else (lambda t: t.astimezone(NY).strftime("%a %b %d %H:%M"))
        key = "trading_day" if tf == "1d" else "ny"
        return [{key: label(b[0]), "o": round(b[1], 2), "h": round(b[2], 2), "l": round(b[3], 2), "c": round(b[4], 2)} for b in bars]
    out = {
        "source": "Dukascopy public datafeed, spot XAU/USD BID prices (no futures)",
        "generated_ny": ny_now.strftime("%a %b %d %Y %H:%M"),
        "trading_day": tday_now.isoformat(),
        "last_price_time_ny": last_t.astimezone(NY).strftime("%a %b %d %H:%M"),
        "spot": round(spot, 2),
        "prior_day": {"date": trading_day(prior[0]).isoformat(), "open": round(prior[1], 2), "high": round(prior[2], 2),
                      "low": round(prior[3], 2), "close": round(prior[4], 2)},
        "change_vs_prior_close": round(spot - prior[4], 2),
        "overnight": overnight,
        "week_open": round(week_bars[0][1], 2) if week_bars else None,
        "prior_week": {"high": round(max(b[2] for b in prev_week), 2), "low": round(min(b[3] for b in prev_week), 2)} if prev_week else None,
        "atr14_daily": round(atr14, 2) if atr14 else None,
        "trend": {"daily": structure(completed[-150:], 2), "4h": structure(h4_recent, 2), "1h": structure(h1_recent, 3)},
        "resistance_levels_nearest_first": res,
        "support_levels_nearest_first": sup,
        "trendlines": {"4h": trendlines(h4_recent, 2, targets, "4h"), "1h": trendlines(h1_recent, 3, targets, "1h")},
        "recent_daily": fmt(completed[-10:], "1d"),
        "recent_4h": fmt(h4_recent[-12:], "4h"),
        "recent_1h": fmt(h1_recent[-24:], "1h"),
        "failed_files": FAILED,
    }
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
