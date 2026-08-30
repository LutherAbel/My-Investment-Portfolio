r"""US account TWR pipeline (Firstrade + IB combined).

Inputs (Data\): NAV.csv (IB Flex: daily NAV + Change-in-NAV + cash transactions,
year blocks concatenated), IB.csv (all trades, Account = IB/FT), FT_monthly.csv
(Firstrade statement月結 nodes), prices_us.json, ndx.json, fx.json.
Output: Data\twr_data.json.

Validation anchors (all must hold, script raises otherwise):
- own yearly TWR vs IB official TWR: |diff| < 0.01 pp for every year
- FT reconstructed positions empty after the 2024-06 ACAT
Booking rules established 2026-07: deposits with Date/Time hour >= 17 are
credited next day (SettleDate), else on the report date; the 2024-06-20
residual +1132.94 is the Firstrade ACAT (internal in the combined view).
"""
import csv
import json
import re
from collections import defaultdict
from datetime import date

DATA = r"C:\Users\Tr7\Trades\Data"
FX_RE = re.compile(r"^[A-Z]{3}\.[A-Z]{3}$")
MISSING_FLOW_DATE = {2024: date(2024, 6, 20)}   # FT ACAT arriving at IB
INTERNAL_XFER_DATE = date(2024, 6, 20)
INTERNAL_XFER_AMT = 1132.94
FIXED_PRICE = {"UST20260228": 1.04132, "TMF_P6.50_230811": 0.0}


def parse_d(s):
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


# ---------------- NAV file: daily NAV, official TWR, cash flows ----------------
nav, stock_mv = {}, {}
ib_twr, ib_dep = {}, {}
flow = defaultdict(float)
with open(rf"{DATA}\NAV.csv", newline="", encoding="utf-8-sig") as f:
    section = header = None
    for r in csv.reader(f):
        if not r or all(c == "" for c in r):
            continue
        if r[0] == "Total":
            section, header = "nav", r
            continue
        if r[0] == "TWR":
            section, header = "cnav", r
            continue
        if r[0] == "Type":
            section, header = "cash", r
            continue
        m = dict(zip(header, r))
        if section == "nav":
            d = parse_d(m["ReportDate"])
            nav[d] = float(m["Total"])
            stock_mv[d] = float(m["Stock"])
        elif section == "cnav":
            yr = int(m["FromDate"][:4])
            ib_twr[yr] = float(m["TWR"])
            ib_dep[yr] = float(m["DepositsWithdrawals"])
        elif section == "cash":
            if m["Type"] != "Deposits/Withdrawals":
                continue
            dt = m["Date/Time"]
            hour = int(dt[9:11]) if len(dt) > 9 else 0
            d = parse_d(m["SettleDate"]) if hour >= 17 else parse_d(dt[:8])
            flow[d] += float(m["Amount"]) * float(m["FXRateToBase"])

for y in sorted(ib_dep):
    residual = ib_dep[y] - sum(v for k, v in flow.items() if k.year == y)
    if abs(residual) > 1:
        if y in MISSING_FLOW_DATE:
            flow[MISSING_FLOW_DATE[y]] += residual
            print(f"# note: {y} residual flow {residual:+.2f} USD placed on {MISSING_FLOW_DATE[y]}")
        else:
            raise SystemExit(f"unreconciled {y} flow residual {residual:+.2f} USD")

# ---------------- IB daily TWR + validation vs official ----------------
days = sorted(nav)
start_i = next(i for i, d in enumerate(days) if nav[d] > 0)
ib_start = days[start_i]

dates, index_vals = [ib_start.isoformat()], [100.0]
ret_by_day = {}
idx = 100.0
for i in range(start_i + 1, len(days)):
    dp, dc = days[i - 1], days[i]
    f_ = sum(v for k, v in flow.items() if dp < k <= dc)
    denom = nav[dp] + f_
    r = (nav[dc] - nav[dp] - f_) / denom if denom != 0 else 0.0
    ret_by_day[dc] = r
    idx *= 1 + r
    dates.append(dc.isoformat())
    index_vals.append(round(idx, 4))

validation = []
for y in sorted(ib_twr):
    prod = 1.0
    for d, r in ret_by_day.items():
        if d.year == y:
            prod *= 1 + r
    mine = (prod - 1) * 100
    diff = mine - ib_twr[y]
    validation.append({"year": y, "mine": round(mine, 3), "ib": round(ib_twr[y], 3), "diff": round(diff, 3)})
    if abs(diff) > 0.01:
        raise SystemExit(f"TWR validation FAILED for {y}: mine {mine:.3f} vs IB {ib_twr[y]:.3f}")

# ---------------- trades: IB holdings + sleeve cash flows ----------------
fxj = json.load(open(rf"{DATA}\fx.json"))
def fx_on(cur, d):
    c = fxj[cur]["close"]
    for k in range(7):
        v = c.get(date.fromordinal(d.toordinal() - k).isoformat())
        if v:
            return v
    raise SystemExit(f"no {cur} FX near {d}")

ib_tr, ft_rows = [], []
net_buys, gross_buys = defaultdict(float), defaultdict(float)
with open(rf"{DATA}\IB.csv", newline="", encoding="utf-8-sig") as f:
    for m in csv.DictReader(f):
        sym = m["Symbol"]
        if not sym or FX_RE.match(sym):
            continue
        d = parse_d(m["TradeDate"])
        amt = float(m["TradeMoney"])
        if sym.endswith(".T"):
            amt /= fx_on("jpy", d)
        elif sym == "1810":
            amt /= fx_on("hkd", d)
        if m.get("Account") == "FT":
            ft_rows.append(m)
        else:
            ib_tr.append((d, sym, float(m["Quantity"])))
        net_buys[d] += amt
        if amt > 0:
            gross_buys[d] += amt
ib_tr.sort(key=lambda t: t[0])

holdings_by_day = []
pos = defaultdict(float)
ti = 0
for d in days[start_i:]:
    while ti < len(ib_tr) and ib_tr[ti][0] <= d:
        pos[ib_tr[ti][1]] += ib_tr[ti][2]
        ti += 1
    holdings_by_day.append(" · ".join(sorted(s for s, q in pos.items() if q > 0.001)))

# ---------------- FT daily market value (adjusted basis, raw-exit zeroing) ----
ftp = json.load(open(rf"{DATA}\prices_us.json"))
def ft_close(t, d):
    c = ftp[t]["close"]
    for k in range(15):
        v = c.get(date.fromordinal(d.toordinal() - k).isoformat())
        if v:
            return v
    return None

ft_tr = []
for m in ft_rows:
    t = m["Symbol"]
    d = parse_d(m["TradeDate"])
    q_raw = float(m["Quantity"])
    q = q_raw
    if t not in FIXED_PRICE:
        yc = ft_close(t, d)
        f_ = float(m["TradePrice"]) / yc if yc else 1.0
        if abs(f_ - 1) > 0.12:
            q *= f_
    ft_tr.append((d, t, q, q_raw))
ft_tr.sort(key=lambda x: x[0])

ndx_raw = json.load(open(rf"{DATA}\ndx.json"))
ndx_close = {k: v for k, v in ndx_raw["close"].items()}
us_days = sorted(date.fromisoformat(k) for k in ndx_close)
ft_first = ft_tr[0][0]
deploy_days = [d for d in us_days if ft_first <= d < ib_start] + days[start_i:]

ft_pos_adj, ft_pos_raw = defaultdict(float), defaultdict(float)
ft_mv, ft_hold_names = {}, {}
fi = 0
for d in deploy_days:
    while fi < len(ft_tr) and ft_tr[fi][0] <= d:
        _, t, q, q_raw = ft_tr[fi]
        if q < 0 and ft_pos_adj[t] + q < 0:
            q = -ft_pos_adj[t]
        ft_pos_adj[t] += q
        ft_pos_raw[t] += q_raw
        if q_raw < 0 and ft_pos_raw[t] <= 0.5:
            ft_pos_raw[t] = ft_pos_adj[t] = 0.0
        fi += 1
    mv = 0.0
    for t, q in ft_pos_adj.items():
        if q > 0.001:
            p = FIXED_PRICE.get(t)
            if p is None:
                p = ft_close(t, d) or 0.0
            mv += q * p
    ft_mv[d] = mv
    ft_hold_names[d] = " · ".join(sorted(t for t, q in ft_pos_raw.items() if q > 0.5))
assert ft_mv[days[-1]] < 1, "FT positions must be empty after ACAT"

# ---------------- deployed-capital sleeve (FT + IB, from 2022-05) ----------------
def sleeve_stock(d):
    return ft_mv.get(d, 0.0) + stock_mv.get(d, 0.0)

def ndx_asof(d):
    for k in range(10):
        v = ndx_close.get(date.fromordinal(d.toordinal() - k).isoformat())
        if v:
            return v
    return None

ddates = [d.isoformat() for d in deploy_days]
deploy_index, ndx_matched_index = [100.0], [100.0]
di = ni = 100.0
# dollar P&L vs matched-deployment index shadow: same buy dollars, same sell fractions
pnl_series, shadow_pnl_series = [0.0], [0.0]
shadow_units = cum_buys = cum_sells = shadow_cum_sells = 0.0
for i in range(1, len(deploy_days)):
    dp, dc = deploy_days[i - 1], deploy_days[i]
    nb = sum(v for k, v in net_buys.items() if dp < k <= dc)
    gb = sum(v for k, v in gross_buys.items() if dp < k <= dc)
    denom = sleeve_stock(dp) + gb
    if denom > 1:
        di *= 1 + (sleeve_stock(dc) - sleeve_stock(dp) - nb) / denom
        if dc.isoformat() in ndx_close and dp.isoformat() in ndx_close:
            ni *= ndx_close[dc.isoformat()] / ndx_close[dp.isoformat()]
    deploy_index.append(round(di, 4))
    ndx_matched_index.append(round(ni, 4))

    sells = gb - nb
    px = ndx_asof(dc)
    shadow_units += gb / px
    f = sells / denom if denom > 1 else 0.0
    f = min(max(f, 0.0), 1.0)
    shadow_cum_sells += f * shadow_units * px
    shadow_units *= 1 - f
    cum_buys += gb
    cum_sells += sells
    pnl_series.append(round(sleeve_stock(dc) + cum_sells - cum_buys, 2))
    shadow_pnl_series.append(round(shadow_units * px + shadow_cum_sells - cum_buys, 2))

ib_hold_by_date = dict(zip(days[start_i:], holdings_by_day))
dholdings = []
for d in deploy_days:
    parts = [h for h in (ib_hold_by_date.get(d, ""), ft_hold_names.get(d, "")) if h]
    dholdings.append(" · ".join(parts))

# ---------------- combined total-wealth series (from 2022-05-09) ----------------
ft_nodes, ft_flows, ft_hold_stmt = {}, {}, {}
with open(rf"{DATA}\FT_monthly.csv", newline="", encoding="utf-8-sig") as f:
    for m in csv.DictReader(f):
        d = date.fromisoformat(m["MonthEnd"])
        ft_nodes[d] = float(m["NAV"])
        if m["Flow"] and "internal" not in m["FlowDesc"]:
            ft_flows[date.fromisoformat(m["FlowDate"])] = float(m["Flow"])
        if m["Holdings"]:
            ft_hold_stmt[d] = m["Holdings"]
ft_nodes[date(2022, 5, 9)] = 1961.84
ft_nodes[INTERNAL_XFER_DATE] = ft_nodes[date(2024, 5, 31)] - INTERNAL_XFER_AMT

def ft_asof(d):
    if d >= date(2024, 7, 31):
        return 0.0
    best = max((k for k in ft_nodes if k <= d), default=None)
    return ft_nodes[best] if best else 0.0

c_flow = dict(ft_flows)
for k, v in flow.items():
    c_flow[k] = c_flow.get(k, 0.0) + v
c_flow[INTERNAL_XFER_DATE] = c_flow.get(INTERNAL_XFER_DATE, 0.0) - INTERNAL_XFER_AMT

def combined_nav(d):
    return nav.get(d, 0.0) + ft_asof(d)

combined_days = sorted(k for k in ft_nodes if k < ib_start) + days[start_i:]
cdates, cindex, cdd, cndx, choldings = [], [], [], [], []
cret_by_day = {}
cidx = cpeak = 100.0
prev_d = combined_days[0]
for i, d in enumerate(combined_days):
    if i > 0:
        if d <= ib_start:
            # monthly-era step: flows dated exactly on the node are end-of-period
            f_in = sum(v for k, v in c_flow.items() if prev_d < k < d)
            f_day = sum(v for k, v in c_flow.items() if k == d)
            denom = combined_nav(prev_d) + f_in
            r = (combined_nav(d) - combined_nav(prev_d) - f_in - f_day) / denom if denom > 1 else 0.0
        else:
            f_ = sum(v for k, v in c_flow.items() if prev_d < k <= d)
            denom = combined_nav(prev_d) + f_
            r = (combined_nav(d) - combined_nav(prev_d) - f_) / denom if denom > 1 else 0.0
        cret_by_day[d] = r
        cidx *= 1 + r
        cpeak = max(cpeak, cidx)
    cdates.append(d.isoformat())
    cindex.append(round(cidx, 4))
    cdd.append(round((cidx / cpeak - 1) * 100, 3))
    nc = ndx_close.get(d.isoformat())
    cndx.append(nc if nc is not None else (cndx[-1] if cndx else None))
    if d <= date(2024, 6, 20):
        h = ft_hold_names.get(d)
        if h is None:
            keys = [k for k in ft_hold_stmt if k <= d]
            h = ft_hold_stmt[max(keys)] if keys else ""
    else:
        h = ""
    parts = [x for x in (ib_hold_by_date.get(d, ""), h) if x]
    choldings.append(" ｜ ".join(parts))
    prev_d = d
b0 = next(v for v in cndx if v)
cndx = [round(v / b0 * 100, 2) if v else None for v in cndx]

cflow_markers = []
for k in sorted(c_flow):
    v = c_flow[k]
    if abs(v) < 0.01:
        continue
    ds = next((x for x in cdates if x >= k.isoformat()), None)
    if ds:
        cflow_markers.append({"date": ds, "amount": round(v, 2), "index": cindex[cdates.index(ds)]})

cmonthly = defaultdict(lambda: 1.0)
for d, r in cret_by_day.items():
    cmonthly[(d.year, d.month)] *= 1 + r
cmonthly_out = [{"y": y, "m": m, "ret": round((v - 1) * 100, 2)} for (y, m), v in sorted(cmonthly.items())]

def dollar_episodes(pnl, dts):
    # drawdown episodes on the cumulative dollar P&L curve; depth in currency
    eps, p, t, in_dd = [], 0, 0, False
    for i in range(1, len(pnl)):
        if pnl[i] >= pnl[p]:
            if in_dd:
                eps.append((p, t, i))
                in_dd = False
            p = t = i
        else:
            in_dd = True
            if pnl[i] < pnl[t]:
                t = i
    if in_dd:
        eps.append((p, t, None))
    out = []
    for p, t, rcv in eps:
        out.append({
            "depth": round(pnl[t] - pnl[p], 2),
            "peak": dts[p], "trough": dts[t],
            "recovered": dts[rcv] if rcv is not None else None,
            "days_down": (date.fromisoformat(dts[t]) - date.fromisoformat(dts[p])).days,
            "days_total": (date.fromisoformat(dts[rcv]) - date.fromisoformat(dts[p])).days if rcv is not None else None,
        })
    out.sort(key=lambda e: e["depth"])
    return out


def episodes(ix, dts):
    eps, p, t, in_dd = [], 0, 0, False
    for i in range(1, len(ix)):
        if ix[i] >= ix[p]:
            if in_dd:
                eps.append((p, t, i))
                in_dd = False
            p = t = i
        else:
            in_dd = True
            if ix[i] < ix[t]:
                t = i
    if in_dd:
        eps.append((p, t, None))
    out = []
    for p, t, rcv in eps:
        out.append({
            "depth": round(ix[t] / ix[p] * 100 - 100, 2),
            "peak": dts[p], "trough": dts[t],
            "recovered": dts[rcv] if rcv is not None else None,
            "days_down": (date.fromisoformat(dts[t]) - date.fromisoformat(dts[p])).days,
            "days_total": (date.fromisoformat(dts[rcv]) - date.fromisoformat(dts[p])).days if rcv is not None else None,
        })
    out.sort(key=lambda e: e["depth"])
    return out

# ---------------- rolling 12-month TWR vs NDX, and XIRR ----------------
def close_asof(cd, d, back=10):
    for k in range(back):
        v = cd.get(date.fromordinal(d.toordinal() - k).isoformat())
        if v:
            return v
    return None

cdd_dates = [date.fromisoformat(x) for x in cdates]
roll, roll_bench = [], []
j = 0
for i, d in enumerate(cdd_dates):
    cutoff = date.fromordinal(d.toordinal() - 365)
    if cutoff < cdd_dates[0]:
        roll.append(None)
        roll_bench.append(None)
        continue
    while j + 1 <= i and cdd_dates[j + 1] <= cutoff:
        j += 1
    roll.append(round((cindex[i] / cindex[j] - 1) * 100, 2))
    b0_, b1_ = close_asof(ndx_close, cutoff), close_asof(ndx_close, d)
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

xf = sorted([(k, -v) for k, v in c_flow.items() if abs(v) > 0.01] + [(days[-1], nav[days[-1]])])
xirr_pct = round(xirr(xf) * 100, 2)

# XIRR through time: at each sample date, all flows so far + that day's value as terminal.
# Benchmark: the same external deposits/withdrawals routed into NDX (account-level shadow).
flow_items = sorted((k, -v) for k, v in c_flow.items() if abs(v) > 0.01)
sh_units = 0.0
fi_ = 0
us_shadow_val = {}
ext_flows = sorted((k, v) for k, v in c_flow.items() if abs(v) > 0.01)
for d in combined_days:
    while fi_ < len(ext_flows) and ext_flows[fi_][0] <= d:
        k, v = ext_flows[fi_]
        sh_units += v / ndx_asof(k)
        fi_ += 1
    us_shadow_val[d] = sh_units * ndx_asof(d)

xdates, xirr_series, xirr_bench = [], [], []
first_d = combined_days[0]
for i, d in enumerate(combined_days):
    if (d - first_d).days < 365:
        continue
    if d < ib_start or i % 5 == 0 or i == len(combined_days) - 1:   # monthly nodes + weekly samples
        past = [(k, a) for k, a in flow_items if k <= d]
        xdates.append(d.isoformat())
        xirr_series.append(round(xirr(past + [(d, combined_nav(d))]) * 100, 2))
        xirr_bench.append(round(xirr(past + [(d, us_shadow_val[d])]) * 100, 2))

out = {
    "asof": days[-1].isoformat(),
    "validation": validation,
    "cdates": cdates, "cindex": cindex, "cdd": cdd, "cndx": cndx,
    "choldings": choldings, "cflows": cflow_markers, "cmonthly": cmonthly_out,
    "ctop_dd": episodes(cindex, cdates)[:5],
    "cfinal": round(cindex[-1], 2), "cmax_dd": round(min(cdd), 2),
    "ddates": ddates, "deploy": deploy_index, "ndx_matched": ndx_matched_index,
    "dholdings": dholdings,
    "roll": roll, "roll_bench": roll_bench, "xirr": xirr_pct,
    "xdates": xdates, "xirr_series": xirr_series, "xirr_bench": xirr_bench,
    "pnl": pnl_series, "shadow_pnl": shadow_pnl_series,
    "pnl_dd": dollar_episodes(pnl_series, ddates)[:5],
}
json.dump(out, open(rf"{DATA}\twr_data.json", "w"))
print("validation:", [(v["year"], v["diff"]) for v in validation])
print("asof", out["asof"], "| combined", out["cfinal"], "| max_dd", out["cmax_dd"],
      "| deploy", deploy_index[-1], "| ndx_matched", ndx_matched_index[-1])
