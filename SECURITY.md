# Security Policy

## Threat model

VPS Keeper Agent runs as root on your server and takes instructions from two places: a Telegram chat matching `OWNER_CHAT_ID`, and an LLM (Gemini or Groq) during healing. Its protections are:

- **Owner check.** Messages from any other chat are ignored.
- **Command blocklist.** Regexes in `agent.py` (`DENY`) reject destructive commands from the LLM and from `/sh`. This is a blocklist and can be bypassed. It is not a sandbox.
- **Step limit.** `MAX_HEAL_STEPS` caps LLM tool calls per heal attempt.
- **Audit log.** Every command, blocked or run, is stored in `/var/lib/vps-agent/agent.db`.

Out of scope for protection: a compromised Telegram account or bot token, a compromised server, malicious content in logs steering the LLM, and data you choose to send to LLM providers.

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Use GitHub's private reporting: **Security tab > Report a vulnerability** on this repository. Include the affected file and line, steps to reproduce, and the impact. You can expect an acknowledgement within a few days. This is a personal project, so there is no fixed fix timeline.

Examples that count as vulnerabilities: a way for someone other than the owner to trigger commands, a bypass of the `DENY` list that is easy to hit by accident, secrets written to logs or Telegram messages, and an unsafe default in `install.sh` or `vps-agent.service`.

Examples that do not count: "the blocklist can be bypassed by a determined root user" (documented), and "the agent runs as root" (documented).

## If you leaked a secret

1. Revoke the Telegram token in @BotFather (`/revoke`) and create a new one.
2. Delete and recreate the Gemini and Groq keys.
3. Update `/opt/vps-agent/.env` and run `systemctl restart vps-agent`.
4. Review `/history` and the `actions` table in `agent.db` for commands you did not expect.
5. If the repository history contains the secret, rotate it anyway; deleting a commit does not remove copies.

## Supported versions

Only the latest commit on `main`.
