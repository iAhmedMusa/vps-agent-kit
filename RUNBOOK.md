# VPS Keeper Agent: Setup Runbook

Set up the self-healing update agent on a new Ubuntu VPS. It upgrades packages on a schedule, checks that everything still runs, fixes problems with AI (Gemini first, Groq fallback), and reports to Telegram. You control it from a Telegram menu.

Time needed: about 15 minutes.

## Kit contents

| File | Purpose |
|---|---|
| `agent.py` | The agent |
| `install.sh` | Installs to `/opt/vps-agent` and starts the systemd service |
| `vps-agent.service` | systemd unit (runs as root, auto restart) |
| `.env.example` | Config template with placeholder values. Copy to `.env` and fill in real values |
| `RUNBOOK.md` | This document |

Real keys live only in `.env` on the server. Keep them in a password manager. Never put the real `.env` in cloud storage, Git or chat.

## 1. Before you start

Have these ready:

- Ubuntu VPS (22.04 or newer), root or sudo access over SSH
- A second way into the server (provider web console, for example Hetzner Cloud Console) in case SSH breaks
- Gemini API key: https://aistudio.google.com/apikey
- Groq API key: https://console.groq.com/keys
- Telegram on your phone

## 2. Create a Telegram bot for this server

Use one bot per server. Two servers on the same bot token fight over messages.

1. In Telegram, open **@BotFather**.
2. Send `/newbot`.
3. Name: for example `<your-vps-name> VPS agent`. Username must end in `bot`.
4. Copy the token (format: `<your-telegram-bot-token>`).
5. Optional: `/setuserpic` to give it an icon.

You do not need `/setcommands`. The agent registers its Menu itself on every start.

## 3. Prepare the server

SSH in, become root, and check nothing is stuck before installing:

```bash
sudo -i
ps aux | grep -E 'apt|dpkg' | grep -v grep     # should print nothing
dpkg --audit                                   # should print nothing
```

If an `apt` process is running, wait for it to finish. If it has been running for a long time (hours or days), see **Troubleshooting: stuck apt** below.

Bring the server up to date once by hand so the agent starts from a clean state:

```bash
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get upgrade -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
ls /var/run/reboot-required 2>/dev/null && echo "REBOOT NEEDED"
```

If a reboot is needed, do the checks in **Section 8: Before any reboot**, then `reboot`, and SSH back in.

## 4. Get the kit

On the server:

```bash
apt-get install -y git
cd /root && git clone https://github.com/iAhmedMusa/vps-agent-kit.git
cd /root/vps-agent-kit
ls -la        # agent.py  install.sh  vps-agent.service  .env.example  RUNBOOK.md
```

Read `agent.py` and `install.sh` before running them as root. See the security risks in [README.md](README.md#security-risks).

## 5. Configure .env

```bash
cp .env.example .env
nano .env
```

Fill in at least:

| Key | Value |
|---|---|
| `TELEGRAM_TOKEN` | Token from step 2 |
| `OWNER_CHAT_ID` | Leave `0` for now |
| `GEMINI_API_KEY` | Your Gemini key |
| `GROQ_API_KEY` | Your Groq key |
| `SERVER_NAME` | Short name shown in Telegram, for example `<your-vps-name>` |
| `HEALTH_URLS` | Your real sites and local app ports, comma separated |

Everything else has working defaults. Save with `Ctrl+O`, `Enter`, `Ctrl+X`.

## 6. Install

```bash
sudo bash install.sh
```

The installer checks `.env`, installs Python venv if missing, copies everything to `/opt/vps-agent`, installs packages, and starts the `vps-agent` service.

Watch the log in a second terminal:

```bash
journalctl -u vps-agent -f
```

You should see `vps-agent running on <name>`.

## 7. Connect your Telegram

1. In Telegram, open your new bot, press **Start**, send `/start`.
2. Bot replies `Your chat id: 123456789`.
3. Put it in the live config and restart:

```bash
nano /opt/vps-agent/.env          # OWNER_CHAT_ID=123456789
systemctl restart vps-agent
```

4. Within a few seconds Telegram shows:

```
🟢 <name> agent online
🧠 gemini:<model> → groq:<model>
🗓 upgrade 04:00 Asia/Dhaka, watchdog every 15 min. /help
```

5. Close and reopen the chat. The blue **Menu** button now lists all commands. If not, fully quit and reopen Telegram (it caches the menu).

From now on edit config only in `/opt/vps-agent/.env`, then `systemctl restart vps-agent`. The bot ignores everyone except `OWNER_CHAT_ID`.

## 8. Verify

Run these from the Telegram Menu:

| Command | Expected |
|---|---|
| `/status` | Uptime, RAM, disk, pending updates, baseline counts, `Drift: none` |
| `/check` | `apt update` then list of available updates, or "up to date" |
| `/sh df -h` | Disk table |
| Plain text: `how much RAM is free?` | AI answer (proves Gemini/Groq keys work) |

Then save the known-good state:

1. Make sure every site and container you care about is running (`docker ps`, open your sites).
2. Send `/baseline`. The agent records running services, containers, open ports and health URLs.

The agent keeps the server matching this baseline. After you intentionally add or stop a service, send `/baseline` again, or the watchdog will try to restart it.

### Before any reboot

```bash
systemctl is-enabled docker caddy nginx tailscaled 2>/dev/null   # every service you use: enabled
systemctl is-enabled ssh.socket ssh 2>/dev/null                  # at least one enabled
docker inspect -f '{{.Name}} {{.HostConfig.RestartPolicy.Name}}' $(docker ps -q)
```

Containers must show `unless-stopped` or `always`. Fix one with:

```bash
docker update --restart unless-stopped <container>
```

If you SSH over Tailscale, keep the provider web console open while rebooting.

## 9. Daily use

**Automatic:**

- Upgrade at `UPGRADE_TIME` on `UPGRADE_DAYS`. Flow: snapshot, apt update + upgrade (keeps your config files), compare with snapshot, quick fixes (restart), AI healing if still broken, Telegram report.
- Watchdog every `HEALTH_INTERVAL_MIN` minutes. Heals drift and reports once per problem.
- Reboot is never automatic unless `AUTO_REBOOT=true`. The report says "Send /reboot yes when ready". After a reboot the agent checks health and reports.

**Menu commands:**

| Command | Does |
|---|---|
| `/status` | Health summary |
| `/check` | List available updates, install nothing |
| `/upgrade` | Run the full upgrade + heal + report now |
| `/heal` | Compare with baseline and fix now |
| `/baseline` | Save current state as known-good |
| `/logs nginx 80` | Last 80 journal lines of a service |
| `/sh <command>` | Run a shell command (safety rules apply) |
| `/reboot yes` | Reboot now, report after boot |
| `/history` | Last runs |
| `/new` | Clear chat memory |
| Any text | Ask the AI about the server |

**Safety rules:** the AI and `/sh` cannot delete system paths, remove packages, users, volumes, firewall rules or SSH access, or reboot. Before editing a file the AI makes a `.bak` copy. Every command is logged in `/var/lib/vps-agent/agent.db`.

## 10. Common changes

All in `/opt/vps-agent/.env`, then `systemctl restart vps-agent`.

| Want | Set |
|---|---|
| Groq as primary | `BRAIN_ORDER=groq,gemini` |
| New model name | `GEMINI_MODEL=...` / `GROQ_MODEL=...` |
| Weekly on Friday | `UPGRADE_DAYS=5` |
| Different time | `UPGRADE_TIME=03:30` |
| Auto reboot | `AUTO_REBOOT=true` |
| Ignore a noisy port | `IGNORE_PORTS=5000,6000` |

Change the menu text: edit the `MENU` list in `/opt/vps-agent/agent.py`, restart.

## 11. Update or remove the agent

Update with a newer kit:

```bash
cd /root/vps-agent-kit && git pull   # fetch the newer kit
cp /opt/vps-agent/.env .env          # keep live config
sudo bash install.sh
```

Remove:

```bash
systemctl disable --now vps-agent
rm /etc/systemd/system/vps-agent.service
systemctl daemon-reload
rm -rf /opt/vps-agent /var/lib/vps-agent
```

## 12. Troubleshooting

### Installer: "Could not get lock /var/lib/dpkg/lock-frontend"

Another apt is running. Find it:

```bash
ps -fp $(fuser /var/lib/dpkg/lock-frontend 2>/dev/null) 2>/dev/null
```

If it started minutes ago, wait. Do not delete the lock file.

### Stuck apt (running for hours or days)

A manual `apt upgrade` can wait forever on an interactive prompt (config file question, needrestart menu, kernel notice) if its terminal closed.

```bash
pstree -ap <apt PID>
```

- Inside tmux or screen: attach (`tmux attach` / `screen -r`), answer the prompt. Config files: keep your current version.
- Terminal gone: kill from the bottom of the tree up, for example `kill <needrestart PID>`, then `kill <sh PID>`, then `kill <apt PIDs>`. Use `kill -9` only if they ignore normal kill. Then repair:

```bash
dpkg --configure -a --force-confold
apt-get -f install -y
dpkg --audit          # empty = healthy
```

A "Pending kernel upgrade" dialog is only information. Press Enter on OK, then reboot later.

The agent itself runs apt non-interactively, so its own upgrades do not hang this way.

### "Operation inhibited by APT" on reboot

An apt process is still running. Finish or clean it (above). Do not force reboot while apt runs.

### Agent crashes: `telegram.error.BadRequest: Chat not found`

`OWNER_CHAT_ID` is wrong, or you never pressed Start on this bot. Open the bot, press Start, send `/start`, use that id. Make sure `TELEGRAM_TOKEN` is this server's bot, not another one.

### `/start` gets no reply

Wrong token, or the service is down:

```bash
systemctl status vps-agent
journalctl -u vps-agent -n 50 --no-pager
```

Check `TELEGRAM_TOKEN` in `/opt/vps-agent/.env` matches BotFather.

### AI replies "all LLM providers failed"

Keys wrong, quota hit, or model name retired. Check the log line `[llm] gemini failed: ...`. Update the model name or key in `.env`, restart.

### Menu button missing

Fully quit and reopen Telegram. Check the log for `set_my_commands failed`.

### Watchdog keeps restarting something you stopped on purpose

Send `/baseline` after the intended change.

### `ssh` shows "disabled"

Normal on newer Ubuntu: SSH starts through `ssh.socket`. Check `systemctl is-enabled ssh.socket` shows `enabled`.

### `cloud-init-hotplugd.service` failed

Common on cloud VPS and harmless. `systemctl reset-failed`.

## 13. File locations

| What | Where |
|---|---|
| Code | `/opt/vps-agent/agent.py` |
| Live config | `/opt/vps-agent/.env` (chmod 600) |
| Python env | `/opt/vps-agent/venv` |
| Service | `/etc/systemd/system/vps-agent.service` |
| Database (baseline, runs, command log) | `/var/lib/vps-agent/agent.db` |
| Logs | `journalctl -u vps-agent` |
