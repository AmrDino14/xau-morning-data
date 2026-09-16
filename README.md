# xau-morning-data

Publishes spot XAUUSD (gold) levels each weekday morning so a scheduled agent can read them
without direct access to a price feed.

- `xau_data.py` downloads spot XAU/USD BID candles from Dukascopy's public datafeed and builds
  1h, 4h (aligned to 17:00 New York) and daily (New York trading day) bars. It prints JSON:
  spot price, prior day OHLC, overnight range (18:00-07:00 New York), week open, prior week
  high/low, 14-day ATR, trend facts per timeframe, clustered support and resistance levels
  with their sources, and unbroken trend lines projected to 08:00 and 12:00 New York.
- `.github/workflows/fetch-xau-data.yml` runs it every weekday at 06:50 New York and commits
  the result.

Published files, read by the 7:00am agent:

- `data/xau.json` - the levels and trend facts
- `data/calendar.json` - this week's economic calendar (ForexFactory weekly JSON)

Prices are spot XAU/USD, not futures. Data is delayed and is for morning preparation only;
it is not financial advice.
