# Research: the best "agents in a room" builds, and what we took from them

October 2026. Asked for while turning the Desk tab into a Habbo-style trading room. Every project below was read on GitHub or its docs only; none of their code was run or copied.

## Agent offices

| Project | How it's built | Worth taking |
|---|---|---|
| [Pixel Agents](https://github.com/pablodelucca/pixel-agents) (VS Code extension: Claude Code agents as pixel people) | Canvas 2D on a 64×64 tile grid. Characters run a small state machine (typing, reading, waiting). A built-in layout editor for floors, walls and furniture, with JSON import and export. | **Show what the agent is really doing** (typing when it works, reading when it waits), not random motion. A layout editor that saves to a file. |
| [Agent Virtual Office](https://github.com/KbWen/agent-virtual-office) | Pure SVG, no game engine. Animations are driven only by real signals. Each role has its own animation. Short "murmurs" instead of made-up dialogue. The room's weather follows the team's mood. Agents are renamed through config. | **Real signals only.** Every line our bots say comes from the bot's data (votes, fills, the feed's lag), never from an invented script. The room reflects state (we use time of day; they use mood). Renaming through saved config. |
| [The Office](https://github.com/shahar061/the-office) | PixiJS. Agents walk to a boardroom when work is handed from one to another. | **Show hand-offs as movement.** Our scanner carries the coin's folder to the AI table and then to the exit manager. |

## Habbo-style rooms

| Project | Worth taking |
|---|---|
| [Phaser Habbo Engine](https://github.com/Celebi-Studios/Phaser-Habbo-Engine) | The classic projection: 2:1 isometric tiles (64×32), screen x = (x − y)·32, screen y = (x + y)·16. Draw order sorted by x + y. |
| [Habbo-like multiplayer room](https://github.com/girishlade111/habbo-hotel-like-multiplayer) | Tile-by-tile walking with a path finder that never cuts corners round furniture. Chat as one row of speech per line, scrolling up and away. Camera pan and zoom. |

**What we built from it:** an SVG room in that projection (16×11 tiles). Furniture blocks tiles; the bots find their way round it tile by tile; draw order is sorted by depth so they pass in front of and behind desks; chat lines rise from above the room and fade after ~25 s; clicking zooms the camera (the SVG viewBox) into what that bot is looking at.

SVG rather than Canvas or PixiJS because the dashboard is one HTML file with no build step and no CDN: every piece of the room is a styled shape, so the light and dark themes and the night lighting are CSS, and the bots' colours and hats can be changed at runtime.

## Trading terminals (for the manual trade box)

| Source | Worth taking |
|---|---|
| [Axiom Pulse explained](https://axiompedia.com/guides/trading/axiom-pulse-explained) | Columns for New Pairs, Final Stretch (near graduation) and Migrated, each with about 14 filters: age, top-10 holders, dev holding, snipers, insiders, bundles, holders, liquidity, volume, market cap. |
| [Axiom review (Coin Bureau)](https://coinbureau.com/review/axiom-trade-review) | Quick buy with presets, limit orders, stop loss and take profit, a wallet tracker, and an X monitor. |

**What we built from it:** pasting a contract address into Manual trade now shows the metrics a trader checks before buying: price, market cap, liquidity, 1 h and 24 h volume, 1 h buys vs sells, 5 m / 1 h / 24 h change, top-10 holding, holder count, curve progress, age, dev holding, and the risk flags, refreshed every ~20 s while the coin is in the box. We already had quick buy, presets, stops and take profits, a wallet watcher (Portfolio) and an X feed.

**Not built yet, worth considering:** limit orders (buy at a price); an Axiom-style three-column "Pulse" with saved filters (the Launch radar is close to a New Pairs column; the Graduation scanner is our Final Stretch).

## What we chose not to copy

- **Big on-screen effects** (stamps, confetti, screen shake). Removed at the owner's request. A trade now shows as a small "+0.012" rising over the bot that made it, and a line in the room's chat.
- **Invented dialogue.** Idle chatter exists (coffee, the window), but anything about the market is read from the bot's data.
- **A full game engine.** Phaser or PixiJS would add a build step and ~1 MB for a room of ten characters.
