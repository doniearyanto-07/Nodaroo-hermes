#!/usr/bin/env python3
"""Saklar induk Nodaroo: pasang + jalankan 9Router dan Hermes, lalu jaga keduanya tetap hidup.

Dijalankan oleh startup command panel (python app.py). Stdlib saja.
Kunci rahasia dibaca dari secrets.env di folder server (tidak pernah ada di repo).
"""
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request

HOME = os.environ.get("HOME_DIR", "/home/container")
PORT = os.environ.get("SERVER_PORT", "7825")
NODE_VERSION = "v24.22.3"
NINEROUTER_VERSION = "0.5.123"
HERMES_VERSION = "v0.23.3"

NODE_DIR = os.path.join(HOME, ".node")
NODE_BIN = os.path.join(NODE_DIR, "bin")
NR_PREFIX = os.path.join(HOME, ".9router-app")
NR_APP = os.path.join(NR_PREFIX, "node_modules", "9router", "app")
NR_DATA = os.path.join(HOME, "data", "9router")
HERMES_VENV = os.path.join(HOME, ".hermes-venv")
HERMES_BIN = os.path.join(HERMES_VENV, "bin", "hermes")
HERMES_HOME = os.path.join(HOME, "data", "hermes")
SECRETS = os.path.join(HOME, "secrets.env")

DEFAULTS = {
    "MODEL_BASE_URL": "https://api.tokenflow.my.id/v1",
    "MODEL_NAME": "deepseek-v4.1-flash-1017",
    "MODEL_CONTEXT": "131072",
}
NEED_9ROUTER = ["NINEROUTER_PASSWORD"]
NEED_HERMES = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS", "MODEL_API_KEY"]

stopping = threading.Event()


def log(msg):
    print(f"[saklar] {msg}", flush=True)


def load_secrets():
    vals = dict(DEFAULTS)
    if os.path.exists(SECRETS):
        with open(SECRETS, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if v:
                    vals[k.strip()] = v
    return vals


def run(cmd, **kw):
    log("jalan: " + " ".join(cmd[:3]) + " ...")
    subprocess.run(cmd, check=True, **kw)


def ensure_node():
    if os.path.exists(os.path.join(NODE_BIN, "node")):
        return
    name = f"node-{NODE_VERSION}-linux-x64.tar.xz"
    base = f"https://nodejs.org/dist/{NODE_VERSION}/"
    log(f"download Node.js {NODE_VERSION} ...")
    sums = urllib.request.urlopen(base + "SHASUMS256.txt", timeout=60).read().decode()
    want = next(l.split()[0] for l in sums.splitlines() if l.endswith(name))
    tmp = os.path.join(HOME, name)
    urllib.request.urlretrieve(base + name, tmp)
    with open(tmp, "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    if got != want:
        os.remove(tmp)
        raise RuntimeError("checksum Node.js tidak cocok, download dibatalkan")
    with tarfile.open(tmp) as t:
        t.extractall(HOME, filter="data")
    os.remove(tmp)
    shutil.rmtree(NODE_DIR, ignore_errors=True)
    os.rename(os.path.join(HOME, name[: -len(".tar.xz")]), NODE_DIR)
    log("Node.js siap")


def node_env():
    env = dict(os.environ)
    env["PATH"] = NODE_BIN + os.pathsep + env.get("PATH", "")
    env["HOME"] = HOME
    return env


def ensure_9router():
    marker = os.path.join(NR_PREFIX, ".version")
    if os.path.exists(os.path.join(NR_APP, "server.js")) and open_text(marker) == NINEROUTER_VERSION:
        return
    log(f"pasang 9Router {NINEROUTER_VERSION} ...")
    os.makedirs(NR_PREFIX, exist_ok=True)
    run([os.path.join(NODE_BIN, "npm"), "install", f"9router@{NINEROUTER_VERSION}",
         "--prefix", NR_PREFIX, "--no-audit", "--no-fund"], env=node_env())
    with open(marker, "w") as f:
        f.write(NINEROUTER_VERSION)


def ensure_hermes():
    marker = os.path.join(HERMES_VENV, ".version")
    if os.path.exists(HERMES_BIN) and open_text(marker) == HERMES_VERSION:
        return
    log(f"pasang Hermes {HERMES_VERSION} (agak lama, sekali saja) ...")
    if not os.path.exists(os.path.join(HERMES_VENV, "bin", "python")):
        run([sys.executable, "-m", "venv", HERMES_VENV])
    src = f"https://github.com/NousResearch/hermes-agent/archive/refs/tags/{HERMES_VERSION}.tar.gz"
    run([os.path.join(HERMES_VENV, "bin", "pip"), "install", "--no-cache-dir",
         f"hermes-agent[messaging] @ {src}"])
    with open(marker, "w") as f:
        f.write(HERMES_VERSION)


def open_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def write_hermes_config(s):
    os.makedirs(HERMES_HOME, exist_ok=True)
    cfg = (
        "# Ditulis ulang oleh app.py tiap start. Ubah lewat secrets.env, bukan di sini.\n"
        "model:\n"
        f"  default: \"{s['MODEL_NAME']}\"\n"
        "  provider: \"custom\"\n"
        f"  base_url: \"{s['MODEL_BASE_URL']}\"\n"
        f"  api_key: \"{s['MODEL_API_KEY']}\"\n"
        f"  context_length: {int(s['MODEL_CONTEXT'])}\n"
    )
    env = (
        f"TELEGRAM_BOT_TOKEN={s['TELEGRAM_BOT_TOKEN']}\n"
        f"TELEGRAM_ALLOWED_USERS={s['TELEGRAM_ALLOWED_USERS']}\n"
    )
    for name, body in (("config.yaml", cfg), (".env", env)):
        p = os.path.join(HERMES_HOME, name)
        with open(p, "w") as f:
            f.write(body)
        os.chmod(p, 0o600)


def pump(tag, stream):
    for line in iter(stream.readline, b""):
        sys.stdout.write(f"[{tag}] {line.decode(errors='replace')}")
        sys.stdout.flush()


class Child:
    def __init__(self, tag, cmd, cwd, env):
        self.tag, self.cmd, self.cwd, self.env = tag, cmd, cwd, env
        self.proc = None
        self.delay = 5

    def start(self):
        log(f"nyalakan {self.tag}")
        self.started = time.time()
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, env=self.env, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, start_new_session=True)
        threading.Thread(target=pump, args=(self.tag, self.proc.stdout), daemon=True).start()

    def check(self):
        code = self.proc.poll()
        if code is None or stopping.is_set():
            return
        if time.time() - self.started > 120:
            self.delay = 5
        log(f"{self.tag} mati (kode {code}), nyala lagi dalam {self.delay} detik")
        if stopping.wait(self.delay):
            return
        self.delay = min(self.delay * 2, 300)
        self.start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)

    def kill(self):
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)


def main():
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    s = load_secrets()
    miss_nr = [k for k in NEED_9ROUTER if k not in s]
    miss_h = [k for k in NEED_HERMES if k not in s]
    if miss_nr or miss_h:
        log("secrets.env belum lengkap, isi lewat menu Files lalu Restart. Yang kosong: "
            + ", ".join(miss_nr + miss_h))
    if s.get("MODEL_CONTEXT", "0").isdigit() and int(s["MODEL_CONTEXT"]) < 64000:
        log("MODEL_CONTEXT di bawah 64000, Hermes tidak mau jalan. Hermes dilewati.")
        miss_h.append("MODEL_CONTEXT>=64000")

    children = []
    if not miss_nr:
        ensure_node()
        ensure_9router()
        env = node_env()
        env.update(PORT=PORT, HOSTNAME="0.0.0.0", DATA_DIR=NR_DATA,
                   INITIAL_PASSWORD=s["NINEROUTER_PASSWORD"], NODE_ENV="production")
        os.makedirs(NR_DATA, exist_ok=True)
        children.append(Child("9router", [os.path.join(NODE_BIN, "node"), "server.js"], NR_APP, env))
    if not miss_h:
        ensure_hermes()
        write_hermes_config(s)
        env = dict(os.environ, HERMES_HOME=HERMES_HOME, HOME=HOME,
                   PATH=os.path.join(HERMES_VENV, "bin") + os.pathsep + NODE_BIN + os.pathsep + os.environ.get("PATH", ""))
        children.append(Child("hermes", [HERMES_BIN, "gateway", "run"], HOME, env))

    for c in children:
        c.start()
    if not children:
        log("tidak ada yang dinyalakan, nunggu secrets.env diisi (server tetap hidup)")
    while not stopping.wait(5):
        for c in children:
            c.check()

    log("berhenti, matikan semua ...")
    for c in children:
        c.stop()
    deadline = time.time() + 10
    while time.time() < deadline and any(c.proc.poll() is None for c in children):
        time.sleep(0.5)
    for c in children:
        c.kill()
    log("selesai")


if __name__ == "__main__":
    main()
