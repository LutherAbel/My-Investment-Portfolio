r"""TW portfolio TWR pipeline from Data\TW.csv + Yahoo adjusted closes, 0050 benchmark.

Method notes (established 2026-07):
- Work entirely in Yahoo's adjusted-price space; each trade's quantity is scaled
  by f = TradePrice/close(trade date) when |f-1| > 0.12 (different share basis:
  pre-split / pre-配股 / 減資). Trade money is conserved exactly.
- Two books per ticker: raw shares (authoritative for what is held) and
  adjusted shares (for market value). A sell that empties the raw book zeroes
  both, so adjustment residue never lingers as a phantom position.
- Over-sells beyond adjusted holdings (配股) are realized on the sale day.
- Daily return: Modified Dietz, r = (MV_t - MV_p - net_buys)/(MV_p + gross_buys),
  out-of-market (denom <= 100 TWD) days flat.
- Excludes cash dividends and understates returns ~1-3%/yr; no broker NAV to
  reconcile against, so precision is approximate by design.
Validation anchor: END_HOLDINGS below must match the final raw book exactly.
Output: Data\tw_data.json.
"""
import csv
import json
from collections import defaultdict
from datetime import date, timedelta

DATA = r"C:\Users\Tr7\Trades\Data"
END_HOLDINGS = {"2308"}   # update when the real portfolio changes

prices = json.load(open(rf"{DATA}\prices_tw.json"))
rows = list(csv.DictReader(open(rf"{DATA}\TW.csv", encoding="utf-8-sig")))


def parse_d(s):
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def close_near(t, d, back=10):
    c = prices[t]["close"]
    for k in range(back):
        v = c.get((d - timedelta(days=k)).isoformat())
        if v:
            return v
    return None


names = {}
trades = []  # (date, ticker, qty_adj, qty_raw, money_twd)
for r in rows:
    t = r["Ticker"].strip()
    if t.isdigit() and len(t) < 4:
        t = t.zfill(4)  # Excel strips leading zeros: 56 -> 0056
    d = parse_d(r["TradeDate"])
    q_raw = float(r["Quantity"]) * (1 if r["Buy/Sell"] == "BUY" else -1)
    q = q_raw
    money = abs(float(r["TradeMoney"])) * (1 if q_raw > 0 else -1)
    yc = close_near(t, d)
    f = float(r["TradePrice"]) / yc if yc else 1.0
    if abs(f - 1) > 0.12:
        q *= f
    trades.append((d, t, q, q_raw, money))
    names[t] = r["Symbol"]
trades.sort(key=lambda x: x[0])

cal = sorted(date.fromisoformat(k) for k in prices["0050"]["close"])
cal = [d for d in cal if d >= trades[0][0]]

pos = defaultdict(float)      # adjusted shares (market value)
raw_pos = defaultdict(float)  # raw shares (what is actually held)
ti = 0
dates, index_vals, dd_vals, holdings_by_day = [], [], [], []
bench_vals, bench_matched = [], []
ret_by_day = {}
idx = peak = 100.0
b_raw = b_match = 100.0
b0 = prices["0050"]["close"][cal[0].isoformat()]
prev_mv = 0.0
prev_b_close = b0

for di_, d in enumerate(cal):
    nb = gb = 0.0
    while ti < len(trades) and trades[ti][0] <= d:
        _, t, q, q_raw, money = trades[ti]
        if q < 0 and pos[t] + q < 0:
            q = -pos[t]      # over-sell (配股): realize on sale day
        pos[t] += q
        raw_pos[t] += q_raw
        if q_raw < 0 and raw_pos[t] <= 0.5:
            raw_pos[t] = pos[t] = 0.0   # full exit clears both books
        nb += money
        if money > 0:
            gb += money
        ti += 1
    mv = 0.0
    for t, q in pos.items():
        if q > 0.001:
            c = close_near(t, d, back=30)
            if c:
                mv += q * c
    if di_ > 0:
        denom = prev_mv + gb
        r = (mv - prev_mv - nb) / denom if denom > 100 else 0.0
        ret_by_day[d] = r
        idx *= 1 + r
        peak = max(peak, idx)
        bc = prices["0050"]["close"].get(d.isoformat(), prev_b_close)
        b_raw = bc / b0 * 100
        if denom > 100:
            b_match *= bc / prev_b_close
        prev_b_close = bc
    dates.append(d.isoformat())
    index_vals.append(round(idx, 4))
    dd_vals.append(round((idx / peak - 1) * 100, 3))
    bench_vals.append(round(b_raw, 2))
    bench_matched.append(round(b_match, 2))
    holdings_by_day.append(" · ".join(sorted(names[t] for t, q in raw_pos.items() if q > 0.5)))
    prev_mv = mv

final_held = {t for t, q in raw_pos.items() if q > 0.5}
assert final_held == END_HOLDINGS, f"end-state holdings mismatch: {final_held} vs {END_HOLDINGS}"

monthly = defaultdict(lambda: 1.0)
for d, r in ret_by_day.items():
    monthly[(d.year, d.month)] *= 1 + r
monthly_out = [{"y": y, "m": m, "ret": round((v - 1) * 100, 2)} for (y, m), v in sorted(monthly.items())]

eps, p_, t_, in_dd = [], 0, 0, False
for i in range(1, len(index_vals)):
    if index_vals[i] >= index_vals[p_]:
        if in_dd:
            eps.append((p_, t_, i))
            in_dd = False
        p_ = t_ = i
    else:
        in_dd = True
        if index_vals[i] < index_vals[t_]:
            t_ = i
if in_dd:
    eps.append((p_, t_, None))
top = []
for p_, t_, rcv in eps:
    top.append({
        "depth": round(index_vals[t_] / index_vals[p_] * 100 - 100, 2),
        "peak": dates[p_], "trough": dates[t_],
        "recovered": dates[rcv] if rcv is not None else None,
        "days_down": (date.fromisoformat(dates[t_]) - date.fromisoformat(dates[p_])).days,
        "days_total": (date.fromisoformat(dates[rcv]) - date.fromisoformat(dates[p_])).days if rcv is not None else None,
    })
top.sort(key=lambda e: e["depth"])

# rolling 12-month TWR vs 0050, and XIRR
cal_d = [date.fromisoformat(x) for x in dates]
b_close = prices["0050"]["close"]
def close_asof(cd, d, back=10):
    for k in range(back):
        v = cd.get(date.fromordinal(d.toordinal() - k).isoformat())
        if v:
            return v
    return None

roll, roll_bench = [], []
j = 0
for i, d in enumerate(cal_d):
    cutoff = date.fromordinal(d.toordinal() - 365)
    if cutoff < cal_d[0]:
        roll.append(None)
        roll_bench.append(None)
        continue
    while j + 1 <= i and cal_d[j + 1] <= cutoff:
        j += 1
    roll.append(round((index_vals[i] / index_vals[j] - 1) * 100, 2))
    b0_, b1_ = close_asof(b_close, cutoff), close_asof(b_close, d)
    roll_bench.append(round((b1_ / b0_ - 1) * 100, 2) if b0_ and b1_ else None)

def xirr(flows):
    t0 = flows[0][0]
    def npv(r):
        return sum(a / (1 + r) ** ((d - t0).days / 365.25) for d, a in flows)
    lo, hi = -0.95, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return mid

xf = sorted([(d, -money) for d, _, _, _, money in trades] + [(cal_d[-1], prev_mv)])
xirr_pct = round(xirr(xf) * 100, 2)

out = {
    "asof": dates[-1],
    "roll": roll, "roll_bench": roll_bench, "xirr": xirr_pct,
    "dates": dates, "index": index_vals, "bench": bench_vals,
    "bench_matched": bench_matched, "drawdown": dd_vals,
    "holdings": holdings_by_day, "monthly": monthly_out, "top_dd": top[:5],
    "final": round(index_vals[-1], 2), "max_dd": round(min(dd_vals), 2),
}
json.dump(out, open(rf"{DATA}\tw_data.json", "w", encoding="utf-8"), ensure_ascii=False)
print("asof", out["asof"], "| final", out["final"], "| max_dd", out["max_dd"],
      "| bench", bench_vals[-1], "| held:", sorted(final_held))
