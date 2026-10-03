# Ubuntu setup: a home mini PC running 24/7

This guide turns the mini PC into an always-on bot box that you can check from your Mac or phone.
Tested on **Ubuntu 24.04 LTS**. The setup script was run end-to-end on a fresh Ubuntu 24.04 machine.

> **The short version:** install Ubuntu 24.04, open Terminal, paste the lines in step 2, put your key in `.env`, then run `bash scripts/setup_ubuntu.sh --service`. Done: it runs 24/7 and restarts itself.

![What you'll type in Terminal](img/ubuntu-commands.png)

---

## 0. Install Ubuntu on the mini PC (skip if it's already on there)
1. On your Mac, download **Ubuntu 24.04 LTS Desktop** from ubuntu.com/download/desktop.
2. Write it to a USB stick (8 GB+) with **balenaEtcher** (etcher.balena.io).
3. Plug the stick into the mini PC. Power it on while tapping the boot-menu key. On mini PCs this is usually **F7, F11, F12 or Esc**; check the mini PC's manual if none of those work. Pick the USB stick.
4. Choose **Install Ubuntu**, then *Erase disk and install* (this wipes Windows), and create your user.
   - Tip: name the computer `botbox`. That makes it reachable from the Mac as `botbox.local`.

## 1. Open Terminal
Press **Ctrl + Alt + T**.

## 2. Download and install the bot
```bash
sudo apt update && sudo apt install -y git
cd ~
git clone https://github.com/m3n4ce-dev/meme_traderv1.git
cd meme_traderv1
bash scripts/setup_ubuntu.sh
```
The script:
- installs anything missing (Python venv, pip, curl). It asks for your password once.
- installs the bot's packages into `.venv`;
- creates `.env`;
- runs the checker.

This is the real output from a fresh Ubuntu 24.04 machine:

![setup output](img/ubuntu-setup-output.png)

*Captured on a cloud test machine with no access to pump.fun, which is why the PumpPortal websocket and RPC rows fail there. At home they show OK. *

## 3. Add keys (optional)
Paper trading on the real market needs **no keys**. Keys only add extras: the AI desk (`ANTHROPIC_API_KEY`), phone alerts (Telegram), and live trading (step "Going live" below).

If you already set up the Mac, just copy its `.env` across. Run this **on the Mac**:
```bash
scp ~/meme_traderv1/.env you@botbox.local:~/meme_traderv1/.env
```
Or type the keys in on the mini PC:
```bash
nano .env
```
![Editing .env in nano](img/edit-env.png)

Paste each key after its name, for example `ANTHROPIC_API_KEY=` (right-click → Paste, or **Ctrl + Shift + V**). Then **Ctrl + O**, **Enter** to save, and **Ctrl + X** to exit.

Check:
```bash
scripts/start.sh doctor
```
For real-market paper trading, `PumpPortal websocket` and `trade logs` must say `[ OK ]`. No key is needed: the PumpPortal key is only for the optional metered trade stream (`sniper.feed.trades: pumpportal`).

## 4. Try it by hand first
```bash
scripts/start.sh demo     # simulated market; Firefox opens the dashboard
scripts/start.sh paper    # real market, fake money
```
The dashboard is at **http://127.0.0.1:8787**. Press **Ctrl + C** in the Terminal to stop.

![Dashboard](dashboard.png)

## 5. Run it 24/7 as a service
```bash
bash scripts/setup_ubuntu.sh --service
```
This installs `meme-sniper` as a system service. It **starts at boot** and **restarts itself if it crashes**. It runs **paper trading** until you deliberately switch it to live.

| Do this | Command |
|---|---|
| Is it running? | `sudo systemctl status meme-sniper` |
| Watch its live log | `journalctl -u meme-sniper -f` (Ctrl + C to stop watching) |
| Restart (after editing `.env` or settings) | `sudo systemctl restart meme-sniper` |
| Stop | `sudo systemctl stop meme-sniper` |
| Don't start at boot any more | `sudo systemctl disable --now meme-sniper` |

**Stop the PC from sleeping.** The bot can't trade while the PC is asleep:
```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```
Or go to Settings → Power → set **Automatic Suspend** to **Off**.

**No sudo?** Run it as a user service instead. It works the same: starts at boot, restarts on crash.
```bash
mkdir -p ~/.config/systemd/user
sed -e "s|__ROOT__|$PWD|g" -e '/^User=/d' -e 's|multi-user.target|default.target|' deploy/meme-sniper.service \
  > ~/.config/systemd/user/meme-sniper.service
systemctl --user daemon-reload && systemctl --user enable --now meme-sniper
loginctl enable-linger "$USER"     # keep it running when you're logged out
```
Then use `systemctl --user ...` and `journalctl --user -u meme-sniper -f` in place of the `sudo` commands above.

## 6. Check on it from your Mac
On the mini PC, one time:
```bash
sudo apt install -y openssh-server
```
Then on the **Mac**, open Terminal and run:
```bash
ssh -L 8787:127.0.0.1:8787 you@botbox.local
```
Leave that window open, and browse to **http://127.0.0.1:8787** on the Mac. You'll see the mini PC's dashboard.

Why the tunnel: the dashboard only listens on the mini PC itself, because its buttons can sell positions. The SSH tunnel is the safe way in. Don't open port 8787 on your router.

## 7. Bring your Mac's recorded data across (optional)
Run this on the **Mac**:
```bash
scp ~/meme_traderv1/data/feed-* you@botbox.local:~/meme_traderv1/data/
```
Backtests and the `leaders` command on the mini PC then cover both machines' recordings.

## Updating to my latest changes
```bash
cd ~/meme_traderv1
git pull
bash scripts/setup_ubuntu.sh
sudo systemctl restart meme-sniper
```

## Going live (later, after reviewing paper results together)
Follow [SETUP.md step 5](SETUP.md#5-going-live-real-money): dedicated bot wallet, paid RPC, `MEME_TRADER_CONFIRM_LIVE=yes`. Then, for the service:
```bash
sudo sed -i 's|sniper run$|sniper run --live|' /etc/systemd/system/meme-sniper.service
sudo systemctl daemon-reload && sudo systemctl restart meme-sniper
```

## Troubleshooting
| Problem | Fix |
|---|---|
| `Permission denied` running a script | `chmod +x scripts/*.sh` |
| `botbox.local` not found from the Mac | Use the mini PC's IP instead. Find it on the mini PC with `hostname -I` |
| Service keeps restarting | `journalctl -u meme-sniper -n 50` shows the error. Usually a missing key in `.env` |
| "Address already in use" | The service is already running. `sudo systemctl stop meme-sniper` before running `start.sh` by hand |
| Want to run the demo while the service runs | `scripts/start.sh demo --port 8788` and open http://127.0.0.1:8788 |
