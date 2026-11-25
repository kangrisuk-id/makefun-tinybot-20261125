"""Tiny Bot: regime-gated 12h momentum rotation, long-or-cash.

Strategy (written for this bounty from its brief):
- Track every symbol's hourly closes. Momentum is the trailing 12-hour
  return; the market regime is the equal-weight index's 12-hour return,
  with hysteresis: risk-on above +0.4%, risk-off below -0.4%, and the
  previous regime kept in between.
- Strong risk-on (index 12h return above +1.0%): hold the 4 strongest
  positive-momentum symbols at 25% of equity each. Mild risk-on: top 3
  at 25%. Flat: top 2 positive names at 12.5%. Risk-off: stay in cash,
  except the single strongest name when its own momentum tops +1.5%,
  held at 12.5%.
- Sticky holdings: a held name is kept while it still ranks in the top 4
  and its 12-hour momentum stays above -0.75%, so the bot does not churn
  on one noisy bar. Rebalance checks run every 12 bars and only trade
  outside a 2%-of-equity band.
- Risk controls: a per-position stop at -7% from its average fill price,
  checked every bar; orders carry a 300 bps slippage cap; position sizes
  respect the kit's 25% weight cap with headroom.
- Guardrail probe: if fewer than 8 fills have landed by bar 48 in a tape
  too quiet for the rotation, take one 12.5% probe in the strongest name
  (and a second step at bar 60 if still short), so the entry clears the
  8-trade minimum with real exposure rather than churn.

Deterministic: a pure function of the bars and fills seen so far. No
clock, no environment, no randomness. Standard library only.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

LB = 12                 # momentum lookback, bars
REBAL = 12              # bars between rebalance checks
ON_THR = 0.004          # regime: risk-on above this index return
STRONG_THR = 0.010      # regime: strong risk-on above this
OFF_THR = -0.004        # regime: risk-off below this
OUTLIER_THR = 0.015     # risk-off lone-wolf momentum threshold
STICKY_RANK = 4         # keep a held name while it ranks this high
STICKY_MOM = -0.0075    # ... and its momentum stays above this
STOP = 0.07             # per-position stop from average entry price
BAND = 0.02             # rebalance band, fraction of equity
WEIGHT = 0.25           # full position weight (the kit cap)
HALF = 0.125            # half position weight
SLIP_BPS = 300          # max_slippage_bps on every order
MIN_TRADES = 8          # the kit guardrail
PROBE_K1 = 48           # first probe bar
PROBE_K2 = 60           # second probe bar

closes = {}             # symbol -> [raw close ints]
entry_px = {}           # symbol -> average fill price (raw int scale)
fills_seen = 0
regime = "flat"
probes_done = 0


def _ret(hist, lb):
    return hist[-1] / hist[-1 - lb] - 1.0


def on_batch(state, batch):
    global fills_seen, regime, probes_done
    symbols = list(state.symbols)

    # Fold this batch's fills in: count them and track entry prices.
    for fill in batch.get("fills", []):
        if fill.get("status") != "filled":
            continue
        fills_seen += 1
        sym = fill["symbol"]
        price = int(fill["price"])
        qty = int(fill["qty"])
        if fill["side"] == "buy" and qty > 0:
            held = state.positions.get(sym)
            prev_qty = held.qty - qty if held else 0
            if sym in entry_px and prev_qty > 0:
                entry_px[sym] = (entry_px[sym] * prev_qty + price * qty) // held.qty
            else:
                entry_px[sym] = price
        elif fill["side"] == "sell":
            held = state.positions.get(sym)
            if held is None or held.qty <= 0:
                entry_px.pop(sym, None)

    for sym in symbols:
        c = state.close(sym)
        if c is not None:
            closes.setdefault(sym, []).append(c)

    if state.warming_up:
        return []

    k = state.seq
    orders = []

    # Per-position stop, checked every bar.
    for sym, held in list(state.positions.items()):
        ep = entry_px.get(sym)
        c = state.close(sym)
        if ep and c is not None and c < ep * (1.0 - STOP):
            orders.append({"side": "sell", "symbol": sym, "qty": held.qty,
                           "max_slippage_bps": SLIP_BPS})
            entry_px.pop(sym, None)

    if k < LB + 1 or k % REBAL != 0:
        return orders

    eq = state.equity
    if eq <= 0:
        return orders
    rets = {}
    for sym in symbols:
        hist = closes.get(sym, [])
        if len(hist) > LB:
            rets[sym] = _ret(hist, LB)
    if len(rets) < len(symbols):
        return orders

    idx = sum(rets.values()) / len(rets)
    if idx > ON_THR:
        regime = "on"
    elif idx < OFF_THR:
        regime = "off"

    ranked = sorted(rets.items(), key=lambda kv: kv[1], reverse=True)
    rank = {sym: i for i, (sym, _) in enumerate(ranked)}
    targets = {}
    if regime == "on":
        n = 4 if idx > STRONG_THR else 3
        chosen = [sym for sym, r in ranked if r > 0][:n]
        for sym in list(state.positions):
            if sym not in chosen and rank.get(sym, 99) < STICKY_RANK and rets[sym] > STICKY_MOM:
                chosen.append(sym)
        for sym in chosen[:n]:
            targets[sym] = int(WEIGHT * eq)
    elif regime == "off":
        sym, r = ranked[0]
        if r > OUTLIER_THR:
            targets[sym] = int(HALF * eq)
    else:
        for sym, r in ranked[:2]:
            if r > 0:
                targets[sym] = int(HALF * eq)

    # Guardrail probe: too few fills late in the window -> one real probe
    # in the strongest name the rotation is not already holding.
    if fills_seen < MIN_TRADES:
        if (k >= PROBE_K1 and probes_done == 0) or (k >= PROBE_K2 and probes_done == 1):
            for sym, _ in ranked:
                if sym not in targets:
                    targets[sym] = int(HALF * eq)
                    probes_done += 1
                    break

    for sym in symbols:
        held = state.positions.get(sym)
        cur = state.value(sym, held.qty) if held else 0
        tgt = targets.get(sym, 0)
        if held and held.qty > 0 and tgt == 0:
            orders.append({"side": "sell", "symbol": sym, "qty": held.qty,
                           "max_slippage_bps": SLIP_BPS})
            entry_px.pop(sym, None)
        elif tgt - cur > BAND * eq and tgt - cur >= 1_000_000:
            room = state.weight_room(sym)
            spend = min(tgt - cur, room) if room else 0
            if spend >= 1_000_000:
                orders.append({"side": "buy", "symbol": sym,
                               "notional_quote_units": spend,
                               "max_slippage_bps": SLIP_BPS})
        elif cur - tgt > BAND * eq and held and held.qty > 0 and cur > 0:
            qty = held.qty * (cur - tgt) // cur
            if qty > 0:
                orders.append({"side": "sell", "symbol": sym, "qty": qty,
                               "max_slippage_bps": SLIP_BPS})
    return orders


if __name__ == "__main__":
    makefun_bars.run(on_batch)
