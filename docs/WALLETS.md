# Wallet study: does following "insider" wallets work?

A research track, separate from the graduation strategy. Nothing here trades: it records public market data
and tests one idea under rules fixed in advance.

## The idea being tested

An X article ("How to become a memecoin insider in 7 days", 2026-09-30) argues:
- don't race developers, insiders and bots on fresh launches;
- find wallets that were early, with real size, on two or more *different* coins before they ran, and that sold
  into strength;
- buy a coin at least 14 days old only when 2–3 of those wallets buy it around the same time, after it has
  survived its first big dump and while holders are still growing.

The holds last days, so our 2–3 s execution delay, which sank the graduation strategy, doesn't matter here.

The article shows no track record. Its method finds wallets by looking back at coins that already ran, which
always looks good in hindsight. So the only test that counts is: **wallets chosen with data from weeks 1–2,
frozen, then judged only on weeks 3–4.**

## The rules (registered before any data: `research/policies/wallets-v1.yaml`)

| Step | Rule |
|---|---|
| A move | Price reaches 3× a low within 48 h, with at least 10 distinct buyers between |
| Early | Bought ≥ 0.2 SOL from 60 min before the low until price first hit 1.5× the low, paying ≤ 1.5× the low |
| Listed wallet | Early on ≥ 2 different coins in period A, sold at ≥ 2× its entry at least once; not a bot (≤ 3000 trades, ≤ 300 coins); best 30 kept |
| Signal | ≥ 2 listed wallets buy (≥ 0.2 SOL) the same coin, at least 14 days old, within 6 h; one signal per coin per week |
| Gates | Fell ≥ 50% from its high on some earlier day; liquidity ≥ $10K; in the 24 h before, more distinct buyers than sellers |
| Trade | 0.5 SOL at the first trade ≥ 60 s after the signal; half off at 2×, then trail 30%; stop −50%; 7 days max; 0.30% fee per side, price impact from pool liquidity, transaction costs |
| Pass | ≥ 14 days and ≥ 30 trades in period B; P(mean > 0) ≥ 90% (day-block bootstrap); mean beats ≥ 90% of random-coin draws (a random coin that passed the same gates at the same moment) **and** ≥ 90% of random wallet lists (drawn from period A's active, non-bot wallets) |

`register` stored the rules' signature (`904875fa93aa`) in `wallets-v1.lock.json` on 2026-10-04, before the
recorder had collected anything. `select`, `freeze` and `eval` refuse to run if the rules change; a new idea
is a new file (`wallets-v2`).

## Timeline

- **Period A (selection):** from 2026-10-04, at least 14 days. `freeze` refuses earlier. If fewer than 10 wallets
  qualify, period A runs 7 more days, once.
- **Period B (test):** from the freeze, at least 14 days and 30 trades. Earliest verdict around 2026-11-01.

## Data

The recorder (`python -m meme_trader.wallets record`, user service `meme-wallets`) writes to `data/wallets/`.

**Measured constraints (2026-10-04):**
- The whole PumpSwap log stream is ~0.9 MB/s (~80 GB/day of download). The bot's own feed already uses ~0.27 MB/s.
- PublicNode silently drops most per-pool subscriptions, and throttled this IP after a few dozen; the bot's feed
  failed over to the public endpoint at the same time (it was fine, at 1.2 s lag).
- GeckoTerminal's free API sustains ~5 calls/min. At 6 s spacing, every other call is refused.
- Coins under a day old carry ~91% of PumpSwap trades. Coins 14+ days old carry ~3%, on ~60 pools active in any
  given minute.

**So, `mode: poll` (the default):**
1. Every 15 min, sample the whole stream for 45 s (one PublicNode connection, ~3.5 GB/day) to see which pools trade.
2. Look up each new pool's age, liquidity and coin on DexScreener.
3. Poll GeckoTerminal for the latest trades of pump.fun coins' pools that are ≥ 2 days old with ≥ $5K liquidity.
   Each pool is polled as often as its trade rate needs, within the 5 calls/min budget. A busy pool can outrun
   it: an overflowing call is a gap, and `status` shows the estimated coverage.
4. Fetch daily candles for pools nearing 14 days old (for the "survived its first dump" gate).

`mode: stream` records every swap from the full stream instead: complete data at ~80 GB/day of download.
Switch to it only if your internet plan has no data cap.

## Commands

```
python -m meme_trader.wallets status                # what the recorder has collected, coverage, errors
python -m meme_trader.wallets select wallets-v1     # period A so far: moves found, wallets qualifying
python -m meme_trader.wallets freeze wallets-v1     # after 14 days: lock the list
python -m meme_trader.wallets eval wallets-v1       # period B: signals, trades, baselines, verdict
```

## Known limits

- **Not full coverage in poll mode.** The busiest pools can outrun the free API, so some trades and wallets are
  missed. Every arm (listed wallets, random coins, random wallets) sees the same data, so the comparison stays
  fair, but absolute numbers are approximate. Stream mode removes this.
- **Price impact** comes from one liquidity snapshot (≤ 6 h old). Exits are priced at recorded trade prices, so
  a crash between two recorded trades fills at the next one.
- **"Holders growing"** is a trade-flow proxy, not an on-chain holder count.
- **Wallet identity** is the transaction's sender. Wallets that split activity across addresses look like
  several small wallets; the article's "follow the funding path" step isn't modelled.
