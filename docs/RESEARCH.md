# Research: finding out whether a strategy really makes money

The bot hasn't shown a trading edge yet. Paper profits from a few dozen trades say little: they can come from a handful of lucky tokens, from cost assumptions that are too kind, or from settings picked because they looked good on the same data. This page is the process for finding out properly, and for rejecting an idea when it doesn't hold up.

## The rules

1. **One strategy, written down first.** A policy file in [`research/policies/`](../research/policies/) states:
   - why the strategy should make money;
   - which tokens qualify;
   - the exact entry and exit rules, and the size;
   - every cost assumption;
   - the pass/fail gates.

   Its settings apply on top of `config/params.example.yaml`, never your local `params.yaml`, so anyone gets the same result.
2. **Freeze before judging.** `research freeze <policy>` stamps the policy with a signature of its settings, gates and cost assumptions, plus the time. It also saves the complete settings snapshot (`<policy>.lock.json`); a frozen policy always replays with exactly that, so settings added to the bot later can't change it. From then on:
   - only data recorded **after** that moment counts as the holdout;
   - development runs only read data from before it;
   - changing anything means a new version (`graduation-v2`) with its own, later, holdout.
3. **The holdout can't be peeked at.** `research final <policy>` shows nothing until the holdout has the policy's minimum days and trades. Then it judges once against the gates and stores the verdict.
4. **Every run is logged** in `data/research/experiments.jsonl`. The report says how many settings were tried on the same data, because the best of many tries always looks better than it is.
5. **Results are net and honest.**
   - **Costs:** P&L is after the pump.fun fee, the PumpPortal fee, an assumed adverse fill, and priority plus base fees on every transaction.
   - **Units:** P&L is in SOL, so it's already measured against just holding SOL. Operating costs (RPC, hosting, AI) are taken off per day.
   - **Uncertainty:** it's estimated by resampling whole days, since trades on the same day share a market and aren't independent draws.

## Commands

```bash
scripts/start.sh research data                    # what's recorded: hours, trades, lag, fees, feed health
scripts/start.sh research eval graduation-v1      # development report (add --quick for the policy alone)
scripts/start.sh research freeze graduation-v1    # lock it; the holdout starts now
scripts/start.sh research final graduation-v1     # the verdict, once the holdout is big enough
scripts/start.sh research log                     # every experiment so far
```

`eval` replays the recordings on all CPU cores. A day of recordings takes about a minute per variant. The report covers:

| Section | Question it answers |
|---|---|
| Result | Net SOL and USD, per trade, win rate, profit factor, return on money traded, after operating costs |
| Uncertainty | Mean P&L per day (or per 4 h while there are fewer than 5 days), 90% interval, chance it's really above zero |
| Winners | How much is left after removing the best 1 / 3 / 5 / 10 trades |
| Slippage | Net P&L if fills are 0–8% worse than signal per side; the break-even point |
| Size | The same strategy at $5 to $250 per trade, with fixed fees, price impact and the bot's own liquidity cap |
| Timing | The same strategy with candidates checked every 1 s and every 3 s instead of 2 s: a real edge shouldn't hinge on that |
| Baseline | Every token in the same window with no momentum filter: do the filters add anything? |
| Random | Do the rules beat picking the same number of trades at random from the baseline? |
| Ablations | Each filter removed on its own, and the Mayhem agent counted as demand |

## What the recordings capture

Every trade has:
- the time we received it, and its **on-chain time and slot**, so feed delay and chain order are measurable;
- the **fees actually charged** (protocol and creator, in basis points);
- the trading wallet.

Once a minute a **health record** notes the feed's endpoint, missing-trade rate, delay, whether entries were blocked, and the SOL price. Replays pause entries wherever the live bot couldn't trust its data, and they price results in USD.

Pump.fun's Mayhem-mode agent wallet (`BwWK17…`) trades about 22% of everything. Its trades move the price, but they never count as demand, as buyers or as holders (`sniper.market.non_organic_wallets`). Mayhem tokens mint 2 billion tokens instead of 1 billion, and their market cap uses the real supply.

## Status (2026-10-03)

`graduation-v1` is frozen. Its holdout is the recordings from that moment on, and its gates need 14 days and 150 trades.

On the 16 hours of development data before it (laggy in places, see BUILD_LOG #16):
- **Result:** 201 trades, +1.55 SOL at $20 a trade (+7.8% on money traded).
- **Biggest winners:** +0.63 SOL without the best 3 trades, −0.72 SOL without the best 10.
- **Slippage:** breaks even at 6.2% adverse fill per side.
- **Filters:** they beat the no-filter baseline (−0.74 SOL) and 100% of random picks.
- **Timing:** checking every 3 s instead of 2 s cut the profit to +0.13 SOL.

So the edge, if there is one, depends heavily on entering quickly. Real fills are slower than this model assumes, which makes measured execution delay the deciding next step.

These development numbers are a starting point, not evidence. The holdout decides.
