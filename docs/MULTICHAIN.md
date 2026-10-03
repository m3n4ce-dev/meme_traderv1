# Cross-chain support: feasibility, work and costs

*Assessed 2026-10-03. Status: **not started, on hold** until the Solana bot proves an edge (see "Go / no-go" below). Tracking issue: [#5](https://github.com/m4n3ce/meme_traderv1/issues/5).*

## Verdict

**Yes, the bot can be made cross-chain, and about half the code carries over unchanged.** The strategy is portable to any launchpad that uses a **bonding curve that graduates to a DEX pool**. That fits four.meme (BNB Chain) and Pons (Robinhood Chain). It doesn't fit Zora or Clanker on Base, where tokens start directly in a Uniswap v4 pool with launch taxes.

Each new chain needs its own data feed, curve model, trading code, wallet type and safety checks. Expect **about a day for a one-time refactor, plus 2–3 days of build and at least 3 days of paper trading per chain**. Running costs can be **$0 to ~$50 a month per chain**, plus that chain's trading budget.

## Candidate chains (measured 2026-10-03, ~01:00–02:00 CDT)

| | pump.fun (Solana), today | four.meme (BNB Chain) | Pons (Robinhood Chain) | Zora / Clanker (Base) |
|---|---|---|---|---|
| Launch model | Bonding curve → PumpSwap | Bonding curve → PancakeSwap (~18–24 BNB) | Bonding curve → locked Uniswap v4 pool (4.2 ETH) | Straight into Uniswap v4, launch tax decays |
| Fits the strategy? | Yes | **Yes** | **Yes** | No |
| Launches / min | ~26–29 | ~7 | ~1.3 | — |
| Trades / min | ~1,000–2,700 | **~13** | **~47+** (counting only the last hour's launches) | — |
| Free real-time data | Public Solana RPC, `logsSubscribe` | Free websocket endpoints (Blockmachine, PublicNode), `eth_subscribe` logs: **tested OK** | Public HTTP RPC, `eth_getLogs` polling (0.1 s blocks): **tested OK**. No websocket. | — |
| Trading fee | 1.25% curve + 0.5% PumpPortal | 1% (min 0.001 BNB) | 1% pool fee | — |
| Network fee per trade | ~0.001 SOL priority | ~$0.01 (0.05 gwei) | ETH gas on an L2; not measured yet | — |
| Quote currency | SOL | BNB | WETH | — |
| Chain-specific risks | Insider bundles | Tax tokens; "X Mode" anti-snipe fees in early blocks; honeypot-style contracts | Only the creator can buy for the first 2 blocks; max 5% of supply per wallet; chain launched July 2026 | — |

Volume moves fast in this market. four.meme has gone from ~$5M to $400M+ a day within a week before. **Re-measure before building.** The probe scripts used for these numbers took 1–2 minutes each.

## How portable the code is

| Area | Files | Portable? | Work per chain |
|---|---|---|---|
| Analytics, bootstrap, report | `analytics.py`, `report.py` | Yes | Label units (SOL → quote currency) |
| Prediction model, sweep, compare | `predictor.py`, `features.py`, `sweep.py`, `compare.py` | Yes | Retrain per chain on that chain's recordings |
| Dashboard | `ui/` | Mostly | Chain badge, quote-currency labels, explorer links |
| Token tracker | `tracker.py` | Mostly | Uses a pluggable curve model |
| Strategies and gates | `strategy.py`, `sizing.py` | Mostly | Thresholds in SOL (e.g. `min_net_flow_sol: 3.0`) become per-chain or USD-based; add chain-specific gates (tax / honeypot) |
| Engine | `engine.py` | Partly | SOL price feed, SOL-denominated balances, pump-specific hooks |
| **Curve math** | `curve.py` | **No** | New model per launchpad (supply, virtual reserves, graduation threshold) |
| **Market data** | `feeds.py` | **No** | New feed per chain (event decoding, launch detection) |
| **Trading** | `execution.py`, `wallet.py` | **No** | EVM: build, sign and send router calls with nonce and gas management (`web3.py`) |
| **Insider funding graph** | `funding.py` | **No** | EVM version: first funder of each wallet |
| Copy trading | `copytrade.py` | Partly | Drop the `pool == "pump"` assumption |

## Plan

**Phase 0: chain adapter refactor (one time, about 1 day).**
- Put a `Chain` interface around the five non-portable pieces: `Feed`, `CurveModel`, `Executor`, `Wallet` and `FundingLookup`, plus the quote currency and its USD price.
- Make Solana/pump.fun the first adapter, with no behaviour change. The existing tests must keep passing, and a backtest on the same recording must give identical results.
- Run **one engine process per chain**, each with its own config, budget, data folder and dashboard port. That avoids a cross-chain portfolio manager, and a problem on one chain can't touch another.

**Phase 1: first EVM chain (2–3 days build, then 3+ days paper).**
1. A data feed recording launches and trades. Check completeness with a reserve-chain test like the one used for Solana (0 gaps in 10,828 trades).
2. A curve model checked against on-chain reserves.
3. Paper trading with chain-specific gates: tax/honeypot detection and anti-snipe windows.
4. Only after paper results justify it: the live executor (`web3.py`), a dedicated wallet, and `doctor` checks.

**Phase 2: further chains.** Repeat Phase 1, reusing the EVM executor and wallet. Each extra EVM launchpad is cheaper than the first.

## Costs

| Item | Cost | Notes |
|---|---|---|
| Market data | **$0** on public endpoints | Best-effort. A reliable paid RPC is typically ~$50/month per chain on entry plans (Helius' Developer tier is $49/month; BNB and Robinhood Chain providers are similar, not quoted here). |
| Hosting | $0 | Runs on the same mini PC, one process per chain |
| Trading fees | 1% per trade (four.meme, Pons) | Comparable to pump.fun's 1.75% |
| Network fees | ~$0.01/trade on BNB Chain | Robinhood Chain not measured yet |
| Capital | Per chain | A dedicated wallet with the trading budget plus its gas token (BNB or ETH) |
| Development | Claude Code sessions | Phase 0 about 1 day, then 2–3 days per chain, plus review. This uses Claude usage; the bot itself uses none while running. |
| New dependency | `web3.py` | For EVM signing and contract calls |

## Go / no-go

Start Phase 0 only when **all** of these are true:
1. The Solana bot has **≥ 100 closed paper trades** with positive expectancy, and the Analytics edge confidence is **≥ 90%**.
2. The edge holds in a `compare` / `sweep` run across several recorded days, not just one session.
3. A target chain shows **sustained volume**, roughly ≥ 300 trades/min on its launchpad, re-measured on 3 separate days.

If (1) or (2) fails, improve the Solana strategy first. Adding chains multiplies a strategy's results, including losses.

## Sources
- [CoinGecko: Memecoin launchpad wars (2026-09-08)](https://www.coingecko.com/learn/memecoin-launchpad-wars-pumpfun-stonkfun-ponsfamily)
- [Pons docs](https://docs.ponsfamily.com/) · [Bitquery: Pons API](https://docs.bitquery.io/docs/blockchain/robinhood/pons-api/)
- [Bitquery: Four Meme API](https://docs.bitquery.io/docs/blockchain/BSC/four-meme-api/) · [four.meme: how it works](https://four-meme.gitbook.io/four.meme/guide/how-it-works) · [four.meme fees (CryptoSlate)](https://cryptoslate.com/launchpads/four-meme-review/)
- [BscScan gas tracker](https://bscscan.com/gastracker) · [Blockmachine free BNB RPC](https://blockmachine.io/bnb-chain-rpc)
- [Helius plans](https://www.helius.dev/docs/billing/plans)
- [Clanker v4 guide](https://pool.fans/clank) · [Best launchpads on Base 2026](https://trustswap.com/base/best-launchpads)
