"""RackView web builder server (Flask): the screen builder page plus a small JSON API.

Security: the first visitor creates the password (stored hashed); the touchscreen's Help screen can
reset it, after which the next visitor sets a new one. Every page and API call needs a login.
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import threading
import time

import requests
import backup
from flask import Flask, Response, jsonify, redirect, request, send_file, session

BASE = os.path.dirname(os.path.abspath(__file__))
PASS_FILE = os.path.expanduser("~/.config/rackview/web_password")
SECRET_FILE = os.path.expanduser("~/.config/rackview/web_secret")
SETTINGS_KEYS = ("rail_side", "door_sleep_delay", "door_closed_idle", "screen_off_after", "home_after",
                 "long_press", "wled_host", "door_open_preset", "door_closed_preset", "time_server",
                 "mqtt_host", "mqtt_user")


def _secret():
    try:
        with open(SECRET_FILE) as f:
            return f.read().strip()
    except OSError:
        s = secrets.token_hex(32)
        os.makedirs(os.path.dirname(SECRET_FILE), exist_ok=True)
        with open(os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            f.write(s)
        return s


def has_password():
    return os.path.exists(PASS_FILE)


def set_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    with open(os.open(PASS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"salt": salt, "hash": h}, f)


def check_password(pw):
    try:
        with open(PASS_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return False
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(d["salt"]), 200_000).hex()
    return hmac.compare_digest(h, d["hash"])


def reset_password():
    try:
        os.remove(PASS_FILE)
    except OSError:
        pass


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RackView Builder</title><style>
:root{--bg:#eef1f4;--panel:#fff;--fg:#17202a;--muted:#5d6b7a;--accent:#0b7a83;--line:#d6dce3;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0f1418;--panel:#161d23;--fg:#e3e9ef;--muted:#93a2b0;--accent:#3cc6cf;--line:#2b3640;color-scheme:dark}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif;display:grid;place-items:center;min-height:100vh;padding:16px;box-sizing:border-box}
form{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px;width:min(360px,100%);display:flex;flex-direction:column;gap:12px}
h1{font-size:19px;margin:0}p{color:var(--muted);margin:0;font-size:13.5px;line-height:1.45}
input{font:inherit;padding:9px 11px;border-radius:8px;border:1px solid var(--line);background:transparent;color:inherit}
button{font:inherit;font-weight:700;padding:10px;border-radius:8px;border:0;background:var(--accent);color:#fff;cursor:pointer}
.err{color:#c62828;font-size:13px}</style></head><body><form method="post"><h1>[[title]]</h1><p>[[text]]</p>[[fields]]
<span class="err">[[err]]</span><button type="submit">[[button]]</button></form></body></html>"""


def form_page(title, text, fields, button, err=""):
    out = PAGE
    for k, v in dict(title=title, text=text, fields=fields, button=button, err=err).items():
        out = out.replace("[[" + k + "]]", v)
    return out


def create_app(ui):
    import rackview as rv
    import engine as eng
    app = Flask(__name__)
    app.secret_key = _secret()
    app.config.update(SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_HTTPONLY=True)
    cache = {"ents": (0, None)}

    @app.before_request
    def guard():
        if os.environ.get("RACKVIEW_WEB_NOAUTH") == "1":         # automated tests on a separate test instance only
            return None
        if request.path in ("/setup", "/login"):
            return None
        if not has_password():
            return (jsonify(error="setup"), 401) if request.path.startswith("/api/") else redirect("/setup")
        if session.get("ok") != _pw_stamp():
            if request.path.startswith("/api/"):
                return jsonify(error="login"), 401
            return redirect("/login?next=/mirror" if request.path == "/mirror" else "/login")
        return None

    def _pw_stamp():
        try:
            return str(os.stat(PASS_FILE).st_mtime_ns)          # changes when the password is reset or replaced
        except OSError:
            return None

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if has_password():
            return redirect("/login")
        err = ""
        if request.method == "POST":
            a, b = request.form.get("pw", ""), request.form.get("pw2", "")
            if len(a) < 6:
                err = "Use at least 6 characters."
            elif a != b:
                err = "The two passwords don't match."
            else:
                set_password(a)
                session["ok"] = _pw_stamp()
                return redirect("/")
        return form_page("Set the builder password",
                         "This is the first visit, so choose the password for editing RackView's screens. "
                         "You can reset it later from the touchscreen's Help screen.",
                         '<input type="password" name="pw" placeholder="New password" autofocus required minlength="6">'
                         '<input type="password" name="pw2" placeholder="Repeat password" required>', "Save password", err)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not has_password():
            return redirect("/setup")
        err = ""
        if request.method == "POST":
            if check_password(request.form.get("pw", "")):
                session["ok"] = _pw_stamp()
                session.permanent = True
                return redirect("/mirror" if request.args.get("next") == "/mirror" else "/")
            time.sleep(1)
            err = "Wrong password."
        return form_page("RackView Builder", "Enter the builder password. Forgot it? Reset it on the touchscreen: "
                         "Help → Reset builder password.",
                         '<input type="password" name="pw" placeholder="Password" autofocus required>', "Log in", err)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.route("/")
    def index():
        return send_file(os.path.join(BASE, "builder.html"))

    # ---------------------------------------------------------------- config
    @app.route("/api/config", methods=["GET"])
    def get_config():
        return jsonify(ui.screens_cfg)

    @app.route("/api/config", methods=["PUT"])
    def put_config():
        cfg = request.get_json(silent=True)
        err = validate(cfg)
        if err:
            return jsonify(error=err), 400
        tmp = rv.SCREENS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
        os.replace(tmp, rv.SCREENS_FILE)
        ui.web_q.put(("reload",))
        bk.saved("Saved screens")
        return jsonify(ok=True)

    def validate(cfg):
        if not isinstance(cfg, dict) or not isinstance(cfg.get("screens"), list) or not cfg["screens"]:
            return "Config needs at least one screen."
        ids = set()
        for s in cfg["screens"]:
            if not isinstance(s, dict) or not s.get("id") or not isinstance(s.get("root"), dict):
                return "Every screen needs an id and a layout."
            if s["id"] in ids:
                return f"Two screens use the id {s['id']}."
            ids.add(s["id"])
        return None

    bk = backup.Backup(rv.CONFIG_FILE, rv.SCREENS_FILE, validate)

    @app.route("/api/settings", methods=["GET"])
    def get_settings():
        out = {k: rv.CFG.get(k, getattr(rv, k.upper(), None)) for k in SETTINGS_KEYS}
        out["mqtt_password_set"] = bool(rv.CFG.get("mqtt_password"))        # the password itself is never sent back
        return jsonify(out)

    @app.route("/api/settings", methods=["PUT"])
    def put_settings():
        new = request.get_json(silent=True) or {}
        try:
            with open(rv.CONFIG_FILE, encoding="utf-8") as f:
                cur = json.load(f)
        except (OSError, ValueError):
            cur = {}
        for k in SETTINGS_KEYS:
            if k in new:
                cur[k] = new[k]
        if new.get("mqtt_password"):
            cur["mqtt_password"] = new["mqtt_password"]
        tmp = rv.CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, indent=2)
        os.replace(tmp, rv.CONFIG_FILE)
        bk.saved("Changed settings")
        threading.Timer(1.0, lambda: os._exit(0)).start()      # run.sh restarts RackView with the new settings
        return jsonify(ok=True, restarting=True)

    # ---------------------------------------------------------------- data for the builder
    @app.route("/api/entities")
    def entities():
        t, data = cache["ents"]
        if not data or time.time() - t > 20:
            r = ui.data.http.get(rv.HA_URL + "/api/states", timeout=15)
            r.raise_for_status()
            data = [slim(s) for s in r.json()]
            data.sort(key=lambda x: x["id"])
            cache["ents"] = (time.time(), data)
        return jsonify(data)

    def slim(s):
        a = s.get("attributes") or {}
        attrs = {}
        for k, v in a.items():
            if k in ("friendly_name", "unit_of_measurement", "icon", "supported_features", "entity_picture"):
                continue
            if isinstance(v, list):
                attrs[k] = {"list": len(v), "sample": v[:2]}
            elif isinstance(v, dict):
                attrs[k] = {"dict": list(v)[:12], "sample": {kk: v[kk] for kk in list(v)[:3]}}
            elif isinstance(v, str) and len(v) > 80:
                attrs[k] = v[:80] + "…"
            else:
                attrs[k] = v
        return {"id": s["entity_id"], "name": a.get("friendly_name") or s["entity_id"], "state": s.get("state"),
                "unit": a.get("unit_of_measurement") or "", "attrs": attrs}

    @app.route("/api/sources")
    def sources():
        m = ui.model or {}
        return jsonify({
            "idrac": [{"id": s["id"], "name": s["name"]} for s in m.get("servers", [])],
            "fancontrol": [{"id": f.get("id"), "name": f["name"]} for f in m.get("fanctl", [])],
            "builtin": [{"id": k, "name": v} for k, v in rv.UI.TITLES.items()],
        })

    @app.route("/api/items", methods=["POST"])
    def items():
        """Items a repeater/list/chips source produces right now (for the builder's preview)."""
        p = request.get_json(silent=True) or {}
        try:
            its = ui.engine.items(p.get("props") or {}, p.get("item"))
        except Exception as e:
            return jsonify(error=str(e)), 400
        return jsonify(json.loads(json.dumps(its[:50], default=str)))

    @app.route("/api/resolve", methods=["POST"])
    def resolve():
        """Current values of refs for the builder preview: {refs: [...], item: {...}}."""
        p = request.get_json(silent=True) or {}
        item = p.get("item")
        out = {}
        for ref in p.get("refs") or []:
            try:
                v = ui.engine.resolve(ref, item)
                out[ref] = {"value": v, "text": eng.fmt_value(v, ui.engine.unit(ref, item))}
            except Exception as e:
                out[ref] = {"error": str(e)}
        return jsonify(json.loads(json.dumps(out, default=str)))

    @app.route("/api/model")
    def model():
        """RackView's data model as flat paths for the builder's data picker ($ups.batt, $servers.0.name, ...)."""
        out = []

        def walk(v, path, depth):
            if isinstance(v, dict):
                for k, x in v.items():
                    if path == "" and k in ("ents", "alerts"):
                        continue
                    walk(x, f"{path}.{k}" if path else str(k), depth + 1)
            elif isinstance(v, (list, tuple)):
                out.append({"path": path, "list": True, "value": f"list of {len(v)}"})
                if v and isinstance(v[0], dict) and depth < 6:
                    walk(v[0], f"{path}.0", depth + 1)
            else:
                out.append({"path": path, "value": "" if v is None else str(v)[:40]})
        walk(ui.model or {}, "", 0)
        return jsonify(out[:3000])

    @app.route("/api/text", methods=["POST"])
    def text():
        p = request.get_json(silent=True) or {}
        try:
            return jsonify(text=ui.engine.text(p.get("text") or "", p.get("item")))
        except Exception as e:
            return jsonify(text="⚠ " + str(e))

    @app.route("/api/template", methods=["POST"])
    def template():
        p = request.get_json(silent=True) or {}
        t = eng.templ(p.get("template") or "", p.get("item"))
        try:
            r = ui.data.http.post(rv.HA_URL + "/api/template", json={"template": t}, timeout=8)
        except requests.RequestException as e:
            return jsonify(error=str(e)), 502
        if not r.ok:
            try:
                msg = r.json().get("message")
            except ValueError:
                msg = r.text
            return jsonify(error=msg or f"HTTP {r.status_code}"), 200
        return jsonify(result=r.text)

    @app.route("/api/preview.png")
    def preview():
        box = {"done": threading.Event(), "png": None}
        ui.web_q.put(("preview", request.args.get("screen", ""), request.args.get("detail") or None, box))
        if not box["done"].wait(6) or not box["png"]:
            return Response(status=503)
        return Response(box["png"], mimetype="image/png", headers={"Cache-Control": "no-store"})

    @app.route("/api/preview", methods=["POST"])
    def preview_draft():
        """Exact render of an unsaved screen: {png: base64, rects: [[id, x, y, w, h]], canvas_x}."""
        import base64
        p = request.get_json(silent=True) or {}
        if not isinstance(p.get("screen"), dict) or not isinstance(p["screen"].get("root"), dict):
            return jsonify(error="screen with a root layout required"), 400
        box = {"done": threading.Event(), "png": None, "rects": []}
        ui.web_q.put(("draft", p["screen"], p.get("detail") or None, p.get("state") or {}, box))
        if not box["done"].wait(6) or not box["png"]:
            return jsonify(error="the display did not answer"), 503
        cx = 0 if rv.RAIL_SIDE == "right" else rv.RAIL_W
        return jsonify(png=base64.b64encode(box["png"]).decode(), canvas_x=cx,
                       rects=[[nid, r[0] + cx, r[1], r[2], r[3]] for nid, r in box["rects"]])

    @app.route("/api/show", methods=["POST"])
    def show():
        p = request.get_json(silent=True) or {}
        ui.web_q.put(("show", p.get("screen"), p.get("detail")))
        return jsonify(ok=True)

    # ---------------------------------------------------------------- backup
    def applied(restart):
        if restart:
            threading.Timer(1.0, lambda: os._exit(0)).start()    # site settings changed: run.sh restarts RackView
        else:
            ui.web_q.put(("reload",))
        return jsonify(ok=True, restarting=restart)

    def parts():
        p = (request.get_json(silent=True) or {}).get("parts") or ["screens", "config"]
        return [x for x in p if x in ("screens", "config")]

    @app.route("/api/backup")
    def backup_info():
        return jsonify(bk.info())

    @app.route("/api/backup/download")
    def backup_download():
        stamp = time.strftime('%Y%m%d-%H%M')
        if request.args.get("format") == "json":            # screens + settings only
            return Response(json.dumps(bk.bundle(), indent=1), mimetype="application/json",
                            headers={"Content-Disposition": f'attachment; filename="rackview-settings-{stamp}.json"'})
        return Response(bk.tarball(), mimetype="application/gzip",
                        headers={"Content-Disposition": f'attachment; filename="rackview-backup-{stamp}.tar.gz"'})

    def uploaded(p):
        import base64
        if p.get("file"):
            raw = base64.b64decode(p["file"])
            if len(raw) > 20_000_000:
                raise ValueError("That file is too big to be a RackView backup.")
            return backup.Backup.bundle_from_file(raw)
        return p.get("bundle")

    @app.route("/api/backup/inspect", methods=["POST"])
    def backup_inspect():
        try:
            b = uploaded(request.get_json(silent=True) or {})
            if not isinstance(b, dict) or b.get("format") != "rackview-backup":
                raise ValueError("That isn't a RackView backup file.")
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(created=b.get("created"), host=b.get("host"),
                       screens=len((b.get("screens") or {}).get("screens") or []), has_config=bool(b.get("config")))

    @app.route("/api/backup/upload", methods=["POST"])
    def backup_upload():
        p = request.get_json(silent=True) or {}
        try:
            restart = bk.restore(uploaded(p), parts())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        bk.saved("Restored from an uploaded file")
        return applied(restart)

    @app.route("/api/backup/version/<vid>")
    def backup_version(vid):
        try:
            return jsonify(bk.version(vid))
        except ValueError as e:
            return jsonify(error=str(e)), 404

    @app.route("/api/backup/restore", methods=["POST"])
    def backup_restore():
        p = request.get_json(silent=True) or {}
        try:
            b = bk.version(p.get("id"))
            restart = bk.restore(b, parts())
        except ValueError as e:
            return jsonify(error=str(e)), 400
        bk.saved(f"Restored the version from {(b.get('created') or '')[:16].replace('T', ' ')}")
        return applied(restart)

    @app.route("/api/backup/git", methods=["PUT"])
    def backup_git():
        try:
            bk.set_git_url((request.get_json(silent=True) or {}).get("url"))
        except (ValueError, subprocess.CalledProcessError) as e:
            return jsonify(error=str(e)), 400
        return jsonify(bk.info())

    @app.route("/api/backup/push", methods=["POST"])
    def backup_push():
        try:
            bk.push("Manual backup")
        except Exception as e:
            bk._set(last_error=str(e), last_try=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            return jsonify(error=str(e)), 502
        return jsonify(bk.info())

    @app.route("/api/backup/git/log")
    def backup_git_log():
        try:
            return jsonify(bk.git_log())
        except Exception as e:
            return jsonify(error=str(e)), 502

    @app.route("/api/backup/git/restore", methods=["POST"])
    def backup_git_restore():
        p = request.get_json(silent=True) or {}
        try:
            restart = bk.restore(bk.git_bundle(p.get("sha")), parts())
        except Exception as e:
            return jsonify(error=str(e)), 400
        bk.saved(f"Restored git backup {p.get('sha')}")
        return applied(restart)

    # ---------------------------------------------------------------- operations: maintenance, notes, devices, history
    @app.route("/api/ops")
    def ops():
        m = ui.model or {}
        return jsonify(json.loads(json.dumps(dict(
            maint=m.get("maint"), outage=m.get("outage"), devices_new=m.get("devices_new") or [],
            devices_count=m.get("devices_count"), devices_scope=m.get("netclients_scope"),
            notes=m.get("notes") or [], history_mb=round(ui.history.size_mb(), 1)), default=str)))

    @app.route("/api/maintenance", methods=["POST"])
    def maintenance():
        on = bool((request.get_json(silent=True) or {}).get("on"))
        ui.web_q.put(("app", "maint_on" if on else "maint_off"))
        return jsonify(ok=True)

    @app.route("/api/notes", methods=["GET", "POST"])
    def notes():
        if request.method == "POST":
            text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
            if not text:
                return jsonify(error="Write something first."), 400
            n = ui.note_add(text)
            bk.saved("Note added")
            return jsonify(n)
        return jsonify(ui.notes.get("notes", []))

    @app.route("/api/notes/<nid>", methods=["PUT", "DELETE"])
    def note(nid):
        if request.method == "DELETE":
            ui.note_delete(nid)
            bk.saved("Note deleted")
        else:
            p = request.get_json(silent=True) or {}
            ui.note_update(nid, **{k: p[k] for k in ("text", "pinned") if k in p})
            bk.saved("Note changed")
        return jsonify(ok=True)

    @app.route("/api/devices/ack", methods=["POST"])
    def devices_ack():
        mac = (request.get_json(silent=True) or {}).get("mac")
        ui.device_ack(mac.lower() if mac else None)
        return jsonify(ok=True)

    @app.route("/api/history/metrics")
    def history_metrics():
        return jsonify([{"key": k, "label": l, "unit": u} for k, l, u in ui.history.metrics])

    @app.route("/api/events")
    def events():
        return jsonify(ui.history.events(int(request.args.get("limit") or 100)))

    @app.route("/api/status")
    def status():
        return jsonify(pages=ui.PAGES, page=ui.PAGES[ui.page] if ui.PAGES else None, screen_on=ui.screen_on,
                       ha_ok=bool(ui.model) and ui.model.get("ha_age") is not None and ui.model["ha_age"] < 30)

    # ---------------------------------------------------------------- live view
    @app.route("/mirror")
    def mirror_page():
        return send_file(os.path.join(BASE, "mirror.html"))

    @app.route("/api/mirror/frame")
    def mirror_frame():
        """The next JPEG of the rack screen: returned as soon as it differs from ?seq= (at most ~2 s wait).
        Asking for frames keeps the screen drawn even while the rack's panel sleeps."""
        try:
            seq = int(request.args.get("seq", -1))
        except ValueError:
            seq = -1
        ui.mirror["seen"] = time.monotonic()
        with ui.mirror_cv:
            ui.mirror_cv.wait_for(lambda: ui.mirror["seq"] != seq, timeout=2.0)
            frame, new = ui.mirror["frame"], ui.mirror["seq"]
        ui.mirror["seen"] = time.monotonic()
        if frame is None:
            return Response(status=204)
        return Response(frame, mimetype="image/jpeg", headers={"Cache-Control": "no-store", "X-Seq": str(new)})

    @app.route("/api/mirror/state")
    def mirror_state():
        page = ui.PAGES[ui.page] if ui.PAGES else None
        return jsonify(screen_on=ui.screen_on, page=ui.TITLES.get(page, page), door=ui.door, maint=bool(ui.maint),
                       rail_side=rv.RAIL_SIDE, long_press=rv.LONG_PRESS, w=rv.W, h=rv.H)

    @app.route("/api/mirror/input", methods=["POST"])
    def mirror_input():
        d = request.get_json(silent=True) or {}
        t = d.get("t")
        if t in ("down", "move", "up"):
            try:
                x = min(max(int(d["x"]), 0), rv.W - 1)
                y = min(max(int(d["y"]), 0), rv.H - 1)
            except (KeyError, TypeError, ValueError):
                return jsonify(error="x and y needed"), 400
            ui.remote_q.put((t, x, y))
        elif t in ("short", "long") and d.get("btn") in (0, 1, 2):
            ui.events.put((t, d["btn"], "remote"))
        else:
            return jsonify(error="unknown input"), 400
        ui.mirror["seen"] = time.monotonic()
        return jsonify(ok=True)

    return app


def start(ui, port):
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app = create_app(ui)
    t = threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False),
                         daemon=True)
    t.start()
    return app
