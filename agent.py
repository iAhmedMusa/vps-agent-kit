"""VPS Keeper: AI agent that keeps an Ubuntu server updated and healthy.

- Scheduled apt update + upgrade (keeps your existing config files)
- Snapshot of known-good state before upgrade: running services, docker
  containers, listening ports, health URLs
- After upgrade: diff vs snapshot, quick fixes, then LLM self-heal loop
  (Groq brain, Gemini fallback) with a guarded root shell
- Watchdog between upgrades: drift from baseline gets healed + reported
- Reports and commands over Telegram (owner only)

Runs as root (needs apt + systemctl). See install.sh.
"""
import os, re, json, time, shlex, shutil, socket, sqlite3, asyncio, threading, subprocess
import urllib.request, urllib.error
import datetime as dt
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import OpenAI
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()


def env(k, d=""):
    return os.getenv(k, d).strip()


# ---------------- CONFIG ----------------
TZ = ZoneInfo(env("TZ_NAME", "Asia/Dhaka"))
OWNER = int(env("OWNER_CHAT_ID", "0") or 0)
HOST = env("SERVER_NAME") or socket.gethostname()
UPGRADE_TIME = env("UPGRADE_TIME", "04:00")
UPGRADE_DAYS = tuple(int(d) for d in env("UPGRADE_DAYS", "0,1,2,3,4,5,6").split(",") if d.strip())  # 0=Sun
UPGRADE_MODE = env("UPGRADE_MODE", "upgrade")  # upgrade | full-upgrade
AUTO_REBOOT = env("AUTO_REBOOT", "false").lower() == "true"
AUTOREMOVE = env("AUTOREMOVE", "false").lower() == "true"
HEALTH_URLS = [u.strip() for u in env("HEALTH_URLS").split(",") if u.strip()]
HEALTH_INTERVAL_MIN = int(env("HEALTH_INTERVAL_MIN", "15"))
MAX_HEAL_STEPS = int(env("MAX_HEAL_STEPS", "15"))
MIN_FREE_GB = float(env("MIN_FREE_GB", "2"))
DISK_ALERT_PCT = int(env("DISK_ALERT_PCT", "90"))
IGNORE_PORTS = {p.strip() for p in env("IGNORE_PORTS").split(",") if p.strip()}
IGNORE_RE = re.compile(env(
    "IGNORE_SERVICES_REGEX",
    r"^(apt-daily|unattended-upgrades|packagekit|fwupd|man-db|motd-news|update-notifier|logrotate|"
    r"e2scrub|fstrim|sysstat|systemd-tmpfiles|user@|user-runtime-dir@|getty@|serial-getty@|certbot|"
    r"snapd\.seeded|vps-agent)"))
DB_PATH = env("DB_PATH", "/var/lib/vps-agent/agent.db")
APT = ("apt-get -y -o DPkg::Lock::Timeout=900 "
       "-o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold")

# ---------------- BRAIN ----------------
# Both providers speak the OpenAI API format, one SDK covers both.
_AVAILABLE = {}
if env("GEMINI_API_KEY"):
    _AVAILABLE["gemini"] = ("gemini", OpenAI(api_key=env("GEMINI_API_KEY"),
                                             base_url="https://generativelanguage.googleapis.com/v1beta/openai/"),
                            env("GEMINI_MODEL", "gemini-2.5-flash"))
if env("GROQ_API_KEY"):
    _AVAILABLE["groq"] = ("groq", OpenAI(api_key=env("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1"),
                          env("GROQ_MODEL", "openai/gpt-oss-120b"))
# BRAIN_ORDER: first = primary, rest = fallbacks in order
_ORDER = [p.strip().lower() for p in env("BRAIN_ORDER", "gemini,groq").split(",") if p.strip()]
PROVIDERS = [_AVAILABLE[p] for p in _ORDER if p in _AVAILABLE] + \
            [v for k, v in _AVAILABLE.items() if k not in _ORDER]


def chat(messages, tools=None):
    last = None
    for name, client, model in PROVIDERS:
        try:
            kw = dict(model=model, messages=messages, temperature=0.2, timeout=120)
            if tools:
                kw["tools"] = tools
            return client.chat.completions.create(**kw).choices[0].message
        except Exception as e:  # rate limit, outage, bad model name: try next
            last = e
            print(f"[llm] {name} failed: {e}")
    raise RuntimeError(f"all LLM providers failed: {last}")


# ---------------- STORAGE ----------------
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
DB_LOCK = threading.Lock()
db.executescript("""
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, kind TEXT, started TEXT, finished TEXT,
                                 status TEXT, report TEXT);
CREATE TABLE IF NOT EXISTS actions (id INTEGER PRIMARY KEY, run_id INTEGER, ts TEXT, cmd TEXT,
                                    rc INTEGER, output TEXT);
""")


def now():
    return dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def q(sql, args=()):
    with DB_LOCK:
        cur = db.execute(sql, args)
        db.commit()
        return cur


def kv_get(k, default=None):
    row = q("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return json.loads(row[0]) if row else default


def kv_set(k, v):
    q("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))


def kv_del(k):
    q("DELETE FROM kv WHERE k=?", (k,))


def run_start(kind):
    return q("INSERT INTO runs(kind,started,status) VALUES(?,?,?)", (kind, now(), "running")).lastrowid


def run_end(rid, status, report):
    q("UPDATE runs SET finished=?, status=?, report=? WHERE id=?", (now(), status, report, rid))


# ---------------- SHELL ----------------
SH_ENV = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "a", "LC_ALL": "C.UTF-8"}


def sh(cmd, timeout=300, run_id=None):
    try:
        p = subprocess.run(cmd, shell=True, executable="/bin/bash", capture_output=True, text=True,
                           timeout=timeout, env=SH_ENV, stdin=subprocess.DEVNULL)
        rc, out = p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        rc, out = 124, f"timed out after {timeout}s"
    if run_id:
        q("INSERT INTO actions(run_id,ts,cmd,rc,output) VALUES(?,?,?,?,?)", (run_id, now(), cmd, rc, out[-4000:]))
    return rc, out


def lines(cmd):
    return [l.strip() for l in sh(cmd, 90)[1].splitlines() if l.strip()]


# Hard safety rails for anything the LLM (or /sh) runs. The agent's own
# upgrade/reboot code calls sh() directly and is not affected.
DENY = [re.compile(p, re.I) for p in [
    r"\brm\s+(-\S+\s+)*/+(\*|\s|$)",
    r"\brm\s+(-\S+\s+)*/+(etc|usr|bin|sbin|lib|lib64|boot|var|home|root|opt|srv)/*(\s|$)",
    r"\bmkfs", r"\bdd\b.*\bof=/dev/", r">\s*/dev/(sd|vd|nvme|xvd)", r"\bwipefs\b", r"\b(fdisk|parted)\b",
    r":\(\)\s*\{",
    r"(^|[;&|]\s*|sudo\s+)(shutdown|poweroff|halt|reboot)\b(?!-)", r"\binit\s+[06]\b",
    r"systemctl\s+(reboot|poweroff|halt|kexec)",
    r"systemctl\s+(stop|disable|mask)\s+(ssh|sshd|vps-agent)",
    r"\b(userdel|deluser|passwd|chpasswd|usermod)\b",
    r"(>|\btee\b|sed\s+-i).*(sshd_config|authorized_keys|/etc/sudoers|/etc/shadow|/etc/passwd)",
    r"\bufw\s+(disable|reset|deny)", r"\biptables\s+(-F|--flush|-P\s+\S+\s+DROP)", r"\bnft\s+flush",
    r"\b(apt|apt-get)\b[^|;&]*\b(remove|purge|autoremove)\b", r"\bdpkg\s+(-r|-P|--remove|--purge)\b",
    r"\b(curl|wget)\b.*\|\s*(sudo\s+)?(ba|z)?sh\b",
    r"\bchmod\s+(-R\s+)?\S+\s+/+(\s|$)", r"\bchown\s+-R\s+\S+\s+/+(\s|$)",
    r"docker\s+(system|volume|image|container)\s+prune", r"docker\s+volume\s+rm", r"docker\s+rm\s+-f",
    r"docker[- ]compose.*\bdown\b.*(-v\b|--volumes)", r"\bcrontab\s+-r\b",
]]


def guarded(cmd, timeout=300, run_id=None):
    if not cmd.strip():
        return "empty command"
    for p in DENY:
        if p.search(cmd):
            if run_id:
                q("INSERT INTO actions(run_id,ts,cmd,rc,output) VALUES(?,?,?,?,?)",
                  (run_id, now(), cmd, -1, "BLOCKED"))
            return f"BLOCKED by safety rule ({p.pattern}). Find a safer way or tell the owner what is needed."
    rc, out = sh(cmd, timeout, run_id)
    return f"exit={rc}\n{out[-3500:] or '(no output)'}"


# ---------------- HEALTH ----------------
def url_ok(u):
    try:
        req = urllib.request.Request(u, headers={"User-Agent": "vps-agent"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500  # 401/403/404 still means the app answers
    except Exception:
        return False


def disk_pct():
    u = shutil.disk_usage("/")
    return round(u.used / u.total * 100)


def free_gb():
    return shutil.disk_usage("/").free / 1e9


def snapshot():
    s = {
        "services": sorted(l.split()[0] for l in
                           lines("systemctl list-units --type=service --state=running --no-legend --plain")),
        "failed": sorted(l.split()[0] for l in lines("systemctl list-units --state=failed --no-legend --plain")),
        "ports": sorted(set(lines("ss -H -ltn | awk '{print $4}' | awk -F: '{print $NF}'"))),
        "containers": [],
        "urls": {u: url_ok(u) for u in HEALTH_URLS},
    }
    if shutil.which("docker"):
        rc, out = sh("docker ps --format '{{.Names}}'", 60)
        if rc == 0:
            s["containers"] = sorted(l for l in out.splitlines() if l.strip())
    return s


def diff(base, cur):
    probs = []
    for x in sorted(set(base.get("services", [])) - set(cur["services"])):
        if not IGNORE_RE.search(x):
            probs.append(["service", x])
    for x in sorted(set(base.get("containers", [])) - set(cur["containers"])):
        probs.append(["container", x])
    for x in sorted(set(base.get("ports", [])) - set(cur["ports"])):
        if x not in IGNORE_PORTS:
            probs.append(["port", x])
    for x in sorted(set(cur["failed"]) - set(base.get("failed", []))):
        if not IGNORE_RE.search(x):
            probs.append(["failed", x])
    for u, ok in cur["urls"].items():
        if not ok and base.get("urls", {}).get(u, True):
            probs.append(["url", u])
    if disk_pct() >= DISK_ALERT_PCT:
        probs.append(["disk", f"/ at {disk_pct()}%"])
    return probs


def problems_now():
    return diff(kv_get("baseline", {}), snapshot())


def fmt(probs):
    return "\n".join(f"  • {k}: {n}" for k, n in probs) or "  none"


def apt_healthy():
    return sh("dpkg --audit", 120)[1].strip() == ""


def upgradable():
    return [l for l in lines("apt list --upgradable 2>/dev/null | tail -n +2") if "/" in l]


def quick_heal(probs, run_id):
    """Cheap deterministic fixes before spending LLM calls."""
    did = []
    for kind, name in probs:
        if kind in ("service", "failed") and name.endswith((".service", ".socket", ".timer")):
            if kind == "failed":
                sh(f"systemctl reset-failed {shlex.quote(name)}", 60, run_id)
            rc, _ = sh(f"systemctl restart {shlex.quote(name)}", 120, run_id)
            did.append(f"restart {name}: {'ok' if rc == 0 else 'failed'}")
        elif kind == "container":
            rc, _ = sh(f"docker start {shlex.quote(name)}", 120, run_id)
            did.append(f"docker start {name}: {'ok' if rc == 0 else 'failed'}")
        elif kind == "disk":
            sh("apt-get clean; journalctl --vacuum-size=300M", 300, run_id)
            did.append("cleaned apt cache + journal")
    if did:
        time.sleep(15)
    return did


# ---------------- AGENT LOOP ----------------
TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a non-interactive bash command as root on the server. Returns exit code and output tail.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"},
            "timeout": {"type": "integer", "description": "seconds, default 300, max 900"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "check_health",
        "description": "Compare the server with its known-good baseline (services, containers, ports, URLs, disk). "
                       "Returns remaining problems, or 'healthy'.",
        "parameters": {"type": "object", "properties": {
            "note": {"type": "string", "description": "optional, ignored"}}}}},
]

HEAL_SYSTEM = f"""You are the maintenance agent for Ubuntu server '{HOST}'. You act as root through tools.
Goal: bring the server back to its known-good baseline: same running services, docker containers,
listening ports and health URLs as before, and a clean package state (dpkg --audit empty).

How to work:
- Diagnose first: systemctl status X --no-pager, journalctl -u X -n 80 --no-pager, docker logs --tail 80 X,
  config tests (nginx -t, apache2ctl configtest, php-fpmX.Y -t, sshd -t), dpkg --audit.
- Smallest fix wins: restart, fix a config syntax error, dpkg --configure -a, apt-get -f install,
  reinstall a broken package (apt-get install --reinstall), compare with *.dpkg-dist / *.dpkg-old / *.ucf-dist,
  docker compose up -d in the project dir (find it via docker inspect labels).
- Before editing any file: cp FILE FILE.bak.$(date +%s)
- Never remove packages, users, data, volumes, firewall rules or SSH access. Never reboot (owner decides).
- Only non-interactive commands. Pass -y, --no-pager, timeouts.
- After each fix, call check_health. When healthy or truly stuck, stop calling tools and reply with a short
  plain-text report: root cause, what you changed, final state, anything the owner must do by hand."""

CHAT_SYSTEM = f"""You are the ops assistant for Ubuntu server '{HOST}', talking to its owner on Telegram.
You can inspect and act as root via run_command and check_health.
- Questions: investigate with read-only commands, then answer.
- Risky changes (editing configs, restarting ssh, docker daemon or databases, installing things): explain
  the plan and ask the owner first, unless the owner already told you to do it.
- Same hard rules as maintenance: back up before editing, never remove data/packages/users/SSH, never reboot.
Reply short, plain text, no markdown tables."""


def run_agent(messages, run_id=None, max_steps=MAX_HEAL_STEPS):
    for _ in range(max_steps):
        msg = chat(messages, TOOLS)
        calls = msg.tool_calls or []
        if not calls:
            return (msg.content or "(no answer)").strip()
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": [c.model_dump() for c in calls]})
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if c.function.name == "run_command":
                t = min(int(args.get("timeout") or 300), 900)
                result = guarded(str(args.get("command", "")), t, run_id)
            elif c.function.name == "check_health":
                p = problems_now()
                result = ("problems:\n" + fmt(p)) if p else "healthy: matches baseline"
                result += "" if apt_healthy() else "\ndpkg --audit reports broken packages"
            else:
                result = "unknown tool"
            messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
    return f"Stopped after {max_steps} steps without finishing. Check /history and fix by hand."


def llm_heal(context, run_id):
    if not PROVIDERS:
        return "No LLM key set, AI healing skipped."
    msgs = [{"role": "system", "content": HEAL_SYSTEM}, {"role": "user", "content": context}]
    try:
        return run_agent(msgs, run_id)
    except Exception as e:
        return f"AI healing failed: {e}"


# ---------------- JOBS (sync, run in threads) ----------------
def header(icon, title):
    return f"{icon} {HOST} | {title}\n🕒 {now()}"


def upgrade_cycle(kind="scheduled"):
    """Returns (report, reboot_now)."""
    t0, rid = time.time(), run_start(kind)

    base = snapshot()  # "as it is working now" = the target after upgrade
    kv_set("baseline", base)
    kv_set("baseline_at", now())

    if free_gb() < MIN_FREE_GB:
        sh("apt-get clean; journalctl --vacuum-size=300M", 300, rid)
        if free_gb() < MIN_FREE_GB:
            rep = header("❌", "Upgrade skipped") + f"\nOnly {free_gb():.1f} GB free on /, need {MIN_FREE_GB} GB."
            run_end(rid, "skipped", rep)
            return rep, False

    sh("dpkg --configure -a", 900, rid)
    rc, out = sh(f"{APT} update", 900, rid)
    apt_err = f"apt-get update failed:\n{out[-2000:]}\n" if rc else ""
    pending = upgradable()
    pkgs = [l.split("/")[0] for l in pending]
    security = sum("-security" in l for l in pending)
    held = lines("apt-mark showhold")

    if not pkgs and not apt_err:
        rep = header("✅", "No updates") + f"\nDisk {disk_pct()}% used" + (f"\nHeld: {', '.join(held)}" if held else "")
        run_end(rid, "noop", rep)
        return rep, False

    kept = []
    if pkgs:
        rc, out = sh(f"{APT} {UPGRADE_MODE}", 3600, rid)
        if rc:  # one automatic repair + retry before asking the brain
            sh("dpkg --configure -a", 900, rid)
            sh(f"{APT} -f install", 1800, rid)
            rc, out = sh(f"{APT} {UPGRADE_MODE}", 3600, rid)
        if rc:
            apt_err += f"apt-get {UPGRADE_MODE} failed:\n{out[-2500:]}\n"
        m = re.search(r"kept back:\n((?:\s+.+\n)+)", out + "\n")
        kept = m.group(1).split() if m else []
        if AUTOREMOVE and rc == 0:
            sh(f"{APT} autoremove", 900, rid)

    time.sleep(20)  # let restarted services settle
    probs = diff(base, snapshot())
    fixes = quick_heal(probs, rid) if probs else []
    if fixes:
        probs = diff(base, snapshot())

    ai = ""
    if probs or apt_err or not apt_healthy():
        ai = llm_heal(
            f"A package upgrade just ran on {HOST}.\nUpgraded: {', '.join(pkgs[:80]) or 'none'}\n"
            f"{apt_err}Problems vs baseline:\n{fmt(probs)}\n"
            f"Quick fixes already tried: {fixes or 'none'}\n"
            f"dpkg --audit clean: {apt_healthy()}\nFix it and verify with check_health.", rid)
        probs = diff(base, snapshot())

    apt_ok = apt_healthy()
    reboot = os.path.exists("/var/run/reboot-required")
    rb_pkgs = []
    if os.path.exists("/var/run/reboot-required.pkgs"):
        rb_pkgs = sorted(set(open("/var/run/reboot-required.pkgs").read().split()))

    if probs or not apt_ok:
        icon, title, status = "❌", "Upgrade done, NEEDS ATTENTION", "failed"
    elif fixes or ai:
        icon, title, status = "🛠", "Upgraded, issues auto-fixed", "healed"
    else:
        icon, title, status = "✅", "Upgraded, all healthy", "ok"

    rep = [header(icon, title),
           f"📦 {len(pkgs)} packages ({security} security): " + ", ".join(pkgs[:40]) + (" ..." if len(pkgs) > 40 else "")]
    if kept:
        rep.append(f"⏸ Kept back: {', '.join(kept)}")
    if held:
        rep.append(f"📌 Held: {', '.join(held)}")
    if fixes:
        rep.append("🔧 Quick fixes:\n" + "\n".join(f"  • {f}" for f in fixes))
    if ai:
        rep.append(f"🧠 AI fix report:\n{ai}")
    rep.append(f"🩺 Final: {'healthy' if not probs else 'problems left'}\n{fmt(probs)}" if probs else "🩺 Final: healthy, matches baseline")
    if not apt_ok:
        rep.append("⚠️ dpkg --audit still reports broken packages")

    reboot_now = False
    if reboot:
        if AUTO_REBOOT and status != "failed":
            reboot_now = True
            rep.append(f"♻️ Reboot required ({', '.join(rb_pkgs) or 'kernel/libs'}). Rebooting in 2 min, will verify after boot.")
        else:
            rep.append(f"♻️ Reboot required ({', '.join(rb_pkgs) or 'kernel/libs'}). Send /reboot yes when ready.")
    rep.append(f"💾 Disk {disk_pct()}% | ⏱ {int(time.time() - t0)}s | run #{rid}")

    report = "\n".join(rep)
    run_end(rid, status, report)
    return report, reboot_now


def heal_pass(kind, force=False):
    """Compare with baseline, fix drift. Returns message or None (nothing worth sending)."""
    base = kv_get("baseline")
    if not base:
        return "No baseline yet. Send /baseline when the server is in a good state."
    probs = diff(base, snapshot())
    last = kv_get("watch_last", [])
    if not probs:
        kv_set("watch_last", [])
        if force:
            return header("✅", "Healthy, matches baseline")
        return header("✅", "Recovered, back to baseline") if last else None
    if probs == last and not force:
        return None  # same unresolved issue, already reported; /heal to retry

    rid = run_start(kind)
    fixes = quick_heal(probs, rid)
    left = diff(base, snapshot())
    ai = ""
    if left:
        ai = llm_heal(f"The server drifted from its baseline (no upgrade involved).\nProblems:\n{fmt(left)}\n"
                      f"Quick fixes already tried: {fixes or 'none'}\nFix it and verify with check_health.", rid)
        left = diff(base, snapshot())
    kv_set("watch_last", left)

    rep = [header("🛠" if not left else "❌", "Self-healed" if not left else "Problem NOT fixed"),
           "Found:\n" + fmt(probs)]
    if fixes:
        rep.append("🔧 Quick fixes:\n" + "\n".join(f"  • {f}" for f in fixes))
    if ai:
        rep.append(f"🧠 AI report:\n{ai}")
    rep.append("🩺 Final: healthy" if not left else "🩺 Still broken:\n" + fmt(left))
    report = "\n".join(rep)
    run_end(rid, "healed" if not left else "failed", report)
    return report


def status_text():
    load = " ".join(open("/proc/loadavg").read().split()[:3])
    mem = sh("free -m | awk '/Mem:/{printf \"%d/%d MB\", $3, $2}'")[1]
    base = kv_get("baseline")
    probs = diff(base, snapshot()) if base else []
    pend = upgradable()
    out = [header("🖥", "Status"),
           f"⏫ {sh('uptime -p')[1]} | load {load}",
           f"🧮 RAM {mem} | 💾 disk {disk_pct()}% ({free_gb():.1f} GB free)",
           f"📦 {len(pend)} pending updates ({sum('-security' in l for l in pend)} security), from last apt update",
           f"♻️ Reboot required: {'yes' if os.path.exists('/var/run/reboot-required') else 'no'}",
           (f"📸 Baseline {kv_get('baseline_at', '?')}: {len(base['services'])} services, "
            f"{len(base['containers'])} containers, {len(base['ports'])} ports")
           if base else "📸 Baseline: none, send /baseline",
           "🩺 Drift:\n" + fmt(probs)]
    return "\n".join(out)


# ---------------- TELEGRAM ----------------
BUSY = asyncio.Lock()
CHAT_HISTORY = []  # short in-RAM conversation, /new clears


async def send(bot, text):
    text = text or "(empty)"
    for i in range(0, len(text), 3900):
        try:
            await bot.send_message(OWNER, text[i:i + 3900])
        except Exception as e:  # never let a Telegram error kill an upgrade/heal job
            print(f"[telegram] send failed: {e}\n{text[i:i + 3900]}")


def owner_only(fn):
    async def wrap(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or update.effective_chat.id != OWNER:
            return
        return await fn(update, ctx)
    return wrap


async def exclusive(bot, label, func, *args):
    if BUSY.locked():
        await send(bot, f"⏳ Busy with another job, {label} skipped. Try again in a bit.")
        return None
    async with BUSY:
        return await asyncio.to_thread(func, *args)


async def do_upgrade(bot, kind):
    await send(bot, f"🔄 {HOST}: {kind} update/upgrade started")
    try:
        res = await exclusive(bot, "upgrade", upgrade_cycle, kind)
    except Exception as e:
        res = (header("❌", f"Upgrade crashed: {e}"), False)
    if not res:
        return
    report, reboot_now = res
    await send(bot, report)
    if reboot_now:
        kv_set("post_reboot", True)
        sh("shutdown -r +2 'vps-agent: post-upgrade reboot'")


async def job_upgrade(ctx: ContextTypes.DEFAULT_TYPE):
    await do_upgrade(ctx.bot, "scheduled")


async def job_watchdog(ctx: ContextTypes.DEFAULT_TYPE):
    if BUSY.locked():
        return
    async with BUSY:
        try:
            msg = await asyncio.to_thread(heal_pass, "watchdog")
        except Exception as e:
            msg = header("❌", f"Watchdog error: {e}")
    if msg:
        await send(ctx.bot, msg)


async def job_post_reboot(ctx: ContextTypes.DEFAULT_TYPE):
    kv_del("post_reboot")
    kernel = sh("uname -r")[1]
    async with BUSY:
        msg = await asyncio.to_thread(heal_pass, "post-reboot", True)
    await send(ctx.bot, f"♻️ {HOST} back online after reboot. Kernel {kernel}\n{msg}")


MENU = [
    ("status", "Health, load, disk, pending updates, drift"),
    ("check", "apt update + list available updates (no install)"),
    ("upgrade", "Run update/upgrade + self-heal + report now"),
    ("heal", "Compare with baseline and fix drift now"),
    ("baseline", "Save current state as known-good"),
    ("logs", "Journal of a service: /logs nginx 80"),
    ("sh", "Run shell command: /sh df -h (safety rules apply)"),
    ("reboot", "Reboot server: /reboot yes, verify after boot"),
    ("history", "Last maintenance runs"),
    ("new", "Clear chat memory"),
    ("help", "Show all commands"),
]


async def on_startup(app: Application):
    try:  # Telegram "Menu" button list, same as BotFather /setcommands
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in MENU])
    except Exception as e:
        print(f"[telegram] set_my_commands failed: {e}")
    if not OWNER:
        print("OWNER_CHAT_ID not set: send /start to the bot, put the id in .env, restart.")
        return
    if not apt_healthy():
        await asyncio.to_thread(sh, "dpkg --configure -a", 900)
    if not kv_get("baseline"):
        kv_set("baseline", await asyncio.to_thread(snapshot))
        kv_set("baseline_at", now())
    if kv_get("post_reboot"):
        app.job_queue.run_once(job_post_reboot, 90)
    brains = " → ".join(f"{n}:{m}" for n, _, m in PROVIDERS) or "NONE (set GROQ_API_KEY / GEMINI_API_KEY)"
    try:
        await app.bot.send_message(OWNER, f"🟢 {HOST} agent online\n🧠 {brains}\n🗓 upgrade {UPGRADE_TIME} {TZ.key}, "
                                          f"watchdog every {HEALTH_INTERVAL_MIN} min. /help")
    except Exception as e:  # wrong OWNER_CHAT_ID or bot never started: keep running so /start can show the id
        print(f"[telegram] cannot message OWNER_CHAT_ID={OWNER}: {e}. "
              f"Open the bot in Telegram, press Start, send /start, put that id in .env, restart.")


HELP = """Commands:
/status  health, load, disk, pending updates, drift
/check   apt update + list available updates (no install)
/upgrade run full update/upgrade + heal + report now
/heal    compare with baseline and fix drift now
/baseline save current state as known-good
/logs <unit> [lines]  journal of a service
/sh <command>  run a shell command (safety rules apply)
/reboot yes  reboot now, verify after boot
/history last runs
/new     clear chat memory
Anything else: talk to the agent ("why is nginx slow?", "free some disk")."""


@owner_only
async def cmd_help(update, ctx):
    await update.message.reply_text(HELP)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Your chat id: {update.effective_chat.id}")


@owner_only
async def cmd_status(update, ctx):
    await send(ctx.bot, await asyncio.to_thread(status_text))


@owner_only
async def cmd_check(update, ctx):
    await update.message.reply_text("🔎 running apt update ...")

    def work():
        rc, out = sh(f"{APT} update", 900)
        if rc:
            return f"❌ apt update failed:\n{out[-2000:]}"
        p = upgradable()
        if not p:
            return f"✅ {HOST}: up to date"
        sec = [l.split("/")[0] for l in p if "-security" in l]
        return (f"📦 {HOST}: {len(p)} updates, {len(sec)} security\n" +
                "\n".join(l.split(" [")[0] for l in p[:50]) + ("\n..." if len(p) > 50 else "") +
                "\n\n/upgrade to install")
    await send(ctx.bot, await asyncio.to_thread(work))


@owner_only
async def cmd_upgrade(update, ctx):
    await do_upgrade(ctx.bot, "manual")


@owner_only
async def cmd_heal(update, ctx):
    await update.message.reply_text("🩺 checking against baseline ...")
    msg = await exclusive(ctx.bot, "heal", heal_pass, "manual-heal", True)
    if msg:
        await send(ctx.bot, msg)


@owner_only
async def cmd_baseline(update, ctx):
    s = await asyncio.to_thread(snapshot)
    kv_set("baseline", s)
    kv_set("baseline_at", now())
    kv_set("watch_last", [])
    await update.message.reply_text(
        f"📸 Baseline saved: {len(s['services'])} services, {len(s['containers'])} containers, "
        f"ports {', '.join(s['ports'])}" + (f", {len(s['urls'])} URLs" if s['urls'] else ""))


@owner_only
async def cmd_logs(update, ctx):
    if not ctx.args:
        return await update.message.reply_text("Usage: /logs nginx [80]")
    unit = ctx.args[0]
    n = int(ctx.args[1]) if len(ctx.args) > 1 and ctx.args[1].isdigit() else 50
    _, out = await asyncio.to_thread(sh, f"journalctl -u {shlex.quote(unit)} -n {min(n, 300)} --no-pager", 60)
    await send(ctx.bot, out[-3900:] or "(no logs)")


@owner_only
async def cmd_sh(update, ctx):
    cmd = update.message.text.partition(" ")[2].strip()
    if not cmd:
        return await update.message.reply_text("Usage: /sh df -h")
    rid = run_start("sh")
    out = await asyncio.to_thread(guarded, cmd, 300, rid)
    run_end(rid, "done", cmd)
    await send(ctx.bot, out)


@owner_only
async def cmd_reboot(update, ctx):
    if ctx.args[:1] != ["yes"]:
        return await update.message.reply_text("Send /reboot yes to confirm.")
    kv_set("post_reboot", True)
    await update.message.reply_text("♻️ Rebooting in 1 min. I'll report when back.")
    sh("shutdown -r +1 'vps-agent: owner reboot'")


@owner_only
async def cmd_history(update, ctx):
    rows = q("SELECT id, kind, started, status FROM runs ORDER BY id DESC LIMIT 10").fetchall()
    await update.message.reply_text("\n".join(f"#{i} {s} | {k} | {st}" for i, k, s, st in rows) or "no runs yet")


@owner_only
async def cmd_new(update, ctx):
    CHAT_HISTORY.clear()
    await update.message.reply_text("Chat memory cleared.")


@owner_only
async def on_text(update, ctx):
    text = update.message.text
    await ctx.bot.send_chat_action(OWNER, "typing")
    CHAT_HISTORY.append({"role": "user", "content": text})
    del CHAT_HISTORY[:-12]
    msgs = [{"role": "system", "content": CHAT_SYSTEM}] + list(CHAT_HISTORY)
    rid = run_start("chat")
    try:
        reply = await asyncio.to_thread(run_agent, msgs, rid, 10)
    except Exception as e:
        reply = f"Brain error: {e}"
    run_end(rid, "done", reply[:2000])
    CHAT_HISTORY.append({"role": "assistant", "content": reply})
    await send(ctx.bot, reply)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run as root (needs apt and systemctl).")
    app = Application.builder().token(env("TELEGRAM_TOKEN")).post_init(on_startup).build()
    app.add_handler(CommandHandler("start", cmd_start))
    for name, fn in [("help", cmd_help), ("status", cmd_status), ("check", cmd_check), ("upgrade", cmd_upgrade),
                     ("heal", cmd_heal), ("baseline", cmd_baseline), ("logs", cmd_logs), ("sh", cmd_sh),
                     ("reboot", cmd_reboot), ("history", cmd_history), ("new", cmd_new)]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    if OWNER:
        h, m = map(int, UPGRADE_TIME.split(":"))
        app.job_queue.run_daily(job_upgrade, time=dt.time(h, m, tzinfo=TZ), days=UPGRADE_DAYS)
        if HEALTH_INTERVAL_MIN > 0:
            app.job_queue.run_repeating(job_watchdog, interval=HEALTH_INTERVAL_MIN * 60, first=300)
    print(f"vps-agent running on {HOST}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
