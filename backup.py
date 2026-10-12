"""Backups of RackView's screens and site settings.

Three layers, all without secrets (passwords and tokens never leave the Pi):
  * version history: every save keeps a copy of screens + settings on the Pi (last KEEP), restorable from the builder;
  * download / upload: a .tar.gz rebuild kit (or just the screens + settings .json);
  * git: after every save the Pi commits the rebuild kit to a PRIVATE repo and pushes it with its own deploy key
    (made on first use; paste the public half into GitHub once).

Code and configuration are kept apart: the app code lives in its own git repository (RackViewer on GitHub); the
backup holds only this display's data and a pointer to the exact code version:
  screens.json   every screen as layout data: placement, text, values and colours (the factory pages too)
  config.json    site settings (no secrets)
  system/        Pi setup: desktop autostart, chrony, timezone, apt/pip package lists, boot config (reference)
  code.json      code repository + commit to rebuild with; code.patch only if the Pi runs uncommitted changes
  restore.sh     fetches that code version and runs its rebuild.sh with this backup
"""
import glob
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import time

import requests

KEEP = 20
SECRET_RE = re.compile(r"pass|token|secret", re.I)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DESKTOP_FILES = ("wayfire.ini", "kanshi/config")
PACKAGES = ("python3-pygame", "python3-flask", "python3-requests", "python3-paho-mqtt", "python3-pip", "python3-smbus",
            "i2c-tools", "chrony", "wlr-randr", "git")
README = """# RackView backup

Written automatically by the RackView display on every save. It holds this display's **configuration only**;
the app code comes from its own repository at the version recorded in `code.json`. Passwords and tokens are
never included.

| Path | What |
|---|---|
| `screens.json` | Every screen as layout data: positions, text, values, colours, detail screens, alert rules |
| `config.json` | Site settings (IPs, presets, ping hosts…) |
| `system/` | Pi setup: desktop autostart, chrony (GPS time), timezone, apt/pip package lists, boot config (reference) |
| `code.json` | Code repository and the exact commit this display ran (`code.patch` only if it had local changes) |
| `restore.sh` | Fetches that code version and rebuilds a fresh Pi with this configuration |

## Rebuild on a new Pi

1. Flash **Raspberry Pi OS 64-bit (Bookworm) with desktop** using Raspberry Pi Imager; set the user and network there.
2. On the Pi: `git clone <this repo> rackview-backup` (or unpack a downloaded backup `.tar.gz`).
3. `bash rackview-backup/restore.sh` (asks for a Home Assistant long-lived token), then `sudo reboot`.
4. Open `http://<pi>:8088`, choose the builder password; re-enter the MQTT password in Settings; in Settings →
   Backup add the Pi's new deploy key to GitHub.

Only the screens and settings changed? Restore them from the builder instead: Settings → Backup.
"""
RESTORE_SH = """#!/bin/bash
# Rebuild a RackView display from this backup: fetch the recorded code version, then run its rebuild.sh.
set -e
B=$(cd "$(dirname "$0")" && pwd)
read -r REPO COMMIT < <(python3 -c "import json;c=json.load(open('$B/code.json'));print(c['repo'],c['commit'])")
APP="$HOME/rackview"
if [ -d "$APP/.git" ]; then
    git -C "$APP" fetch -q origin
else
    [ -e "$APP" ] && mv "$APP" "$APP.before-restore-$(date +%Y%m%d-%H%M%S)"
    git clone -q "$REPO" "$APP"
fi
git -C "$APP" checkout -q "$COMMIT" 2>/dev/null || { echo "Commit $COMMIT not found; using the newest code."; git -C "$APP" checkout -q origin/HEAD; }
[ -s "$B/code.patch" ] && git -C "$APP" apply "$B/code.patch" && echo "Applied the display's local code changes."
exec bash "$APP/rebuild.sh" "$B"
"""


def sanitize(cfg):
    return {k: v for k, v in (cfg or {}).items() if not SECRET_RE.search(k)}


def _read(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _dump(data):
    return (json.dumps(data, indent=1) + "\n").encode()


def _run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _write(path, data, mode=None):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    if mode:
        os.chmod(tmp, mode)
    os.replace(tmp, path)


class Backup:
    def __init__(self, config_file, screens_file, validate):
        self.config_file, self.screens_file, self.validate = config_file, screens_file, validate
        self.dir = os.path.dirname(os.path.abspath(config_file))
        self.hist_dir = os.path.join(self.dir, "history")
        self.repo = os.path.join(self.dir, "backup-git")
        self.key = os.path.join(self.dir, "backup_key")
        self.status_file = os.path.join(self.dir, "backup_status.json")
        self.status = _read(self.status_file, {}) or {}
        self.status["busy"] = False
        self.lock = threading.Lock()
        self.wake = threading.Event()
        os.makedirs(self.hist_dir, exist_ok=True)
        self.message = None
        changed = self.snapshot("First backup" if not self.history() else "Changed while RackView was off")
        threading.Thread(target=self._worker, daemon=True).start()
        if self.git_url():                 # catches edits made while RackView was off and code updates
            self.request_push(None if changed else f"Started with code {self.code_info().get('short') or '?'}")

    # ------------------------------------------------------------ bundle
    def bundle(self):
        return {"format": "rackview-backup", "version": 1, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "host": socket.gethostname(), "screens": _read(self.screens_file),
                "config": sanitize(_read(self.config_file, {}))}

    def kit(self):
        """{path: bytes} of the rebuild kit (the layout of the git repo and of the .tar.gz)."""
        b = self.bundle()
        files = {"screens.json": _dump(b["screens"]), "config.json": _dump(b["config"]), "README.md": README.encode(),
                 "restore.sh": RESTORE_SH.encode()}
        code = self.code_info()
        files["code.json"] = _dump({k: v for k, v in code.items() if k != "patch"})
        if code.get("patch"):
            files["code.patch"] = code["patch"].encode()
        home = os.path.expanduser("~")
        for f in DESKTOP_FILES:
            p = os.path.join(home, ".config", f)
            if os.path.isfile(p):
                files["system/desktop/" + f] = _bytes(p)
        for p in glob.glob("/etc/chrony/sources.d/*.sources"):
            files["system/chrony/" + os.path.basename(p)] = _bytes(p)
        if os.path.isfile("/boot/firmware/config.txt"):
            files["system/boot-config.txt"] = _bytes("/boot/firmware/config.txt")
        tz = _run(["timedatectl", "show", "-p", "Timezone", "--value"]).strip()
        if tz:
            files["system/timezone"] = (tz + "\n").encode()
        pip = _run(["pip3", "freeze", "--user"])
        if pip:
            files["system/pip-user.txt"] = pip.encode()
        pk = _run(["dpkg-query", "-W", "-f=${Package} ${Version}\n", *PACKAGES])
        if pk:
            files["system/packages.txt"] = pk.encode()
        osname = re.search(r'^PRETTY_NAME="?([^"\n]*)', open("/etc/os-release").read() if os.path.exists("/etc/os-release") else "", re.M)
        files["system/manifest.json"] = _dump({"created": b["created"], "host": b["host"], "home": home,
                                               "os": osname[1] if osname else None, "arch": os.uname().machine})
        return files

    def code_info(self):
        """Which code this display runs: the app folder is a checkout of the code repository."""
        def git(*a):
            r = subprocess.run(["git", "-C", APP_DIR, *a], capture_output=True, text=True, timeout=30)
            return r.stdout.strip() if r.returncode == 0 else None
        commit = git("rev-parse", "HEAD")
        if not commit:
            return {"repo": None, "commit": None, "clean": False, "note": "the app folder is not a git checkout"}
        changed = [ln[3:] for ln in (git("status", "--porcelain", "--untracked-files=no") or "").splitlines()]
        out = {"repo": git("remote", "get-url", "origin"), "commit": commit, "short": commit[:7], "clean": not changed,
               "changed": changed, "date": git("log", "-1", "--format=%cI")}
        if changed:
            out["patch"] = git("diff", "HEAD") or ""
        return out

    def tarball(self):
        files = self.kit()
        files["backup.json"] = _dump(self.bundle())
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for rel, data in sorted(files.items()):
                ti = tarfile.TarInfo("rackview-backup/" + rel)
                ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o755 if rel.endswith(".sh") else 0o644
                t.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    @staticmethod
    def bundle_from_file(raw):
        """A backup .json, or a .tar.gz kit (backup.json inside, or screens.json + config.json)."""
        if raw[:2] != b"\x1f\x8b":
            try:
                return json.loads(raw.decode("utf-8-sig"))
            except ValueError:
                raise ValueError("That file isn't a RackView backup.")
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as t:
                m = {x.name.split("/", 1)[-1]: x for x in t.getmembers() if x.isfile()}
                load = lambda n: json.load(t.extractfile(m[n])) if n in m else None
                if "backup.json" in m:
                    return load("backup.json")
                if "screens.json" in m:
                    return {"format": "rackview-backup", "version": 1, "screens": load("screens.json"),
                            "config": load("config.json")}
        except (tarfile.TarError, OSError, ValueError):
            pass
        raise ValueError("No RackView backup found in that archive.")

    def restore(self, b, parts=("screens", "config")):
        """Apply a bundle. Returns True when site settings changed (RackView must restart)."""
        if not isinstance(b, dict) or b.get("format") != "rackview-backup":
            raise ValueError("That isn't a RackView backup file.")
        restart = False
        if "screens" in parts and b.get("screens") is not None:
            err = self.validate(b["screens"])
            if err:
                raise ValueError("The backup's screens are invalid: " + err)
            _write(self.screens_file, b["screens"])
        if "config" in parts and isinstance(b.get("config"), dict):
            cur = _read(self.config_file, {}) or {}
            new = dict(sanitize(b["config"]))
            new.update({k: v for k, v in cur.items() if SECRET_RE.search(k)})     # keep this Pi's secrets
            if new != cur:
                _write(self.config_file, new, 0o600)
                restart = True
        return restart

    # ------------------------------------------------------------ version history
    def history(self):
        out = []
        names = [n[:-5] for n in os.listdir(self.hist_dir) if n.endswith(".json")]
        for vid in sorted(names, reverse=True):
            name = vid + ".json"
            b = _read(os.path.join(self.hist_dir, name), {}) or {}
            scr = (b.get("screens") or {}).get("screens") or []
            out.append({"id": name[:-5], "time": b.get("created"), "reason": b.get("reason", ""), "screens": len(scr)})
        return out

    def snapshot(self, reason):
        """Keep a copy of what is on disk now (skipped when nothing changed since the newest copy)."""
        with self.lock:
            b = self.bundle()
            b["reason"] = reason
            h = self.history()
            if h:
                last = _read(os.path.join(self.hist_dir, h[0]["id"] + ".json"), {}) or {}
                if last.get("screens") == b["screens"] and last.get("config") == b["config"]:
                    return None
            stamp, n = time.strftime("%Y%m%d-%H%M%S"), 1
            while os.path.exists(os.path.join(self.hist_dir, f"{stamp}-{n:02d}.json")):
                n += 1
            vid = f"{stamp}-{n:02d}"                      # same-second saves still sort in order
            _write(os.path.join(self.hist_dir, vid + ".json"), b, 0o600)
            for old in self.history()[KEEP:]:
                os.remove(os.path.join(self.hist_dir, old["id"] + ".json"))
            return vid

    def version(self, vid):
        if not re.fullmatch(r"[0-9-]+", vid or ""):
            raise ValueError("bad version id")
        b = _read(os.path.join(self.hist_dir, vid + ".json"))
        if not b:
            raise ValueError("That version no longer exists.")
        return b

    def saved(self, reason):
        """Call after screens.json or config.json was written."""
        if self.snapshot(reason) and self.git_url():
            self.request_push()

    # ------------------------------------------------------------ git
    def git_url(self):
        return (_read(self.config_file, {}) or {}).get("backup_git_url") or ""

    def set_git_url(self, url):
        url = (url or "").strip()
        if url and not re.fullmatch(r"(git@[\w.-]+:[\w./-]+?(\.git)?|ssh://[\w@.:/-]+)", url):
            raise ValueError("Use the SSH address, like git@github.com:you/rackview-config.git")
        cur = _read(self.config_file, {}) or {}
        cur["backup_git_url"] = url
        _write(self.config_file, cur, 0o600)
        if url:
            self.pubkey()
            self.request_push()
        else:
            self._set(last_error=None)

    def pubkey(self):
        if not os.path.exists(self.key):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"rackview@{socket.gethostname()}",
                            "-f", self.key], check=True, capture_output=True)
        with open(self.key + ".pub") as f:
            return f.read().strip()

    def request_push(self, message=None):
        self.message = message
        self.wake.set()

    def _set(self, **kw):
        self.status.update(kw)
        _write(self.status_file, {k: v for k, v in self.status.items() if k != "busy"})

    def _git(self, *args, check=True):
        env = dict(os.environ, GIT_SSH_COMMAND=f"ssh -i {self.key} -o IdentitiesOnly=yes -o BatchMode=yes "
                                               "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15",
                   GIT_TERMINAL_PROMPT="0")
        r = subprocess.run(["git", "-C", self.repo, *args], env=env, capture_output=True, text=True, timeout=90)
        if check and r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip()
                               else f"git {args[0]} failed")
        return r

    def _public_on_github(self, url):
        m = re.match(r"git@github\.com:([\w.-]+)/([\w.-]+?)(\.git)?$", url)
        if not m:
            return False
        try:
            r = requests.get(f"https://api.github.com/repos/{m[1]}/{m[2]}", timeout=8)
            return r.status_code == 200 and not r.json().get("private", True)
        except requests.RequestException:
            return False

    def _prepare(self, url):
        if not os.path.isdir(os.path.join(self.repo, ".git")):
            os.makedirs(self.repo, exist_ok=True)
            subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True, capture_output=True)
        self._git("config", "user.name", f"RackView on {socket.gethostname()}")
        self._git("config", "user.email", f"rackview@{socket.gethostname()}.local")
        self._git("config", "core.autocrlf", "false")
        if self._git("remote", "get-url", "origin", check=False).returncode:
            self._git("remote", "add", "origin", url)
        else:
            self._git("remote", "set-url", "origin", url)
        f = self._git("fetch", "-q", "origin", "main", check=False)
        if f.returncode == 0:
            self._git("reset", "-q", "--hard", "origin/main")          # the repo just mirrors the remote
        elif "couldn't find remote ref" not in (f.stderr or ""):       # empty repo is fine, other errors are not
            raise RuntimeError((f.stderr or "fetch failed").strip().splitlines()[-1])

    def push(self, message="Backup"):
        url = self.git_url()
        if not url:
            raise RuntimeError("No git repository set.")
        if self._public_on_github(url):
            raise RuntimeError("That GitHub repository is PUBLIC. Make it private first; the backup has your IPs.")
        self.pubkey()
        with self.lock:
            self._prepare(url)
            for d in ("app", "system", "rebuild.sh", "code.patch"):        # rewritten whole, so deleted files go away too
                if os.path.isfile(os.path.join(self.repo, d)):
                    os.remove(os.path.join(self.repo, d))
                shutil.rmtree(os.path.join(self.repo, d), ignore_errors=True)
            for rel, data in self.kit().items():
                p = os.path.join(self.repo, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(data)
                if rel.endswith(".sh"):
                    os.chmod(p, 0o755)
            self._git("add", "-A")
            if self._git("diff", "--cached", "--quiet", check=False).returncode:
                self._git("commit", "-q", "-m", message)
            self._git("push", "-q", "origin", "HEAD:main")
            head = self._git("rev-parse", "--short", "HEAD").stdout.strip()
        self._set(last_ok=time.strftime("%Y-%m-%dT%H:%M:%S%z"), last_error=None, last_commit=head)

    def _worker(self):
        while True:
            self.wake.wait()
            time.sleep(3)                  # several saves in a row become one commit
            self.wake.clear()
            reason = self.message or (self.history() or [{}])[0].get("reason") or "Backup"
            self.message = None
            self.status["busy"] = True
            try:
                self.push(reason)
            except Exception as e:
                self._set(last_error=str(e), last_try=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
                print(time.strftime("%H:%M:%S"), "backup:", e, flush=True)
            finally:
                self.status["busy"] = False

    def git_log(self):
        with self.lock:
            self._prepare(self.git_url())
            r = self._git("log", "-n", "40", "--format=%h|%cI|%s", "origin/main", check=False)
        return [dict(zip(("sha", "time", "msg"), line.split("|", 2))) for line in r.stdout.splitlines() if line]

    def git_bundle(self, sha):
        if not re.fullmatch(r"[0-9a-f]{4,40}", sha or ""):
            raise ValueError("bad commit")
        with self.lock:
            scr = json.loads(self._git("show", f"{sha}:screens.json").stdout)
            conf = self._git("show", f"{sha}:config.json", check=False)
        return {"format": "rackview-backup", "version": 1, "screens": scr,
                "config": json.loads(conf.stdout) if conf.returncode == 0 else None}

    def info(self):
        url = self.git_url()
        code = self.code_info()
        code.pop("patch", None)
        return {"history": self.history(), "keep": KEEP, "code": code,
                "git": {"url": url, "pubkey": self.pubkey() if url else None,
                        **{k: self.status.get(k) for k in ("last_ok", "last_error", "last_commit", "last_try", "busy")}}}
