# What open-source trading bots taught us (survey, 2026-10-04)

A read-only survey of GitHub: READMEs and source were read through the GitHub API, and nothing was cloned or run.
The aim was to find techniques, data sources and pitfalls that matter to this bot and to `edge-scout`.

## Read this first: most "pump.fun bot" repos are bait

Searching "pump.fun sniper" or "copy trading bot" returns hundreds of repos. Most are advertising, and some are
built to steal wallets. Patterns seen:

- **Keyword-stuffed descriptions with a Telegram handle**, e.g. "solana sniper bot pumpfun sniper bot solana sniper…".
  The code is a teaser and the "full version" is sold in DMs.
- **Inflated history:** one top result (4,475 stars) is a repo "created 2013" and repurposed in 2026. Its README is a
  sales page ("message me on Telegram").
- **Drainers in issues:** Chainstack's own README warns that its Issues section is "regularly targeted by scam bots
  that try to redirect you to an external site and drain your funds".
- **"Free bot, paste your private key."** Never run one of these, and never give any of them a key.
  This bot's live mode signs locally and never sends a key anywhere.

## Worth learning from

| Repo | What it is | What we take from it |
|---|---|---|
| [chainstacklabs/pumpfun-bonkfun-bot](https://github.com/chainstacklabs/pumpfun-bonkfun-bot) (~1k★, Apache-2.0, Python) | An educational pump.fun bot from an RPC provider. Four listeners (`logs`, `blocks`, Geyser gRPC, shreds) with a tool that races them; an "extreme fast mode" with no RPC call between seeing a coin and buying it; one regression script per fixed bug; the current pump.fun IDL | **(1)** pump.fun now lists coins quoted in USDC (see "Found" below). **(2)** The listener race is the right way to price latency: our logs feed is the slowest of the four. **(3)** `programSubscribe` on bonding-curve accounts with a byte pre-filter pushes the full curve state for every coin past ~64.5%, at low bandwidth: a leaner feed for graduation plays. **(4)** Progress for Mayhem coins must use the chain's own reserves, not constants. |
| [0xfnzero/solana-streamer](https://github.com/0xfnzero/solana-streamer), [sol-trade-sdk](https://github.com/0xfnzero/sol-trade-sdk) (Rust, MIT) | Low-latency event streaming (Yellowstone gRPC, Jito ShredStream) and multi-route transaction sending | This is the stack serious snipers use. graduation-v1 already loses at a 1 s delay (BUILD_LOG #18), so going live would need this class of infrastructure *and* measured landing under 1 s. That's paid and complex; only worth it after a strategy passes on paper at that delay. |
| [UNAUTH-ACCESS/s1wave-bot](https://github.com/UNAUTH-ACCESS/s1wave-bot) (Python, 1★) | A small memecoin bot run with an AI operator, with a careful calibration handbook | The README claims "edge confirmed at 99.7% confidence". **Its own calibration notes say the opposite:** paper ("shadow") ~80% win rate and +16% mean, verified live 1 win in 38 with a −44% mean. Paper flatters, which is exactly why we model execution delay. Good practices to borrow: a source-of-truth ranking (on-chain P&L > quotes > snapshots); capped means against phantom price spikes; exit variants riding the same paper entries; regime start dates. |
| [hummingbot `v2_funding_rate_arb.py`](https://github.com/hummingbot/hummingbot/blob/master/scripts/v2_funding_rate_arb.py) (20k★) | Funding-rate arbitrage between two perp venues | A template for edge-scout's carry paper-trader: normalise funding per second, enter only if 24 h of funding beats both venues' fees, take profit on P&L *including* funding received, and exit when the funding gap flips. |
| [freqtrade](https://github.com/freqtrade/freqtrade) (55k★), [nautilus_trader](https://github.com/nautechsystems/nautilus_trader) (30k★) | The mature open-source frameworks | Freqtrade's `lookahead-analysis` and `recursive-analysis` hunt for look-ahead bias. Our event-driven replays avoid it by construction, but a "truncate the data and re-decide" check would be cheap insurance for research. Nautilus runs the same code in backtest and live, which our engine also does. |
| [Vybe top-traders / trader-PnL APIs](https://github.com/vybenetwork/solana-top-traders-api) | Wallet leaderboards per token and wallet P&L (free key) | Could seed or cross-check a future wallet-study version. It can't change wallets-v1: those rules are registered. |
| [OctagonAI/kalshi-trading-bot-cli](https://github.com/OctagonAI/kalshi-trading-bot-cli) (~400★) | An LLM writes a probability estimate, compares it with the Kalshi order book, and trades the gap | Fine for ideas. An "edge" from an LLM estimate is untested until it's calibrated against outcomes, which is edge-scout's M1 plan. |
| [defi-ape/polymarket-kalshi-arbitrage-bot](https://github.com/defi-ape/polymarket-kalshi-arbitrage-bot) and many look-alikes | "Arbitrage" between 15-minute Polymarket and Kalshi crypto markets | It buys **one side only** when Kalshi prices the outcome higher: a directional bet, not arbitrage. The two venues also settle from different price references, so "identical" markets can resolve differently. Treat any cross-venue prediction "arb" as basis risk until settlement rules are compared line by line. |

**Skipped:**
- CloddsBot (2.9k★): a 12-day hackathon "AI trading terminal" that promotes its own pump.fun token.
- Volume bots and market-maker bots: they exist to fake activity.
- Most copy-trading repos: sales pages.

## Found while surveying (checked against live data)

1. **USDC-quoted pump.fun coins.**
   - The current pump.fun `TradeEvent` carries `quote_mint` and `quote_amount` after its variable-length fields.
   - In a 20 s live sample, 357 of 369 trades were SOL-quoted (`quote_mint` all zeros, quote fields equal to the SOL fields).
   - 11 were **USDC-quoted** and 1 used another token.
   - For those, `sol_amount` and `virtual_sol_reserves` are **0**, so this bot reads them as price 0. They never reach a trading window, so nothing is bought wrongly, but about 3% of the market is invisible.
   - Fixed in BUILD_LOG #24: decode `quote_mint` and skip non-SOL coins explicitly, with a counter.
2. **ipfs.io rate-limits this machine** (HTTP 429), as does dweb.link, which public gateways redirect to.
   - pump.fun metadata lives there, so the bot's per-launch social-link lookups (X / Telegram / website) are likely failing often.
   - The logo fetcher now falls back to other gateways (pump.fun's Pinata, 4everland, public Pinata).
   - Fixed in BUILD_LOG #24: the same fallback for the social-link lookup.
3. **Latency is the gating cost, not code.** The fastest bots all use pre-execution data (shreds) or Geyser plus paid landing routes. Nothing in the open-source world changes the conclusion of BUILD_LOG #18.
