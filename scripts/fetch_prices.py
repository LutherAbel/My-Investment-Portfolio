"""Fetch all price history needed by compute_twr.py / compute_tw.py from Yahoo.

Writes Data\prices_tw.json (TW tickers, adjusted closes + split events),
Data\prices_us.json (FT-era US tickers), Data\ndx.json, Data\fx.json (JPY/HKD).
Run before the compute scripts whenever new trades/dates were added.
"""
import csv
import json
import time
import urllib.request

DATA = r"C:\Users\Tr7\Trades\Data"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
P_START = 1546300800  # 2019-01-01
NOW = int(time.time()) + 86400


def fetch(sym, p1=P_START):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
           f"?period1={p1}&period2={NOW}&interval=1d&events=splits")
    d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))
    res = d["chart"]["result"][0]
    closes = {}
    for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            closes[time.strftime("%Y-%m-%d", time.localtime(ts))] = round(c, 4)
    splits = {}
    for ev in (res.get("events", {}).get("splits", {}) or {}).values():
        splits[time.strftime("%Y-%m-%d", time.localtime(ev["date"]))] = ev["numerator"] / ev["denominator"]
    return {"close": closes, "splits": splits}


def main():
    # --- TW tickers from the canonical trade file ---
    tw = set()
    for r in csv.DictReader(open(rf"{DATA}\TW.csv", encoding="utf-8-sig")):
        t = r["Ticker"].strip()
        if t.isdigit() and len(t) < 4:
            t = t.zfill(4)
        tw.add(t)
    out, failed = {}, []
    for t in sorted(tw):
        got = None
        for suffix in (".TW", ".TWO"):
            try:
                g = fetch(t + suffix)
                if g["close"]:
                    got = g
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if got is None:
            failed.append(t)
        else:
            out[t] = got
        time.sleep(0.25)
    json.dump(out, open(rf"{DATA}\prices_tw.json", "w"))
    print(f"TW: {len(out)} tickers fetched, failed: {failed}")

    # --- FT-era US tickers (fixed list; UST/option are cost-carried in compute) ---
    us = ["TMF", "TLT", "TTT", "SQQQ", "SDOW", "QID", "SPXS", "IWO", "TQQQ",
          "WMT", "NVDA", "AMD", "MU", "VIXY", "UVIX"]
    outu = {}
    for t in us:
        outu[t] = fetch(t, p1=1651000000)  # 2022-04-26
        time.sleep(0.25)
    json.dump(outu, open(rf"{DATA}\prices_us.json", "w"))
    print(f"US: {len(outu)} tickers fetched")

    # --- NDX + FX ---
    json.dump(fetch("%5ENDX", p1=1650844800), open(rf"{DATA}\ndx.json", "w"))
    fx = {"jpy": fetch("JPY%3DX", p1=1656000000), "hkd": fetch("HKD%3DX", p1=1656000000)}
    json.dump(fx, open(rf"{DATA}\fx.json", "w"))
    print("NDX + FX fetched")


if __name__ == "__main__":
    main()
