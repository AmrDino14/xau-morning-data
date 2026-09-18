#!/usr/bin/env python3
"""Front-month ES, NQ and GC futures levels from Yahoo Finance -> JSON for the morning brief.

Yahoo's continuous symbols (ES=F, NQ=F, GC=F) splice contract months together, so swings
from before a roll would sit on the old contract's prices (ES Sep and Dec differed by about
65 points). Instead this picks the traded contract from the exchange roll calendar and builds
every level from that one contract's own history, rounded to its tick size.

Yahoo throttles plain HTTP clients quickly (HTTP 429), so this makes only two requests per
symbol and, when curl_cffi is installed, presents a browser TLS fingerprint. Prices are about
10 minutes delayed. Shares the level logic in xau_data.py. Usage:
python3 futures_data.py > futures.json
"""
import datetime as dt
import json
import time
import urllib.error
import urllib.request

from xau_data import NY, UTC, fractals, h4_key, resample, structure, trading_day, trendlines

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

MONTH_CODES = "FGHJKMNQUVXZ"
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# cycle: the months volume actually trades in (gold skips the thin Oct contract).
# rth: regular trading hours (New York) whose high/low/close matter to index futures traders.
SPECS = {
    "ES": {"name": "E-mini S&P 500", "suffix": "CME", "cycle": "HMUZ", "tick": 0.25,
           "point_value": 50, "micro": "MES", "micro_point_value": 5, "rth": (dt.time(9, 30), dt.time(16, 0))},
    "NQ": {"name": "E-mini Nasdaq-100", "suffix": "CME", "cycle": "HMUZ", "tick": 0.25,
           "point_value": 20, "micro": "MNQ", "micro_point_value": 2, "rth": (dt.time(9, 30), dt.time(16, 0))},
    "GC": {"name": "Gold (COMEX)", "suffix": "CMX", "cycle": "GJMQZ", "tick": 0.10,
           "point_value": 100, "micro": "MGC", "micro_point_value": 10, "rth": None},
}


class NotListed(Exception):
    pass


_session = cffi_requests.Session(impersonate="chrome") if cffi_requests else None


def get_json(url):
    if _session is not None:
        r = _session.get(url, timeout=30)
        if r.status_code == 404:
            raise NotListed(url)
        r.raise_for_status()
        return r.json()
    try:
        return json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotListed(url) from e
        raise


def yahoo(ticker, interval, rng):
    """(bars, meta) from Yahoo's chart API; bars are (utc datetime, open, high, low, close)."""
    err = None
    for attempt, host in enumerate(["query1", "query2", "query1", "query2"]):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}?interval={interval}&range={rng}"
        try:
            res = get_json(url)["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            bars = [(dt.datetime.fromtimestamp(t, UTC), o, h, l, c)
                    for t, o, h, l, c in zip(res.get("timestamp") or [], q["open"], q["high"], q["low"], q["close"])
                    if None not in (o, h, l, c)]
            return bars, res["meta"]
        except NotListed:
            raise
        except Exception as e:
            err = e
        time.sleep((5, 15, 30, 0)[attempt])  # 429s need real backoff, not quick retries
    raise RuntimeError(f"{ticker} {interval}/{rng}: {err}")


def third_friday(y, m):
    d = dt.date(y, m, 15)
    return d + dt.timedelta(days=(4 - d.weekday()) % 7)


def last_business_day(y, m):
    d = dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def roll_date(root, y, m):
    """The day volume moves off the contract expiring in month m of year y."""
    if root in ("ES", "NQ"):
        return third_friday(y, m) - dt.timedelta(days=8)  # CME equity roll: Thursday before expiry week
    py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
    return last_business_day(py, pm) - dt.timedelta(days=3)  # gold: a few days before first notice


def front_contract(root, spec, today):
    """(yahoo ticker, label, code) for the contract being traded on `today`."""
    y, m = today.year, today.month
    for _ in range(24):
        code = MONTH_CODES[m - 1]
        if code in spec["cycle"] and today < roll_date(root, y, m):
            short = f"{root}{code}{y % 100:02d}"
            return f"{short}.{spec['suffix']}", f"{MONTH_NAMES[m - 1]} {y}", short
        m += 1
        if m == 13:
            y, m = y + 1, 1
    raise ValueError(f"no contract found for {root}")


def cluster(cands, spot, band, tol, tick):
    """Merge nearby candidate prices into zones; nearest resistance and support first."""
    rnd = lambda p: round(round(p / tick) * tick, 2)
    cands = sorted(((n, p) for n, p in cands if abs(p - spot) <= band), key=lambda x: x[1])
    zones = []
    for n, p in cands:
        if zones and p - zones[-1]["prices"][-1] <= tol:
            zones[-1]["prices"].append(p)
            zones[-1]["sources"].append(n)
        else:
            zones.append({"prices": [p], "sources": [n]})
    levels = [{"price": rnd(sum(z["prices"]) / len(z["prices"])),
               "zone": [rnd(min(z["prices"])), rnd(max(z["prices"]))],
               "sources": sorted(set(z["sources"])), "touches": len(z["prices"])} for z in zones]
    res = sorted([l for l in levels if l["price"] > spot], key=lambda l: l["price"])[:6]
    sup = sorted([l for l in levels if l["price"] < spot], key=lambda l: -l["price"])[:6]
    return res, sup


def build(root, spec, now):
    tick = spec["tick"]
    rnd = lambda p: round(round(p / tick) * tick, 2)
    ticker, label, code = front_contract(root, spec, now.astimezone(NY).date())
    try:
        m30, meta = yahoo(ticker, "30m", "60d")
    except NotListed:
        # Yahoo has not listed the calendar's contract: fall back to its continuous series.
        ticker, label, code = f"{root}=F", "continuous (month not identified)", f"{root}1!"
        m30, meta = yahoo(ticker, "30m", "60d")
    h60, _ = yahoo(ticker, "60m", "6mo")
    if len(h60) < 300 or len(m30) < 100:
        return {"error": f"not enough bars for {ticker} (60m: {len(h60)}, 30m: {len(m30)})"}

    ny_now = now.astimezone(NY)
    tday = trading_day(now)
    spot = m30[-1][4]
    last_trade = dt.datetime.fromtimestamp(meta.get("regularMarketTime") or m30[-1][0].timestamp(), NY)

    daily = resample(h60, trading_day)
    completed = [b for b in daily if trading_day(b[0]) < tday]
    prior = completed[-1]
    trs = [max(h - l, abs(h - completed[i - 1][4]), abs(l - completed[i - 1][4]))
           for i, (_, _, h, l, _) in enumerate(completed) if i > 0]
    atr14 = sum(trs[-14:]) / 14 if len(trs) >= 14 else None

    # Overnight (Globex) session: 18:00 New York the evening before, up to now or the regular-hours open.
    on_start = dt.datetime.combine(tday - dt.timedelta(days=1), dt.time(18), NY)
    on_end = dt.datetime.combine(tday, spec["rth"][0] if spec["rth"] else dt.time(8, 20), NY)
    on = [b for b in m30 if on_start <= b[0].astimezone(NY) < min(on_end, ny_now)]
    overnight = None
    if on:
        oh, ol = max(b[2] for b in on), min(b[3] for b in on)
        overnight = {"from_ny": on_start.strftime("%a %H:%M"), "to_ny": min(on_end, ny_now).strftime("%a %H:%M"),
                     "high": rnd(oh), "low": rnd(ol), "range": round(oh - ol, 2),
                     "pct_of_atr14": round((oh - ol) / atr14 * 100) if atr14 else None}

    # Prior regular-hours session (index futures): the latest New York date whose 9:30-16:00 is complete.
    prior_rth = None
    if spec["rth"]:
        start, end = spec["rth"]
        sessions = {}
        for b in m30:
            t = b[0].astimezone(NY)
            if start <= t.time() < end and t.weekday() < 5:
                sessions.setdefault(t.date(), []).append(b)
        done = [d for d in sorted(sessions) if d < ny_now.date() or ny_now.time() >= end]
        if done:
            s = sessions[done[-1]]
            prior_rth = {"date": done[-1].isoformat(), "open": rnd(s[0][1]), "high": rnd(max(b[2] for b in s)),
                         "low": rnd(min(b[3] for b in s)), "close": rnd(s[-1][4])}

    iso = lambda d: d.isocalendar()[:2]
    week_bars = [b for b in m30 if iso(trading_day(b[0])) == iso(tday)]
    prev_week = [b for b in completed if iso(trading_day(b[0])) == iso(tday - dt.timedelta(days=7))]

    cands = [("Prior day high", prior[2]), ("Prior day low", prior[3]), ("Prior day close", prior[4])]
    if prior_rth:
        cands += [("Prior RTH high", prior_rth["high"]), ("Prior RTH low", prior_rth["low"]),
                  ("Prior RTH close", prior_rth["close"])]
    if overnight:
        cands += [("Overnight high", overnight["high"]), ("Overnight low", overnight["low"])]
    if week_bars:
        cands.append(("Week open", week_bars[0][1]))
    if prev_week:
        cands += [("Prior week high", max(b[2] for b in prev_week)), ("Prior week low", min(b[3] for b in prev_week))]
    h4 = resample(h60, h4_key)
    h4_recent = [b for b in h4 if b[0] >= now - dt.timedelta(days=30)]
    h1_recent = [b for b in h60 if b[0] >= now - dt.timedelta(days=7)]
    dh, dl = fractals(completed[-120:], 2)
    hh4, hl4 = fractals(h4_recent, 2)
    cands += [("Daily swing high", p) for _, _, p in dh] + [("Daily swing low", p) for _, _, p in dl]
    cands += [("4H swing high", p) for _, _, p in hh4] + [("4H swing low", p) for _, _, p in hl4]
    band = max(2 * atr14, 0.015 * spot) if atr14 else 0.02 * spot
    tol = max(4 * tick, 0.05 * atr14) if atr14 else 4 * tick
    res, sup = cluster(cands, spot, band, tol, tick)

    targets = {name: dt.datetime.combine(tday, at, NY).astimezone(UTC)
               for name, at in (("08:00_ny", dt.time(8)), ("09:30_ny", dt.time(9, 30)), ("12:00_ny", dt.time(12)))}
    lines = {"4h": trendlines(h4_recent, 2, targets, "4h"), "1h": trendlines(h1_recent, 3, targets, "1h")}
    for tf in lines.values():
        for line in tf.values():
            if line:
                line["line_now"] = rnd(line["line_now"])
                line["projected"] = {k: rnd(v) for k, v in line["projected"].items()}

    return {
        "name": spec["name"],
        "contract": label,
        "contract_code": code,
        "yahoo_ticker": ticker,
        "tick": tick,
        "point_value_usd": spec["point_value"],
        "micro": spec["micro"],
        "micro_point_value_usd": spec["micro_point_value"],
        "last": rnd(spot),
        "last_trade_ny": last_trade.strftime("%a %b %d %H:%M"),
        "prior_day": {"date": trading_day(prior[0]).isoformat(), "open": rnd(prior[1]), "high": rnd(prior[2]),
                      "low": rnd(prior[3]), "close": rnd(prior[4])},
        "change_vs_prior_close": round(spot - prior[4], 2),
        "prior_rth": prior_rth,
        "overnight": overnight,
        "week_open": rnd(week_bars[0][1]) if week_bars else None,
        "prior_week": {"high": rnd(max(b[2] for b in prev_week)), "low": rnd(min(b[3] for b in prev_week))} if prev_week else None,
        "atr14_daily": round(atr14, 2) if atr14 else None,
        "trend": {"daily": structure(completed[-150:], 2), "4h": structure(h4_recent, 2), "1h": structure(h1_recent, 3)},
        "resistance_levels_nearest_first": res,
        "support_levels_nearest_first": sup,
        "trendlines": lines,
    }


def main():
    now = dt.datetime.now(UTC)
    out = {"source": "Yahoo Finance futures, about 10 minutes delayed; one contract month per symbol",
           "client": "curl_cffi (browser fingerprint)" if cffi_requests else "urllib",
           "generated_ny": now.astimezone(NY).strftime("%a %b %d %Y %H:%M"),
           "trading_day": trading_day(now).isoformat(), "symbols": {}}
    for root, spec in SPECS.items():
        try:
            out["symbols"][root] = build(root, spec, now)
        except Exception as e:
            out["symbols"][root] = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
