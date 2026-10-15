# VPS Keeper Agent

A self-healing update agent for a single Ubuntu VPS. It runs `apt upgrade` on a schedule, checks that your services, containers, ports and sites still match a saved known-good baseline, asks an LLM (Gemini first, Groq as fallback) to fix what broke, and reports everything to a Telegram bot that only you can use.

One Python file (`agent.py`), one systemd service, one `.env`. Setup takes about 15 minutes. See [RUNBOOK.md](RUNBOOK.md) for the full walkthrough.

> **Read [Security risks](#security-risks) before installing.** The agent runs as **root** and executes shell commands chosen by an LLM or typed into Telegram.

## Why this exists

A small VPS running a few Docker apps, a reverse proxy and a VPN rarely has a dedicated admin. Two things go wrong over time:

1. **Updates get skipped** because "something might break", so the server drifts behind on security patches.
2. **Updates get applied by hand** and break something quietly: a container with no restart policy does not return after a reboot, a config prompt hangs `apt` for days, or a port stops listening and nobody notices until a user complains.

This agent makes updates routine and adds the check a human would do afterwards:

- Snapshot services, containers, listening ports and your health URLs **before** the upgrade.
- Upgrade non-interactively, keeping your existing config files (`--force-confold`).
- Compare with the snapshot. Restart what stopped. If that is not enough, hand the problem to the LLM with a step limit.
- Message you on Telegram with what changed, what it fixed and what still needs you.

## When to use it

Good fit:

- A personal, hobby, lab or small-team VPS (Ubuntu 22.04 or newer) where an hour of downtime is acceptable.
- Servers where you already have a second way in, such as the provider web console.
- Setups you can describe with systemd services, Docker containers and a few HTTP health URLs.

## When not to use it

- **Production systems with uptime or compliance requirements.** Use staged rollouts, configuration management and human change approval.
- **Servers holding regulated or highly sensitive data.** Command output and logs are sent to third-party LLM APIs.
- **Multi-user servers.** One Telegram chat ID is a root-equivalent credential.
- **Anything without backups or provider snapshots.** The agent can only heal what it understands; it will not restore lost data.
- **Non-Debian systems.** It uses `apt`, `dpkg` and systemd.

## What it does

| Feature | Behaviour |
|---|---|
| Scheduled upgrade | At `UPGRADE_TIME` on `UPGRADE_DAYS` (default 04:00 daily, timezone `TZ_NAME`). Skips if free space on `/` is below `MIN_FREE_GB`. |
| Baseline | `/baseline` records running services, containers, open ports and health URL results. This is "known good". |
| Watchdog | Every `HEALTH_INTERVAL_MIN` minutes (default 15) compares the server to the baseline and heals drift. Each problem is reported once. |
| Quick heal | Restarts failed or stopped units first, with no LLM call. |
| AI heal | If still broken, the LLM gets a `run_command` tool and a `check_health` tool, up to `MAX_HEAL_STEPS` (default 15). It is told to copy a file to `.bak` before editing it. |
| Reboot | Never automatic unless `AUTO_REBOOT=true`. Otherwise the report says "Send /reboot yes when ready", and the agent verifies health after boot. |
| Audit log | Every command, including blocked ones, is written to SQLite at `/var/lib/vps-agent/agent.db`. |

## Quick start

Requirements: Ubuntu 22.04+, root SSH access, a Gemini and/or Groq API key, a Telegram account.

```bash
# 1. On Telegram: @BotFather -> /newbot -> copy the token (one bot per server)

# 2. On the server, as root
git clone https://github.com/iAhmedMusa/vps-agent-kit.git
cd vps-agent-kit
cp .env.example .env
nano .env        # set TELEGRAM_TOKEN, GEMINI_API_KEY and/or GROQ_API_KEY, SERVER_NAME, HEALTH_URLS

# 3. Install and start
sudo bash install.sh
journalctl -u vps-agent -f      # expect: vps-agent running on <name>
```

Then:

1. Open your bot in Telegram, press **Start**, send `/start`. It replies with your chat ID.
2. Put that number in `OWNER_CHAT_ID` in `/opt/vps-agent/.env` and run `systemctl restart vps-agent`.
3. Make sure every site and container you care about is running, then send `/baseline`.

Full steps, pre-reboot checks and troubleshooting are in [RUNBOOK.md](RUNBOOK.md).

## Telegram commands

Only the chat matching `OWNER_CHAT_ID` is answered.

| Command | Does |
|---|---|
| `/status` | Uptime, RAM, disk, pending updates, drift versus baseline |
| `/check` | `apt update` and list available updates, installs nothing |
| `/upgrade` | Run the full upgrade, heal and report now |
| `/heal` | Compare with baseline and fix now |
| `/baseline` | Save the current state as known-good |
| `/logs nginx 80` | Last 80 journal lines of a service |
| `/sh <command>` | Run a shell command (blocklist applies) |
| `/reboot yes` | Reboot now, report after boot |
| `/history` | Recent runs |
| `/new` | Clear chat memory |
| Any text | Ask the LLM about the server |

## Configuration

Everything lives in `/opt/vps-agent/.env`. Restart with `systemctl restart vps-agent` after changes. All keys are documented in [`.env.example`](.env.example). The ones that matter most:

| Key | Default | Note |
|---|---|---|
| `TELEGRAM_TOKEN` | none | Required. One bot per server. |
| `OWNER_CHAT_ID` | `0` | Set after the first `/start`. |
| `GEMINI_API_KEY` / `GROQ_API_KEY` | none | At least one required. |
| `BRAIN_ORDER` | `gemini,groq` | First is primary, the rest are fallbacks. |
| `UPGRADE_MODE` | `upgrade` | `full-upgrade` can also install new dependencies and kernels. |
| `AUTO_REBOOT` | `false` | Leave off unless every service survives a reboot. |
| `AUTOREMOVE` | `false` | Leave off. |
| `HEALTH_URLS` | example values | Replace with your real sites. This is the best signal that apps work. |

## Security risks

This tool trades safety margin for convenience. Know what you are accepting.

1. **Root, driven by an LLM.** The service runs as root (`vps-agent.service`). The LLM chooses shell commands during healing. A wrong answer, a hallucinated flag or a prompt injection can do real damage.
2. **Prompt injection through server data.** Logs, service output and web responses are fed to the LLM. Anyone who can write a line into a log your services print (for example a crafted HTTP User-Agent) can try to steer the model.
3. **The command filter is a blocklist, not a sandbox.** `agent.py` rejects commands matching regexes for `rm` on system paths, `mkfs`, `dd of=/dev/…`, reboot or shutdown, stopping `ssh`/`sshd`/`vps-agent`, user and password changes, edits to `sshd_config`, `authorized_keys` and `sudoers`, firewall flushes, `apt remove/purge/autoremove`, `curl | sh`, Docker prunes and volume removal, and `crontab -r`. It is easy to get around: wrapping a command in `python3 -c`, running a script written to disk, decoding base64 into a shell, or `find / -delete` are not covered. It also does not stop deleting your own data under `/mnt` or `/data`. Treat it as protection against accidents, not against an attacker.
4. **A stolen bot token or Telegram account is a root shell.** `/sh` runs on the server for whoever controls the owner chat. Use Telegram two-step verification and a strong password, and revoke the token in BotFather if it leaks.
5. **Data leaves the server.** Command output, logs and file contents the LLM reads go to Google (Gemini) and Groq. Do not run this where logs contain secrets, personal data or customer data.
6. **Secrets at rest.** `/opt/vps-agent/.env` holds the Telegram token and API keys in plain text (`chmod 600`, readable by root). Anyone with root already owns the server, but backups and snapshots will contain these keys.
7. **Upgrades can break things.** `apt upgrade` can change behaviour. The agent detects and repairs many failures but not all, and a failed repair at 04:00 means you find out from Telegram. `AUTO_REBOOT=true` can turn a bad kernel or a container without a restart policy into an outage.
8. **Availability depends on three outside services.** Telegram, Gemini and Groq outages mean no reports and no AI healing. Quick restarts and the watchdog still work without an LLM.
9. **Supply chain.** `install.sh` installs `openai`, `python-dotenv` and `python-telegram-bot` from PyPI without pinned versions. Review `agent.py` and `install.sh` before running them as root, and install only from this repository.

### Recommended hardening

- Keep `AUTO_REBOOT=false` and `AUTOREMOVE=false`.
- Take a provider snapshot before enabling the schedule, and keep the provider web console as a backup way in.
- Use a dedicated Telegram bot per server, with two-step verification on your account.
- Use API keys scoped to this project with a spending or quota limit, and rotate them if the server is ever exposed.
- Set `HEALTH_URLS` to real endpoints so failures are detected, not just assumed.
- Check `/history` and the `actions` table in `agent.db` regularly to see what the agent ran.
- Pin the pip versions in `install.sh` once you have a working install.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Files

| File | Purpose |
|---|---|
| `agent.py` | The agent |
| `install.sh` | Installs to `/opt/vps-agent`, creates a venv and starts the service |
| `vps-agent.service` | systemd unit (root, auto restart) |
| `.env.example` | Config template with placeholders. Copy to `.env`; never commit `.env` |
| `RUNBOOK.md` | Setup, operation and troubleshooting |

## Uninstall

```bash
systemctl disable --now vps-agent
rm /etc/systemd/system/vps-agent.service
systemctl daemon-reload
rm -rf /opt/vps-agent /var/lib/vps-agent
```

## License

[MIT](LICENSE). Provided as is, with no warranty. You are responsible for what runs on your server.
