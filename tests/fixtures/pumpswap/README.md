# PumpSwap SDK fixtures (pinned)

- `pumpswap-sdk-1.20.0-golden.json`: exact integer outputs of the official `@pump-fun/pump-swap-sdk@1.20.0` for
  94 quote cases, 5 pool layouts and 2 mints. Generated offline by the external reviewer (round 9, 2026-10-07) from
  the npm tarball, whose sha512 integrity (`sha512-DuBZ5ge3…`) was checked here against the npm registry; the
  SDK source files' sha256 recorded in the JSON match the tarball's.
- `pump_amm-1.20.0.idl.json`: the IDL shipped in that tarball (sha256 53e9ae78…).

All pools, balances and fee schedules in them are synthetic: they test `meme_trader/sniper/pumpswap.py`'s arithmetic
against the SDK's, nothing about current mainnet fees or fills.
