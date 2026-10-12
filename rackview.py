#!/usr/bin/env python3
"""RackView - native rack status display for a Raspberry Pi with a 7" 800x480 DSI touchscreen.

Buttons (AW9523, beside the screen):
  top    short = previous page        hold = jump to Overview
  middle short = page action          hold = screen off
  bottom short = next page            hold = help
  Any press while the screen is off just wakes it.

LEDs:
  middle       = overall rack health (green ok / amber warning / red critical,
                 pulsing red until acknowledged, blue = no data from HA)
  top / bottom = the current page's colour; with the door closed all three mirror the rack's WLED strip.

Site settings: ~/.config/rackview/config.json (see config.example.json).
"""
import json
import math
import sys
import os
import queue
import re
import signal
import subprocess
import threading
import time

import colorsys

import requests

from wled import WledLink
import socket
import struct
from collections import deque
import io
import zlib
import queue as _queue
import secrets

if __name__ == "__main__":                 # let engine.py / webui.py "import rackview" get this running module
    sys.modules.setdefault("rackview", sys.modules["__main__"])
import engine as eng  # noqa: E402

# ----------------------------------------------------------------- config
# Site settings live in ~/.config/rackview/config.json (see config.example.json); anything not set
# there falls back to the defaults below. Set RACKVIEW_CONFIG to use a different file.
CONFIG_FILE = os.environ.get("RACKVIEW_CONFIG", os.path.expanduser("~/.config/rackview/config.json"))
try:
    with open(CONFIG_FILE, encoding="utf-8") as _f:
        CFG = json.load(_f)
except FileNotFoundError:
    CFG = {}
except ValueError as _e:
    raise SystemExit(f"RackView: {CONFIG_FILE} is not valid JSON: {_e}")


def cfg(key, default):
    return CFG.get(key, default)


HA_URL = cfg("ha_url", "http://homeassistant.local:8123")
TOKEN_FILE = os.path.expanduser(cfg("ha_token_file", "~/.config/rackview/ha_token"))
UIWLED_URL = cfg("uiwled_url", "http://homeassistant.local:8080")
HA_POLL = cfg("ha_poll", 5)            # seconds
PING_POLL = cfg("ping_poll", 10)
SCREEN_OFF_AFTER = cfg("screen_off_after", 900)   # door open: idle seconds before the screen sleeps anyway
DOOR_CLOSED_IDLE = cfg("door_closed_idle", 60)    # door closed but someone woke the screen: back to sleep after
DOOR_SLEEP_DELAY = cfg("door_sleep_delay", 60)    # seconds after the door closes before the screen goes dark
HOME_AFTER = cfg("home_after", 120)               # idle seconds before returning to Overview
LONG_PRESS = cfg("long_press", 0.9)
RAIL_SIDE = cfg("rail_side", "right")             # side of the screen the physical buttons are on
ESSENTIAL_SERVERS = set(cfg("essential_servers", []))   # iDRAC server ids where powered-off = critical
SERVER_NAMES = cfg("server_names", {})                  # iDRAC server id -> display name
PING_HOSTS = [tuple(x) for x in cfg("ping_hosts", [   # (label, host or null = default gateway, critical-if-down)
    ["Gateway", None, True],
    ["Internet", "1.1.1.1", True],
    ["Home Assistant", "homeassistant.local", True],
])]
# Home Assistant entity prefixes for this site's integrations
UPS_PREFIX = cfg("ups_prefix", "ups")                     # NUT: sensor.<prefix>_status, _battery, ...
PDU_PREFIX = cfg("pdu_prefix", "usp_pdu_pro")             # UniFi PDU: sensor.<prefix>_outlet_N_outlet_power
FIREWALL_PREFIX = cfg("firewall_prefix", "opnsense")      # OPNsense: sensor.<prefix>_interface_wan_status, ...
INTERNET_ENTITY = cfg("internet_entity", "")              # optional binary_sensor that is on when internet is up
SCREENS_FILE = os.path.expanduser(cfg("screens_file", "~/.config/rackview/screens.json"))
FACTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layouts", "default_screens.json")
WEB_PORT = cfg("web_port", 8088)
ENTITY_RE = (r"^(sensor|binary_sensor|switch)\.(idrac_|fancontrol|lab_fancontrol|usw_pro_max_|"
             + "|".join(re.escape(x + "_") for x in (UPS_PREFIX, PDU_PREFIX, FIREWALL_PREFIX) if x) + ")"
             + (r"|^" + re.escape(INTERNET_ENTITY) + "$" if INTERNET_ENTITY else "")
             + r"|^button\.idrac_.+_(power_on|soft_shutdown|clear_sel)$"
             r"|^light\.(" + "|".join(re.escape(e.split(".", 1)[1]) for _, e in cfg("extra_lights", [])) + ")$"
             r"|^sensor\.(disk_free|disk_use|load_1m|memory_use_percent|processor_use|processor_temperature)$"
             r"|^sensor\.backup_\w+$|^binary_sensor\.zigbee2mqtt_bridge_connection_state$"
             r"|^fan\.(lab_)?fancontrol\d+_")

# Touch actions. Hard power off and power cycle are deliberately not offered.
SERVER_ACTIONS = [  # (key, label, button suffix, shown when)
    ("on", "Power On", "power_on", "off"),
    ("soft", "Soft Shutdown", "soft_shutdown", "on"),
    ("sel", "Clear SEL", "clear_sel", "any"),
]
UPS_TEST_SCRIPT = cfg("ups_test_script", "script.ups_self_test")
# Rack WLED controller. A door switch on its button input selects a preset: open -> one, closed -> another.
WLED_HOST = cfg("wled_host", "wled-rack.local")
DOOR_OPEN_PRESET = cfg("door_open_preset", 1)
DOOR_CLOSED_PRESET = cfg("door_closed_preset", 2)
EXTRA_LIGHTS = [tuple(x) for x in cfg("extra_lights", [])]   # [label, light entity] shown on the Lights page
SWATCHES = [(255, 255, 255), (255, 170, 90), (255, 0, 0), (0, 255, 40), (0, 110, 255), (170, 0, 255)]
HOLD_CONFIRM = 1.5

# GPS time server (Symmetricom SyncServer S250) and the devices that should follow it
TIME_SERVER = cfg("time_server", "192.168.1.44")
TIME_POLL = 10                     # the Pi's own offset measurement against the GPS clock
NTP_EPOCH = 2208988800
S250_MODEL = "SyncServer S250"     # HA device model published by the SyncServer NTP Monitor add-on
WPSD_MODEL = "WPSD Hotspot"        # HA device model published by the WPSD Hotspot Monitor add-on
PORTMAP_MODEL = "UniFi Port Map"   # HA device model published by the UniFi Port Map add-on
PORT_HUNT_SWITCH = cfg("port_hunt_switch", "48_Port")   # UIWLED switch name that gets its jack blinked
PORT_HUNT_SECONDS = cfg("port_hunt_seconds", 60)
PORT_HUNT_RGB = cfg("port_hunt_rgb", [255, 255, 255])
CONF_DIR = os.path.dirname(os.path.abspath(CONFIG_FILE))
OUTAGE_SUGGEST_MIN = cfg("outage_suggest_minutes", 15)     # runtime below which the outage screen says "shut down now"
OUTAGE_ORDER = cfg("outage_order", [])                     # iDRAC server ids, shut down first to last
MAINT_COLOR = (150, 90, 255)
MIRROR_FPS = 6                      # live view (webui /mirror): most frames a second sent to browsers
HA_SYS = ("disk_free", "disk_use", "load_1m", "memory_use_percent", "processor_use", "processor_temperature")
GPS_HEALTH = {0: "Receiver down", 1: "Unknown mode", 2: "Acquiring signal", 3: "Bad geometry",
              4: "Propagate mode", 5: "2D solution", 6: "3D solution", 7: "Position hold", 8: "Time valid"}

W, H = 800, 480
RAIL_W = 118
CW = W - RAIL_W

BG = (13, 17, 23)
CARD = (22, 27, 34)
CARD2 = (30, 36, 45)
LINE = (48, 54, 61)
TEXT = (230, 237, 243)
MUTED = (139, 148, 158)
DIM = (72, 79, 88)
OK = (63, 185, 80)
WARN = (210, 153, 34)
CRIT = (248, 81, 73)
INFO = (88, 166, 255)
SEV_COL = {0: OK, 1: WARN, 2: CRIT}
PAGE_COLORS = {
    "ports": ((0, 210, 180), (0, 110, 90)),
    "hotspot": ((255, 150, 60), (150, 45, 0)),
    "ha": ((65, 189, 245), (0, 90, 160)),
    "overview": ((220, 225, 235), (90, 90, 100)),
    "power": ((176, 120, 255), (90, 0, 150)),
    "servers": ((60, 210, 230), (0, 110, 130)),
    "network": ((90, 140, 255), (0, 30, 170)),
    "time": ((255, 110, 190), (150, 0, 70)),
    "cooling": ((150, 200, 255), (50, 90, 160)),
    "lights": ((255, 255, 255), (100, 100, 100)),     # arrows show the live strip instead
}
SEV_LED = {0: (0, 110, 0), 1: (150, 55, 0), 2: (170, 0, 0)}
STALE_LED = (0, 30, 160)

BAD = {None, "unknown", "unavailable", ""}


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fmt(v, unit="", nd=0, none="—"):
    if v is None:
        return none
    return f"{v:.{nd}f}{unit}"


# ----------------------------------------------------------------- hardware
class Panel:
    """AW9523 buttons + RGB LEDs. Runs its own thread so I2C is never shared."""
    BTN_PINS = (1, 2, 3)                              # top, middle, bottom
    LED_PINS = ((6, 7, 12), (13, 14, 15), (11, 10, 9))

    def __init__(self, events):
        self.events = events
        self.pattern = [((0, 0, 0), "steady")] * 3
        self.live = None           # callable(i) -> rgb, used by "live" mode
        self.flash_until = [0.0] * 3
        self.aw = None
        try:
            import board
            import adafruit_aw9523
            self.aw = adafruit_aw9523.AW9523(board.I2C())
            self.aw.LED_modes = 0xFFF0
            self.aw.directions = 0xFFF0
        except Exception as e:  # lets the UI run on a dev machine
            print("panel: AW9523 not available:", e, flush=True)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        down = [False] * 3
        count = [0] * 3
        t0 = [0.0] * 3
        long_fired = [False] * 3
        sent = [None] * 3
        next_led = 0.0
        while True:
            now = time.monotonic()
            if self.aw:
                try:
                    bits = self.aw.inputs
                except OSError:
                    bits = 0xFFFF
                for i, pin in enumerate(self.BTN_PINS):
                    raw = not (bits >> pin) & 1
                    if raw != down[i]:
                        count[i] += 1
                        if count[i] >= 3:  # ~30 ms debounce
                            down[i], count[i] = raw, 0
                            if raw:
                                t0[i], long_fired[i] = now, False
                                self.flash_until[i] = now + 0.12
                            elif not long_fired[i]:
                                self.events.put(("short", i))
                    else:
                        count[i] = 0
                    if down[i] and not long_fired[i] and now - t0[i] >= LONG_PRESS:
                        long_fired[i] = True
                        self.flash_until[i] = now + 0.25
                        self.events.put(("long", i))
            if now >= next_led:
                next_led = now + 0.04
                for i in range(3):
                    out = self._level(i, now)
                    if out != sent[i] and self.aw:
                        try:
                            for pin, v in zip(self.LED_PINS[i], out):
                                self.aw.set_constant_current(pin, v)
                            sent[i] = out
                        except OSError:
                            pass
            time.sleep(0.01)

    def _level(self, i, now):
        if now < self.flash_until[i]:
            return (140, 140, 140)
        rgb, mode = self.pattern[i]
        if mode == "live":
            return (self.live(i) if self.live else None) or (0, 0, 0)
        k = 1.0
        if mode == "pulse":
            k = 0.12 + 0.88 * (0.5 + 0.5 * math.sin(now * 2 * math.pi * 0.9))
        elif mode == "tick":       # flash on each second; the Pi's clock is disciplined to the GPS server
            k = 1.0 if (time.time() % 1.0) < 0.09 else 0.18
        elif mode == "glow":       # gentle wave across the three buttons
            k = 0.3 + 0.7 * (0.5 - 0.5 * math.cos((now + i * 0.7) * 2 * math.pi / 4))
        elif mode == "breathe":
            k = 0.04 + 0.96 * (0.5 - 0.5 * math.cos(now * 2 * math.pi / 5))
        return tuple(int(c * k) for c in rgb)


# ----------------------------------------------------------------- data
class Data:
    def __init__(self):
        self.lock = threading.Lock()
        self.ents = {}
        self.ha_ok_at = 0.0
        self.ha_err = "starting"
        self.switches = []
        self.pings = {}          # label -> (ms or None, consecutive failures)
        self.ping_now = threading.Event()
        self.refresh = threading.Event()
        with open(TOKEN_FILE) as f:
            self.token = f.read().strip().lstrip("﻿")
        self.http = requests.Session()
        self.http.headers["Authorization"] = "Bearer " + self.token
        gw = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True).stdout.split()
        self.gateway = gw[2] if len(gw) > 2 else "127.0.0.1"
        self.s250 = None
        self.extra_re = ""          # entities used by custom screens (engine.entity_regex)
        self.watch = set()          # entities whose full attributes custom screens need
        self.user_tpls = []         # HA templates used by custom screens
        self.tvals = {}             # template -> rendered value
        self.rules = []             # alert rules from screens.json
        self.alert_label = ""       # HA label whose binary sensors raise alerts
        self.wpsd = None
        self.portmap = None
        self.netclients = None
        self.ha_meta = {}
        self.ha_latency = None
        self.time = dict(server=None, fails=0, history=deque(maxlen=90), pi={})
        for target in (self._ha_loop, self._uiwled_loop, self._ping_loop, self._time_loop):
            threading.Thread(target=target, daemon=True).start()

    def _ha_tpl(self):
        regex = ENTITY_RE + ("|" + self.extra_re if self.extra_re else "")
        watch = json.dumps(sorted(self.watch))
        label = json.dumps(self.alert_label or "__none__")
        return ("{\"e\": [{% for s in states if s.entity_id is search('" + regex.replace("\\", "\\\\").replace("'", "\\'") + "') %}"
               "[{{ s.entity_id|tojson }},{{ s.state|tojson }},"
               "{{ s.attributes.unit_of_measurement|default('')|tojson }},"
               "{% if s.entity_id in " + watch + " %}{{ dict(s.attributes, b=s.attributes.brightness|default(none), "
               "pct=s.attributes.percentage|default(none), friendly_name=s.name) | tojson }}"
               "{% elif s.domain in ['light', 'select', 'fan'] %}{{ {'b': s.attributes.brightness|default(none), "
               "'pct': s.attributes.percentage|default(none), "
               "'rgb': s.attributes.rgb_color|default(none), 'fx': s.attributes.effect|default(none), "
               "'opts': s.attributes.options|default(none)}|tojson }}"
               "{% elif s.entity_id == 'sensor." + UPS_PREFIX + "_self_test_result' %}"
               "{{ {'lc': as_timestamp(s.last_changed)}|tojson }}{% else %}null{% endif %},{{ s.name|tojson }}]"
               "{{ ',' if not loop.last }}{% endfor %}], "
               "\"s250\": {% set ns = namespace(e=none) %}"
               "{% for e in integration_entities('mqtt') | select('is_device_attr', 'model', '" + S250_MODEL + "') %}"
               "{% if state_attr(e, 'gps_receiver') is not none or state_attr(e, 'reachable') is not none %}"
               "{% set ns.e = e %}{% endif %}{% endfor %}"
               "{% if ns.e %}{{ {'state': states(ns.e), 'attrs': (expand(ns.e) | first).attributes} | tojson }}"
               "{% else %}null{% endif %}, "
               "\"wpsd\": {% set nw = namespace(e=none) %}"
               "{% for e in integration_entities('mqtt') | select('is_device_attr', 'model', '" + WPSD_MODEL + "') %}"
               "{% if state_attr(e, 'dmr_net') is not none or state_attr(e, 'online') is not none %}"
               "{% set nw.e = e %}{% endif %}{% endfor %}"
               "{% if nw.e %}{{ {'state': states(nw.e), 'attrs': (expand(nw.e) | first).attributes} | tojson }}"
               "{% else %}null{% endif %}, "
               "\"portmap\": {% set npm = namespace(e=none) %}"
               "{% for e in integration_entities('mqtt') | select('is_device_attr', 'model', '" + PORTMAP_MODEL + "') %}"
               "{% if state_attr(e, 'clients') is not none %}{% set npm.e = e %}{% endif %}{% endfor %}"
               "{% if npm.e %}{{ {'state': states(npm.e), 'attrs': (expand(npm.e) | first).attributes} | tojson }}"
               "{% else %}null{% endif %}, "
               "\"netclients\": {% set nnc = namespace(e=none) %}"
               "{% for e in integration_entities('mqtt') | select('is_device_attr', 'model', '" + PORTMAP_MODEL + "') %}"
               "{% if state_attr(e, 'network') is not none %}{% set nnc.e = e %}{% endif %}{% endfor %}"
               "{% if nnc.e %}{{ state_attr(nnc.e, 'network') | tojson }}{% else %}null{% endif %}, "
               "\"ha\": {{ {"
               "'core': state_attr('update.home_assistant_core_update', 'installed_version'), "
               "'core_latest': state_attr('update.home_assistant_core_update', 'latest_version'), "
               "'supervisor': state_attr('update.home_assistant_supervisor_update', 'installed_version'), "
               "'os': state_attr('update.home_assistant_operating_system_update', 'installed_version'), "
               "'updates': states.update | selectattr('state', 'eq', 'on') | map(attribute='name') | list, "
               "'update_count': states.update | list | count, "
               "'unavailable': states | selectattr('state', 'eq', 'unavailable') | list | count, "
               "'total': states | list | count, "
               "'notifications': states.persistent_notification | list | count, "
               "'idrac': states.binary_sensor | selectattr('entity_id', 'search', '^binary_sensor\\.idrac_.+_available$') "
               "| map(attribute='state') | list"
               "} | tojson }}, "
               "\"labels\": [{% for e in label_entities(" + label + ") %}"
               "[{{ e|tojson }}, {{ state_attr(e, 'friendly_name')|default(e)|tojson }}, {{ states(e)|tojson }}]"
               "{{ ',' if not loop.last }}{% endfor %}]}")

    def _render_user_templates(self):
        """Render the custom screens' HA templates in one call; on an error, find the bad one(s)."""
        tpls = list(self.user_tpls)
        if not tpls:
            self.tvals = {}
            return
        body = "".join("{%% set _t%d %%}%s{%% endset %%}" % (i, t) for i, t in enumerate(tpls))
        body += "{" + ",".join('"%d": {{ _t%d | trim | tojson }}' % (i, i) for i in range(len(tpls))) + "}"
        try:
            r = self.http.post(HA_URL + "/api/template", json={"template": body}, timeout=8)
            r.raise_for_status()
            j = r.json()
            self.tvals = {t: j.get(str(i)) for i, t in enumerate(tpls)}
            return
        except Exception:
            pass
        out = {}
        for t in tpls:                       # isolate failures so one typo doesn't blank every template
            try:
                r = self.http.post(HA_URL + "/api/template", json={"template": t}, timeout=5)
                out[t] = r.text.strip() if r.ok else "⚠ template error"
            except Exception:
                out[t] = "⚠"
        self.tvals = out

    def _ha_loop(self):
        while True:
            try:
                tpl = self._ha_tpl()
                r = self.http.post(HA_URL + "/api/template", json={"template": tpl}, timeout=8)
                r.raise_for_status()
                j = r.json()
                ents = {row[0]: {"s": row[1], "u": row[2], "a": row[3] or {}, "n": row[4] if len(row) > 4 else None}
                        for row in j["e"]}
                with self.lock:
                    self.ents, self.s250, self.ha_ok_at, self.ha_err = ents, j.get("s250"), time.time(), ""
                    self.wpsd, self.ha_meta, self.ha_latency = j.get("wpsd"), j.get("ha") or {}, r.elapsed.total_seconds()
                    self.portmap = j.get("portmap")
                    self.netclients = j.get("netclients")
                    self.labels = j.get("labels") or []
                self._render_user_templates()
            except Exception as e:
                with self.lock:
                    self.ha_err = str(e)[:80]
            self.refresh.wait(HA_POLL)
            self.refresh.clear()

    def call(self, domain, service, data, done=None, quiet=False):
        """Call an HA service in the background; done(ok, message) runs on completion."""
        def run():
            try:
                r = requests.post(f"{HA_URL}/api/services/{domain}/{service}", json=data, timeout=10,
                                  headers={"Authorization": "Bearer " + self.token})
                r.raise_for_status()
                ok, msg = True, ""
            except Exception as e:
                ok, msg = False, str(e)[:60]
            if not quiet or not ok:
                print(time.strftime("%H:%M:%S"), f"call {domain}.{service} {data} -> {'ok' if ok else msg}", flush=True)
            time.sleep(0.6)
            self.refresh.set()
            if done:
                done(ok, msg)
        threading.Thread(target=run, daemon=True).start()

    def _uiwled_loop(self):
        while True:
            try:
                sw = requests.get(UIWLED_URL + "/api/switches", timeout=5).json()
                with self.lock:
                    self.switches = sw
            except Exception:
                pass
            time.sleep(30)

    def _ping_loop(self):
        while True:
            procs = []
            for label, host, _ in PING_HOSTS:
                host = host or self.gateway
                p = subprocess.Popen(["ping", "-n", "-c", "1", "-W", "2", host],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                procs.append((label, p))
            for label, p in procs:
                out = p.communicate()[0]
                m = re.search(r"time=([\d.]+)", out)
                with self.lock:
                    fails = self.pings.get(label, (None, 0))[1]
                    self.pings[label] = (float(m.group(1)), 0) if m else (None, fails + 1)
            self.ping_now.wait(PING_POLL)
            self.ping_now.clear()

    @staticmethod
    def ntp_query(host, timeout=2.0):
        """One NTP v4 client exchange. Returns header fields plus offset/delay in seconds."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            t1 = time.time()
            sec = int(t1 + NTP_EPOCH)
            frac = int((t1 % 1) * 2 ** 32)
            sock.sendto(b"\x23" + 39 * b"\0" + struct.pack("!II", sec, frac), (host, 123))
            d, _ = sock.recvfrom(512)
            t4 = time.time()
        finally:
            sock.close()
        if len(d) < 48:
            raise ValueError("short NTP reply")

        def ts(off):
            a, b = struct.unpack("!II", d[off:off + 8])
            return a - NTP_EPOCH + b / 2 ** 32 if a else None
        stratum = d[1]
        ref = d[12:16]
        refid = ref.decode("ascii", "replace").strip("\0") if stratum <= 1 else ".".join(map(str, ref))
        rd, rdisp = struct.unpack("!II", d[4:12])
        t2, t3, tref = ts(32), ts(40), ts(16)
        return dict(leap=d[0] >> 6, stratum=stratum, poll=struct.unpack("!b", d[2:3])[0],
                    precision=struct.unpack("!b", d[3:4])[0], root_delay=rd / 65536, root_disp=rdisp / 65536,
                    refid=refid, ref_age=(t4 - tref) if tref else None,
                    offset=((t2 - t1) + (t3 - t4)) / 2, delay=(t4 - t1) - (t3 - t2))

    def _time_loop(self):
        n = 0
        while True:
            try:
                r = self.ntp_query(TIME_SERVER)
                with self.lock:
                    self.time["server"], self.time["fails"] = r, 0
                    self.time["history"].append((time.time(), r["offset"], r["delay"]))
            except Exception:
                with self.lock:
                    self.time["fails"] += 1
            if n % 6 == 0:          # every ~minute: the Pi's own sync status
                pi = {}
                try:
                    f = subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True,
                                       timeout=5).stdout.strip().split(",")
                    if len(f) >= 14:
                        pi = dict(client="chrony", server=f[1], synced=f[13] != "Not synchronised",
                                  stratum=int(f[2]), est=abs(float(f[4])), rms=float(f[6]))
                except Exception:
                    pass
                if not pi:
                    try:
                        out = subprocess.run(["timedatectl", "show-timesync", "-p", "ServerAddress", "--value"],
                                             capture_output=True, text=True, timeout=5).stdout.strip()
                        synced = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                                capture_output=True, text=True, timeout=5).stdout.strip()
                        pi = dict(client="timesyncd", server=out, synced=synced == "yes")
                    except Exception:
                        pass
                with self.lock:
                    self.time["pi"] = pi
            n += 1
            time.sleep(TIME_POLL)

    def snapshot(self):
        with self.lock:
            t = dict(self.time, history=list(self.time["history"]), s250=self.s250, wpsd=self.wpsd, portmap=self.portmap, netclients=self.netclients,
                     labels=list(getattr(self, "labels", [])), rules=list(self.rules), tvals=dict(self.tvals),
                     ha_meta=dict(self.ha_meta), ha_latency=self.ha_latency)
            return dict(self.ents), self.ha_ok_at, self.ha_err, list(self.switches), dict(self.pings), t


def local_ip():
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("1.1.1.1", 80))
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except OSError:
        return "rackviewer.local"


# ----------------------------------------------------------------- model
class Alert:
    def __init__(self, sev, page, key, text, detail=""):
        self.sev, self.page, self.key, self.text, self.detail = sev, page, key, text, detail


def pretty_label(key):
    words = key.replace("_temp", "").replace("_c", "").split("_")
    return " ".join(w.upper() if any(c.isdigit() for c in w) or len(w) <= 3 else w.title() for w in words)


def build_model(data):
    ents, ha_ok_at, ha_err, switches, pings, tdata = data.snapshot()

    def st(e):
        return ents.get(e, {}).get("s")

    def nv(e):
        return num(st(e))

    def unit(e):
        return ents.get(e, {}).get("u", "").replace("Â", "")

    m = {"alerts": [], "ha_age": time.time() - ha_ok_at if ha_ok_at else None, "ha_err": ha_err, "ents": ents}
    A = m["alerts"]

    # --- servers (iDRAC)
    servers = []
    for eid in ents:
        mm = re.match(r"binary_sensor\.idrac_(.+)_power_state$", eid)
        if not mm:
            continue
        n = mm.group(1)
        p = f"sensor.idrac_{n}_"
        power, health = st(eid), st(p + "overall_health")
        if power in BAD and health in BAD:
            continue  # stale/legacy device
        temps, fans = [], []
        for k in ents:
            if not k.startswith(p):
                continue
            key = k[len(p):]
            v = nv(k)
            if key.endswith("_temp") or key.endswith("_temp_c"):
                if v is not None:
                    temps.append((pretty_label(key), v, unit(k)))
            fm = re.match(r"fan_(\d+)_rpm$", key)
            if fm:
                fans.append((int(fm.group(1)), v))
        name = SERVER_NAMES.get(n, n.replace("_", " ").title())
        srv = dict(id=n, name=name, power=power, on=power == "on", health=health,
                   avail=st(f"binary_sensor.idrac_{n}_available"), os=st(f"binary_sensor.idrac_{n}_os_status"),
                   watts=nv(p + "system_power"), psu_v=nv(p + "psu_input_voltage"),
                   psu_a=nv(p + "psu_input_current"), redund=st(p + "fan_redundancy"),
                   last_error=st(p + "last_error"),
                   reason=(st(p + "health_reason") or "").replace(" | ", " · ") or None,
                   temps=sorted(temps, key=lambda t: -t[1]), fans=sorted(fans))
        srv["actions"] = [(key, label, f"button.idrac_{n}_{suffix}") for key, label, suffix, when in SERVER_ACTIONS
                          if f"button.idrac_{n}_{suffix}" in ents and when in ("any", power)]
        srv["sev"] = 0
        if srv["on"] and health not in BAD and health.lower() != "ok":
            srv["sev"] = 2 if health.lower() == "critical" else 1
            A.append(Alert(srv["sev"], "servers", f"{n}-health", f"{name}: iDRAC health {health}",
                           srv["reason"] if srv["reason"] not in BAD else ""))
        if power == "off" and n in ESSENTIAL_SERVERS:
            srv["sev"] = 2
            A.append(Alert(2, "servers", f"{n}-off", f"{name} is powered off"))
        if srv["avail"] == "off":
            srv["sev"] = max(srv["sev"], 1)
            A.append(Alert(1, "servers", f"{n}-idrac", f"{name}: iDRAC unreachable"))
        servers.append(srv)
    servers.sort(key=lambda s: (s["id"] not in ESSENTIAL_SERVERS, s["name"]))
    m["servers"] = servers

    # --- UPS
    u = f"sensor.{UPS_PREFIX}_"
    ups = dict(present=(u + "status") in ents, status=st(u + "status"), batt=nv(u + "battery"),
               load=nv(u + "load"), runtime=nv(u + "battery_runtime"), vin=nv(u + "input_voltage"),
               vout=nv(u + "output_voltage"), freq=nv(u + "input_frequency"), temp=nv(u + "ups_temperature"),
               bvolt=nv(u + "battery_voltage"), bdate=st(u + "battery_date"),
               reason=st(u + "voltage_transfer_reason"), selftest=st(u + "self_test_result"),
               selftest_lc=ents.get(u + "self_test_result", {}).get("a", {}).get("lc"))
    ups["sev"] = 0
    if ups["present"]:
        if ups["status"] in BAD:
            ups["sev"] = 1
            A.append(Alert(1, "power", "ups-nodata", "UPS: no data"))
        elif "online" not in ups["status"].lower():
            ups["sev"] = 2
            A.append(Alert(2, "power", "ups-status", f"UPS {ups['status']}"))
        on_mains = ups["status"] not in BAD and "online" in ups["status"].lower()
        # on mains a partly charged battery is normal (e.g. right after a self-test); on battery it is already red
        if ups["batt"] is not None and ups["batt"] < (60 if on_mains else 90):
            s = 2 if ups["batt"] < (40 if on_mains else 50) else 1
            ups["sev"] = max(ups["sev"], s)
            A.append(Alert(s, "power", "ups-batt", f"UPS battery {ups['batt']:.0f}%"))
        if ups["load"] is not None and ups["load"] > 80:
            ups["sev"] = max(ups["sev"], 1)
            A.append(Alert(1, "power", "ups-load", f"UPS load {ups['load']:.0f}%"))
    m["ups"] = ups

    # --- PDU
    pp = f"sensor.{PDU_PREFIX}_"
    outlets = []
    for k in ents:
        om = re.match(r"sensor\." + re.escape(PDU_PREFIX) + r"_outlet_(\d+)_outlet_power$", k)
        if om:
            o = int(om.group(1))
            outlets.append((o, nv(k), st(f"switch.{PDU_PREFIX}_outlet_{o}")))
    pdu = dict(present=bool(outlets), state=st(pp + "state"), watts=nv(pp + "ac_power_consumption"),
               budget=nv(pp + "ac_power_budget"), outlets=sorted(outlets), sev=0)
    if pdu["present"]:
        if pdu["state"] != "connected":
            pdu["sev"] = 1
            A.append(Alert(1, "power", "pdu-state", f"PDU {pdu['state'] or 'no data'}"))
        if pdu["watts"] and pdu["budget"] and pdu["watts"] > 0.8 * pdu["budget"]:
            pdu["sev"] = 1
            A.append(Alert(1, "power", "pdu-budget", f"PDU at {100 * pdu['watts'] / pdu['budget']:.0f}% of budget"))
        for o, w, sw in pdu["outlets"]:
            if sw == "off":
                pass  # shown on the page; turned off on purpose more often than not
    m["pdu"] = pdu

    # --- firewall
    fp = f"sensor.{FIREWALL_PREFIX}_"
    fw_temps = [v for k in ents if k.startswith(fp + "temp_cpu_") and (v := nv(k)) is not None]
    fw = dict(present=(fp + "interface_wan_status") in ents,
              wan_gw=st(fp + "gateway_wan_dhcp_status"), bwan_gw=st(fp + "gateway_backupwan_dhcp_status"),
              wan=st(fp + "interface_wan_status"), wwan=st(fp + "interface_wwan_status"),
              lan=st(fp + "interface_lan_status"), vpn=st(fp + "openvpn_server_openvpn_server_status"),
              wan_in=nv(fp + "interface_wan_inbytes_kilobytes_per_second"),
              wan_out=nv(fp + "interface_wan_outbytes_kilobytes_per_second"),
              lan_in=nv(fp + "interface_lan_inbytes_kilobytes_per_second"),
              lan_out=nv(fp + "interface_lan_outbytes_kilobytes_per_second"),
              rate_unit=unit(fp + "interface_wan_inbytes_kilobytes_per_second"),
              cpu=nv(fp + "cpu_usage"), mem=nv(fp + "memory_used_percentage"),
              load=nv(fp + "system_load_average_one_minute"),
              temp=max(fw_temps) if fw_temps else None, temp_u=unit(fp + "temp_cpu_0"),
              leases=nv(fp + "dhcp_leases_all"), boot=st(fp + "system_boottime"),
              notices=st(f"binary_sensor.{FIREWALL_PREFIX}_pending_notices_present"))
    fw["sev"] = 0
    if fw["present"]:
        if fw["wan"] != "up" or fw["wan_gw"] != "online":
            fw["sev"] = 2
            A.append(Alert(2, "network", "fw-wan", f"Primary WAN {fw['wan_gw'] or fw['wan'] or 'no data'}"))
        if fw["bwan_gw"] not in (None, "online"):
            fw["sev"] = max(fw["sev"], 1)
            A.append(Alert(1, "network", "fw-bwan", f"Backup WAN {fw['bwan_gw']}"))
        if fw["notices"] == "on":
            fw["sev"] = max(fw["sev"], 1)
            A.append(Alert(1, "network", "fw-notice", "Firewall has pending notices"))
    m["fw"] = fw
    m["internet"] = st(INTERNET_ENTITY) if INTERNET_ENTITY else None

    # --- switches (UniFi via HA + UIWLED for Etherlight)
    sws = []
    for k in ents:
        sm = re.match(r"sensor\.(usw_pro_max_(\d+)_poe)_state$", k)
        if not sm:
            continue
        d = sm.group(1)
        sw = dict(name=f"USW Pro Max {sm.group(2)}", state=st(k), temp=nv(f"sensor.{d}_temperature"),
                  temp_u=unit(f"sensor.{d}_temperature"), cpu=nv(f"sensor.{d}_cpu_utilization"),
                  mem=nv(f"sensor.{d}_memory_utilization"), up=st(f"sensor.{d}_uptime"), sev=0)
        if sw["state"] != "connected":
            sw["sev"] = 2
            A.append(Alert(2, "network", f"{d}-state", f"{sw['name']}: {sw['state']}"))
        sws.append(sw)
    sws.sort(key=lambda s: s["name"])
    m["switches"] = sws
    m["etherlight"] = switches

    # --- pings
    pl = []
    for label, host, crit in PING_HOSTS:
        ms, fails = pings.get(label, (None, 0))
        sev = 0 if fails < 2 else (2 if crit else 1)
        pl.append(dict(label=label, host=host or data.gateway, ms=ms, fails=fails, sev=sev))
        if sev:
            A.append(Alert(sev, "network", f"ping-{label}", f"{label} not answering ping"))
    m["pings"] = pl

    # --- fan controllers
    fcs = []
    devs = sorted({mm.group(1) for k in ents if (mm := re.match(r"sensor\.(fancontrol\d+)_\1_fan_\d+_rpm$", k))})
    for d in devs:
        pre = f"sensor.{d}_{d}_"
        mine = [k for k in ents if f"{d}_{d}_" in k]
        offline = all(ents[k]["s"] == "unavailable" for k in mine)
        fans = []
        for i in range(1, 6):
            rpm, pwm = st(pre + f"fan_{i}_rpm"), st(pre + f"fan_{i}_pwm")
            alert = any(ents[k]["s"] == "on" for k in mine if k.endswith(f"fan_{i}_alert") or k.endswith(f"fan_{i}_alarm"))
            fe = f"fan.{d}_{d}_fan_{i}"
            fans.append(dict(n=i, rpm=num(rpm), pwm=num(pwm), fitted=not (rpm in BAD and pwm in BAD), alert=alert,
                             entity=fe if fe in ents else None, state=st(fe)))
        fc = dict(id=d, name=d.replace("fancontrol", "Fan ctrl "), offline=offline, esp=nv(pre + "esp32_temp"),
                  esp_u=unit(pre + "esp32_temp"), fans=fans, sev=0)
        if offline:
            fc["sev"] = 1
            A.append(Alert(1, "cooling", f"{d}-offline", f"{fc['name']} offline"))
        off = [f["n"] for f in fans if f["fitted"] and f["state"] == "off"]
        if off and not offline:
            fc["sev"] = max(fc["sev"], 1)
            A.append(Alert(1, "cooling", f"{d}-fans-off", f"{fc['name']}: fan{'s' if len(off) > 1 else ''} "
                                                            f"{', '.join(map(str, off))} switched off"))
        for f in fans:
            if f["alert"] or (f["fitted"] and f["state"] != "off" and f["rpm"] == 0 and (f["pwm"] or 0) > 0):
                fc["sev"] = 2
                A.append(Alert(2, "cooling", f"{d}-fan{f['n']}", f"{fc['name']} fan {f['n']} stalled/alarm"))
        fcs.append(fc)
    m["fanctl"] = fcs

    # --- GPS time (S250 data from HA's SyncServer NTP Monitor add-on; Pi sync measured locally)
    srv = tdata["server"]
    tm = dict(server=srv, fails=tdata["fails"], history=tdata["history"], pi=tdata["pi"], ports=[],
              clients=[], sev=0, snmp=None, source="ha")
    h = tdata.get("s250")
    a = (h or {}).get("attrs") or {}
    live = bool(h) and h.get("state") not in BAD
    if live:
        hc = a.get("gps_health_code")
        tm["snmp"] = dict(model=a.get("model") or "S250", health_code=hc, gps_health=a.get("gps_receiver"),
                          mode=a.get("receiver_mode") or "", timing_status=a.get("timing_status"),
                          fly=int(a.get("holdover_s") or 0),
                          sat_count=a["satellites"] if isinstance(a.get("satellites"), int) else None,
                          sats=a.get("sats") or [], utc_offset=a.get("utc_offset"),
                          freq=f"{a['osc_ppm']} ppm" if a.get("osc_ppm") is not None else None,
                          firmware=a.get("firmware"), uptime=a.get("uptime"), ntp_error=a.get("ntp_error"))
        tm["ports"] = a.get("lan_ports") or []
        tm["locked"] = a.get("locked") == "on"
        status = h["state"]
        if status == "Unreachable":
            tm["sev"] = 2
            A.append(Alert(2, "time", "ntp-down", "GPS time server not answering", TIME_SERVER))
        elif status == "Not locked":
            tm["sev"] = 2
            A.append(Alert(2, "time", "ntp-unlocked", "GPS time server lost GPS lock",
                           f"stratum {a.get('stratum')}, ref {a.get('refid')}"))
        sn = tm["snmp"]
        if hc is not None and hc < 8:
            sev = 2 if hc in (0, 1, 2, 3) else 1
            tm["sev"] = max(tm["sev"], sev)
            A.append(Alert(sev, "time", "gps-health", f"GPS receiver: {GPS_HEALTH.get(hc, hc)}",
                           f"S250 mode {sn['mode']}"))
        if sn["fly"] > 0:
            tm["sev"] = max(tm["sev"], 1)
            A.append(Alert(1, "time", "gps-holdover", "Time server in holdover (no GPS)",
                           f"flywheeling for {sn['fly']} s"))
        if sn["timing_status"] and str(sn["timing_status"]).lower() != "good":
            tm["sev"] = max(tm["sev"], 1)
            A.append(Alert(1, "time", "timing-status", f"S250 timing status: {sn['timing_status']}"))
        if sn["ntp_error"] and str(sn["ntp_error"]).lower() != "no error":
            tm["sev"] = max(tm["sev"], 1)
            A.append(Alert(1, "time", "s250-ntp-error", f"S250 NTP error: {sn['ntp_error']}"))
        if a.get("leap_code") in (1, 2):
            tm["sev"] = max(tm["sev"], 1)
            A.append(Alert(1, "time", "ntp-leap", "Leap second pending", "inserted at end of month (UTC)"))
        for label, c in (a.get("clients") or {}).items():
            cstate = "gps" if c.get("state") == "on GPS" else "unreachable" if c.get("state") == "no reply" else "other"
            r = dict(stratum=c.get("stratum"), refid=c.get("refid")) if c.get("stratum") is not None else None
            tm["clients"].append(dict(label=label, host=c.get("host"), r=r, state=cstate))
            if cstate == "other":
                tm["sev"] = max(tm["sev"], 1)
                A.append(Alert(1, "time", f"ntp-client-{c.get('host')}", f"{label} not syncing from GPS clock",
                               f"stratum {c.get('stratum')} via {c.get('refid')}"))
    else:
        # add-on not running / not installed: fall back to what the Pi can see itself
        tm["source"] = "local"
        tm["locked"] = bool(srv) and tdata["fails"] < 2 and srv["stratum"] == 1 and srv["leap"] != 3
        if m["ha_age"] is not None:
            tm["sev"] = 1
            A.append(Alert(1, "time", "s250-addon", "No S250 data in HA",
                           "start the SyncServer NTP Monitor add-on"))
        if tdata["fails"] >= 2:
            tm["sev"] = 2
            A.append(Alert(2, "time", "ntp-down", "GPS time server not answering", TIME_SERVER))
        elif srv and (srv["leap"] == 3 or srv["stratum"] != 1):
            tm["sev"] = 2
            A.append(Alert(2, "time", "ntp-unlocked", "GPS time server lost GPS lock",
                           f"stratum {srv['stratum']}, ref {srv['refid']}"))
    offs = [o for _, o, _ in tdata["history"][-30:]]
    if len(offs) > 2:
        mean = sum(offs) / len(offs)
        tm["jitter"] = (sum((o - mean) ** 2 for o in offs) / len(offs)) ** 0.5
    else:
        tm["jitter"] = None
    pi = tdata["pi"]
    if pi and (not pi.get("synced") or pi.get("server") != TIME_SERVER):
        tm["sev"] = max(tm["sev"], 1)
        A.append(Alert(1, "time", "pi-ntp", "Rack Pi not synced to GPS clock", f"using {pi.get('server') or 'nothing'}"))
    if srv and tdata["fails"] < 2 and abs(srv["offset"]) > 0.05:
        tm["sev"] = max(tm["sev"], 1)
        A.append(Alert(1, "time", "pi-offset", f"Rack Pi clock off by {srv['offset'] * 1000:.0f} ms"))
    m["time"] = tm

    # --- Home Assistant itself
    hm = tdata.get("ha_meta") or {}
    ha = dict(meta=hm, latency=tdata.get("ha_latency"), sev=0,
              sys={k: nv("sensor." + k) for k in HA_SYS}, sys_u={k: unit("sensor." + k) for k in HA_SYS},
              backup_last=st("sensor.backup_last_successful_automatic_backup"),
              backup_next=st("sensor.backup_next_scheduled_automatic_backup"),
              backup_state=st("sensor.backup_backup_manager_state"),
              z2m=st("binary_sensor.zigbee2mqtt_bridge_connection_state"),
              addons=[("iDRAC poller", (lambda v: ("on" in v and "off" not in v) if v else None)(
                          [x for x in hm.get("idrac") or [] if x in ("on", "off")])),
                      ("SyncServer NTP", bool(h) and h.get("state") not in BAD),
                      ("WPSD Hotspot", bool(tdata.get("wpsd")) and tdata["wpsd"].get("state") not in BAD)])
    import datetime as _dt

    def age_h(iso):
        try:
            return (time.time() - _dt.datetime.fromisoformat(iso).timestamp()) / 3600
        except (TypeError, ValueError):
            return None
    ha["backup_age_h"] = age_h(ha["backup_last"])
    if m["ha_age"] is not None and m["ha_age"] < 30:
        if ha["backup_age_h"] is not None and ha["backup_age_h"] > 48:
            ha["sev"] = 1
            A.append(Alert(1, "ha", "ha-backup", "No successful HA backup in 2 days",
                           f"last {ha['backup_age_h'] / 24:.0f} days ago"))
        if ha["z2m"] == "off":
            ha["sev"] = 1
            A.append(Alert(1, "ha", "ha-z2m", "Zigbee2MQTT bridge disconnected"))
        df, du = ha["sys"]["disk_free"], ha["sys"]["disk_use"]
        if df is not None and du and df / (df + du) < 0.10:
            ha["sev"] = 1
            A.append(Alert(1, "ha", "ha-disk", f"HA disk {100 * du / (df + du):.0f}% full"))
        if (ha["sys"]["memory_use_percent"] or 0) > 90:
            ha["sev"] = 1
            A.append(Alert(1, "ha", "ha-mem", f"HA memory {ha['sys']['memory_use_percent']:.0f}% used"))
    m["ha"] = ha

    # --- WPSD hotspot (from the WPSD Hotspot Monitor add-on via HA)
    w = tdata.get("wpsd")
    wa = (w or {}).get("attrs") or {}
    hs = dict(present=bool(w), live=bool(w) and w.get("state") not in BAD and wa.get("online") == "on",
              a=wa, state=(w or {}).get("state"), sev=0)
    if m["ha_age"] is not None and m["ha_age"] < 30:
        if not w:
            hs["sev"] = 1
            A.append(Alert(1, "hotspot", "wpsd-addon", "No hotspot data in HA", "start the WPSD Hotspot Monitor add-on"))
        elif not hs["live"]:
            hs["sev"] = 1
            A.append(Alert(1, "hotspot", "wpsd-offline", "Hotspot offline", wa.get("host") or ""))
        elif "DMR" in (wa.get("modes") or []) and wa.get("dmr_net") != "on":
            hs["sev"] = 1
            A.append(Alert(1, "hotspot", "wpsd-dmr", "Hotspot DMR network disconnected", wa.get("dmr_master") or ""))
    m["hotspot"] = hs

    # --- port map (UniFi Port Map add-on via HA)
    pm = tdata.get("portmap")
    pa = (pm or {}).get("attrs") or {}
    m["portmap"] = dict(present=bool(pm), live=bool(pm) and pm.get("state") not in BAD,
                        clients=pa.get("clients") or [], ports=pa.get("ports") or [],
                        switch=pa.get("switch_name"), model=pa.get("switch_model"), updated=pa.get("updated"))
    nc = tdata.get("netclients")
    if nc:                         # add-on 1.1+: every client, wired and Wi-Fi
        m["netclients"] = [dict(mac=(c.get("m") or "").lower(), name=c.get("n"), ip=c.get("ip"), wired=bool(c.get("w")),
                                at=c.get("at"), net=c.get("net"), first_seen=c.get("fs")) for c in nc if c.get("m")]
        m["netclients_scope"] = "all"
    else:                          # older add-on: wired clients of the port-map switch
        m["netclients"] = [dict(mac=(c.get("mac") or "").lower(), name=c.get("name"), ip=c.get("ip"), wired=True,
                                at=f"{m['portmap']['switch'] or 'switch'} port {c.get('port')}", net=c.get("network"),
                                first_seen=None) for c in m["portmap"]["clients"] if c.get("mac")]
        m["netclients_scope"] = "switch"

    # --- lights
    def light(e):
        if e not in ents:
            return None
        a = ents[e]["a"]
        return dict(id=e, on=st(e) == "on", state=st(e), b=a.get("b"), rgb=a.get("rgb"), fx=a.get("fx"))
    m["lights"] = dict(extra=[(label, light(e)) for label, e in EXTRA_LIGHTS if e in ents])
    m["ups_test"] = UPS_TEST_SCRIPT

    if m["ha_age"] is None or m["ha_age"] > 30:
        A.append(Alert(1, "overview", "ha-stale", "No data from Home Assistant"))
    else:
        # alert rules made in the builder
        tv = tdata.get("tvals") or {}
        for i, rule in enumerate(tdata.get("rules") or []):
            ref = rule.get("ref") or rule.get("entity")
            if not ref:
                continue
            if "{{" in ref:
                v = tv.get(ref)
            elif "@" in ref:
                e_, path = ref.split("@", 1)
                v = eng.dig((ents.get(e_.strip()) or {}).get("a") or {}, path)
            else:
                v = st(ref)
            if v is not None and eng.compare(v, rule.get("op", "=="), rule.get("value", "")):
                A.append(Alert(2 if rule.get("severity") == "crit" else 1, rule.get("page") or "overview",
                               f"rule-{i}", rule.get("text") or ref, f"{ref} = {v}"))
        # binary sensors carrying the alert label in HA
        for eid, name, state in tdata.get("labels") or []:
            if state == "on":
                A.append(Alert(1, "overview", f"label-{eid}", name or eid))

    A.sort(key=lambda a: -a.sev)
    m["page_sev"] = {}
    for a in A:
        m["page_sev"][a.page] = max(m["page_sev"].get(a.page, 0), a.sev)
    m["overall"] = max((a.sev for a in A), default=0)
    m["page_sev"]["overview"] = m["overall"]
    m["stale"] = m["ha_age"] is None or m["ha_age"] > 30
    view_fields(m, data)
    return m


SEV_NAME = {0: "ok", 1: "warn", 2: "crit"}


def _f(v, unit):
    """Temperature in °F (for thresholds) from a value in °F or °C."""
    return None if v is None else (v if "F" in (unit or "") else v * 9 / 5 + 32)


def _temp_color(v, unit, warn_f):
    f = _f(v, unit)
    return "muted" if f is None else "crit" if f >= 185 else "warn" if f >= warn_f else "ok"


def view_fields(m, data):
    """Fields the screen layouts display: status colour names, ready-made rows and short texts.

    Layouts (screens.json) decide where and how things are drawn; this adds the derived values they read,
    e.g. $servers.0.color = "ok" / "warn" / "crit" / "dim".
    """
    sevc = lambda sev, other: SEV_NAME[sev] if sev else other

    # --- servers
    for s in m["servers"]:
        on, health, reason = s["on"], s["health"], s.get("reason")
        s["color"] = sevc(s["sev"], "ok" if on else "dim")
        s["health_color"] = sevc(s["sev"], "text")
        s["state_line"] = ("ON" if on else "OFF") + (f" · {health}" if health not in BAD else "")
        temps = s["temps"]
        s["temps"] = [dict(name=label, value=v, unit=u, f=_f(v, u), color=_temp_color(v, u, 158)) for label, v, u in temps]
        hot = s["temps"][0] if s["temps"] else None
        s["hot_text"] = f" · {hot['value']:.0f}{hot['unit']} {hot['name']}" if hot and on else ""
        s["fans"] = [dict(n=n, rpm=rpm, name=f"Fan {n}") for n, rpm in s["fans"]]
        s["temps_max"] = 8 if s["fans"] else 14
        s["no_temps"] = "No readings" + ("" if on else " (server off)")
        s["can"] = {key: True for key, _label, _ent in s["actions"]}
        s["essential"] = s["id"] in ESSENTIAL_SERVERS
        s["confirm"] = "hold" if s["essential"] else "confirm"
        s["soft_ess"] = bool(s["can"].get("soft")) and s["essential"]
        s["soft_norm"] = bool(s["can"].get("soft")) and not s["essential"]
        s["why"] = bool(s["sev"] and on and health not in BAD and health.lower() != "ok")
        s["reason_text"] = reason if reason not in BAD else "Cause not reported. Update the iDRAC MQTT Poller add-on to 1.0.3."
        s["reason_color"] = s["health_color"] if reason not in BAD else "dim"
        s["has_error"] = s["last_error"] not in BAD

    # --- power
    ups, pdu = m["ups"], m["pdu"]
    ups["color"] = sevc(ups["sev"], "ok")
    ups["load_color"] = "warn" if (ups["load"] or 0) > 80 else "info"
    pdu["color"] = sevc(pdu["sev"], "text")
    pdu["top"] = max([(w or 0) for _, w, _ in pdu["outlets"]] or [0]) or 1
    pdu["outlets"] = [dict(n=o, watts=w, state=sw, off=sw == "off",
                           text="off" if sw == "off" else fmt(w, " W", 0 if (w or 0) >= 10 else 1),
                           color="dim" if sw == "off" or not w else "text", num_color="dim" if sw == "off" else "muted",
                           bar_color="dim" if sw == "off" else "info") for o, w, sw in pdu["outlets"]]

    # --- network
    fw = m["fw"]

    def updown(name, v, good):
        return dict(name=name, value=v or "—", color="ok" if v == good else "crit" if v not in BAD else "muted")
    fw["links"] = [updown("WAN gateway", fw["wan_gw"], "online"), updown("Backup WAN", fw["bwan_gw"], "online"),
                   updown("LTE (WWAN)", fw["wwan"], "up"), updown("OpenVPN", fw["vpn"], "up")]
    m["inet"] = next((p for p in m["pings"] if p["label"] == "Internet"), {})
    for p in m["pings"]:
        p["color"] = sevc(p["sev"], "ok" if p["ms"] is not None else "muted")
        p["vcolor"] = p["color"] if p["sev"] else "text"
        p["none_text"] = "no reply" if p["fails"] else "…"
    ether = {e.get("hostname", ""): e for e in m["etherlight"]}
    for sw in m["switches"]:
        sw["color"] = sevc(sw["sev"], "ok")
        el = next((e for k, e in ether.items() if k.replace("-", "_").lower().startswith(sw["name"].replace(" ", "_").lower())), None)
        if el:
            on, bri = el.get("state", {}).get("On"), el.get("state", {}).get("Brightness")
            sw["etherlight"] = f"on · {round(bri * 100 / 255)}%" if on and bri is not None else ("on" if on else "off")
        else:
            sw["etherlight"] = None

    # --- cooling
    rpms = [f["rpm"] for fc in m["fanctl"] for f in fc["fans"] if f["fitted"] and f["rpm"]]
    temps = [s["temp"] for s in m["switches"] if s["temp"] is not None]
    stemps = [t["f"] for s in m["servers"] if s["on"] for t in s["temps"] if t.get("f") is not None]
    m["summary"] = dict(fan_avg=sum(rpms) / len(rpms) if rpms else None, switch_tmax=max(temps) if temps else None,
                        server_tmax=max(stemps) if stemps else None)
    for fc in m["fanctl"]:
        fc["color"] = sevc(fc["sev"], "ok")
        fc["esp_text"] = "OFFLINE" if fc["offline"] else f"ESP32 {fmt(fc['esp'], fc['esp_u'])}"
        fc["esp_color"] = "warn" if fc["offline"] else "muted"
        top = max([f["rpm"] or 0 for f in fc["fans"]] + [3000])
        for f in fc["fans"]:
            on, unknown = f["state"] == "on", f["state"] in BAD
            fit = f["fitted"] and not fc["offline"]
            f.update(name=f"Fan {f['n']}", fit=fit, on=on, live=fit and (on or unknown), off_shown=fit and not (on or unknown),
                     frac=(f["rpm"] or 0) / top if on or unknown else 0, frac_on=(f["rpm"] or 0) / top if on else 0,
                     color="crit" if f["alert"] else "info" if on or unknown else "dim",
                     label_color="text" if fit else "dim",
                     state_text="ALARM" if f["alert"] else ("running" if on else "off"),
                     state_color="crit" if f["alert"] else "ok" if on else "muted",
                     nc_text="not connected" if not fc["offline"] else "offline",
                     rpm_text=f"{fmt(f['rpm'])} rpm" if on else "—", rpm_color="text" if on else "muted",
                     row_color="card2" if fit else "card",
                     can_toggle=bool(f["entity"]) and fit and not unknown, toggle_on=bool(f["entity"]) and fit and on,
                     toggle_off=bool(f["entity"]) and fit and not on and not unknown)
        fitted = [f for f in fc["fans"] if f["fitted"]]
        ctl = [f["entity"] for f in fitted if f["entity"] and f["state"] not in BAD]
        fc["ctl"] = ctl
        fc["fitted_n"] = len(fitted)
        fc["running"] = sum(1 for f in fitted if f["state"] == "on")
        fc["any_on"] = any(f["state"] == "on" for f in fitted)
        fc["ctl_mode"] = "offline" if fc["offline"] else "none" if not ctl else "on" if fc["any_on"] else "off"
        fc["ctl_text"] = "Controller offline" if fc["offline"] else "No fan control"
    eq = [(s["name"], s["temp"], s["temp_u"]) for s in m["switches"]]
    if fw["present"]:
        eq.append(("Firewall CPU", fw["temp"], fw["temp_u"]))
    if ups["present"]:
        eq.append(("UPS", ups["temp"], "°F"))
    for s in m["servers"]:
        if s["on"] and s["temps"]:
            eq.append((f"{s['name']} (hottest)", s["temps"][0]["value"], s["temps"][0]["unit"]))
    m["equip_temps"] = [dict(name=n, value=v, unit=u, color=_temp_color(v, u, 150)) for n, v, u in eq]

    # --- time
    t = m["time"]
    srv, sn, pi = t["server"], t["snmp"], t["pi"]
    if t["fails"] >= 2:
        t["state_text"], t["state_color"] = "NO REPLY", "crit"
    elif not srv:
        t["state_text"], t["state_color"] = "WAITING…", "muted"
    elif t["locked"]:
        t["state_text"], t["state_color"] = "GPS LOCKED", "ok"
    else:
        t["state_text"], t["state_color"] = "NOT LOCKED", "crit"
    if sn:
        hc = sn["health_code"]
        used = sum(1 for x in sn["sats"] if x.get("used"))
        up = re.match(r"(\d+)(?: days?|:)", sn.get("uptime") or "")
        t["gps"] = dict(
            receiver=GPS_HEALTH.get(hc, sn.get("gps_health", "?")),
            receiver_color="ok" if hc == 8 else "warn" if hc in (4, 5, 6, 7) else "crit",
            mode={"Hold": "Position hold", "Survey": "Surveying", "Dynamic": "Dynamic"}.get(sn["mode"], sn["mode"]),
            timing=sn.get("timing_status") or "—",
            timing_color="ok" if (sn.get("timing_status") or "").lower() == "good" else "warn",
            holdover="no (GPS disciplined)" if not sn["fly"] else f"{sn['fly']} s",
            holdover_color="text" if not sn["fly"] else "warn",
            sats=f"{used} used / {sn['sat_count']} tracked" if sn["sat_count"] else ("not reported (locked)" if hc == 8 else "none"),
            sats_color="text" if sn["sat_count"] else ("dim" if hc == 8 else "crit"),
            utc=f"{sn.get('utc_offset', '?')} s (leap secs)", osc=(sn.get("freq") or "—").replace(" PPM", " ppm"),
            fw_line=f"fw {sn.get('firmware', '?')}" + (f" · up {up.group(1)} d" if up else ""))
    else:
        t["gps"] = None
    t["source_text"] = "via HA" if t["source"] == "ha" else "direct"
    if srv:
        t["srv"] = dict(stratum=f"{srv['stratum']} · ref {srv['refid']}",
                        leap={0: "none", 1: "+1 s pending", 2: "−1 s pending", 3: "unsynchronised"}[srv["leap"]],
                        leap_color="warn" if srv["leap"] else "text",
                        precision=f"±{2.0 ** srv['precision'] * 1e6:.0f} µs · {srv['root_disp'] * 1000:.2f} ms",
                        last_update="—" if srv["ref_age"] is None else f"{srv['ref_age']:.0f} s ago",
                        last_update_color="warn" if srv["ref_age"] and srv["ref_age"] > 300 else "text")
    else:
        t["srv"] = None
    t["direct"] = bool(srv) and not sn
    ok_pi = pi.get("synced") and pi.get("server") == TIME_SERVER
    t["pi_text"] = f"synced ✓ ({pi.get('client')})" if ok_pi else "NOT synced" if pi else "…"
    t["pi_color"] = "ok" if ok_pi else "warn" if pi else "dim"
    t["measured"] = bool(srv) and t["fails"] < 2
    if t["measured"]:
        off = srv["offset"] * 1000
        t["offset_text"] = f"{off * 1000:+.0f} µs" if abs(off) < 1 else f"{off:+.3f} ms"
        t["offset_sub"] = "offset (measured)" + (f" · chrony est. {pi['est'] * 1e6:.0f} µs, rms {pi['rms'] * 1e6:.0f} µs"
                                                 if pi.get("est") is not None else "")
        t["delay_text"] = f"delay {srv['delay'] * 1000:.2f} ms"
        t["jitter_text"] = f"jitter {t['jitter'] * 1000:.3f} ms" if t["jitter"] is not None else "jitter …"
    hist = t["history"]
    t["offset_us"] = srv["offset"] * 1e6 if t["measured"] else None
    t["history_text"] = f"offset history · last {max(1, round((hist[-1][0] - hist[0][0]) / 60))} min" if len(hist) > 1 else ""
    t["lan_ports"] = [dict(n=i + 1, up=p == "up", text=f"{i + 1} {'up' if p == 'up' else '—'}",
                           color="ok" if p == "up" else "dim", tcolor="ok" if p == "up" else "muted")
                      for i, p in enumerate(t["ports"])]
    for cl in t["clients"]:
        st, rr = cl["state"], cl["r"]
        cl["color"] = {"gps": "ok", "other": "warn", "unreachable": "dim", "pending": "dim"}[st]
        cl["text"] = {"gps": f"stratum {rr['stratum']} ✓" if rr else "", "other": f"via {rr['refid']}" if rr else "",
                      "unreachable": "no NTP reply", "pending": "…"}[st]
        cl["tcolor"] = "muted" if st == "gps" else cl["color"]

    # --- Home Assistant
    ha = m["ha"]
    hm, sy, su = ha["meta"], ha["sys"], ha["sys_u"]
    running = m["ha_age"] is not None and m["ha_age"] < 30
    upd_core = hm.get("core_latest") and hm.get("core_latest") != hm.get("core")
    df, du = sy["disk_free"], sy["disk_use"]
    dpct = 100 * du / (df + du) if df is not None and du else None
    try:
        import datetime as _dt
        nxt = _dt.datetime.fromisoformat(ha["backup_next"]).astimezone().strftime("%a %H:%M")
    except (TypeError, ValueError):
        nxt = "—"
    ah = ha["backup_age_h"]
    ups_list = hm.get("updates") or []
    un, nt, z = hm.get("unavailable"), hm.get("notifications"), ha["z2m"]
    ha["view"] = dict(
        state="RUNNING" if running else "UNREACHABLE", color="ok" if running else "crit",
        latency=f"{ha['latency'] * 1000:.0f} ms" if ha["latency"] else "",
        core=(hm.get("core") or "—") + (f" → {hm['core_latest']}" if upd_core else ""), core_color="warn" if upd_core else "text",
        supervisor=hm.get("supervisor") or "—", os=hm.get("os") or "—", entities=f"{hm.get('total', '—')}",
        unavailable=f"{un}" if un is not None else "—", unavailable_color="warn" if un else "text",
        notifications=f"{nt}" if nt is not None else "—", notifications_color="warn" if nt else "text",
        z2m={"on": "connected", "off": "DISCONNECTED"}.get(z, "—"), z2m_color="ok" if z == "on" else "warn" if z == "off" else "dim",
        cpu=sy["processor_use"], cpu_text=f"{fmt(sy['processor_use'], '%')} · load {fmt(sy['load_1m'], '', 2)}",
        cpu_color="warn" if (sy["processor_use"] or 0) > 80 else "info",
        mem=sy["memory_use_percent"], mem_text=fmt(sy["memory_use_percent"], "%"),
        mem_color="warn" if (sy["memory_use_percent"] or 0) > 90 else "info",
        disk=dpct, disk_text=f"{fmt(du, ' GiB', 1)} used · {fmt(df, ' GiB', 1)} free", disk_color="warn" if (dpct or 0) > 90 else "info",
        cpu_temp=fmt(sy["processor_temperature"], (su["processor_temperature"] or "").replace("Â", "") or "°", 1),
        backup_last="—" if ah is None else (f"{ah:.0f} h ago" if ah < 48 else f"{ah / 24:.0f} days ago"),
        backup_color="ok" if ah is not None and ah < 26 else "warn" if ah is not None else "dim", backup_next=nxt,
        updates=[dict(name=u.replace(" Update", "").replace(" Firmware", "")) for u in ups_list],
        updates_title=f"{len(ups_list)} update{'s' if len(ups_list) > 1 else ''} available" if ups_list else "",
        updates_more=f"+{len(ups_list) - 3} more" if len(ups_list) > 3 else "",
        all_ok=f"All {hm.get('update_count', '')} components up to date",
        addons=[dict(name=label, color="ok" if ok else "dim" if ok is None else "warn",
                     text="ok" if ok else "—" if ok is None else "no data") for label, ok in ha["addons"]])

    # --- hotspot
    hs = m["hotspot"]
    a = hs["a"]
    lv = a.get("live") or {}
    on_air = a.get("on_air") == "on"
    tgt = (lv.get("target") or a.get("last_target") or "").replace("BM: ", "")
    m_tg = re.match(r"(TG\s*\d+)\s*(.*)", tgt)
    dur = lv.get("duration") or ""
    hot = PAGE_COLORS.get("hotspot", ((255, 150, 60), None))[0]
    hot = "#%02x%02x%02x" % tuple(hot)
    rows = [(x, "static") for x in a.get("tg_static") or []] + [(x, "dynamic") for x in a.get("tg_dynamic") or []] \
        + [(x, "timed") for x in a.get("tg_timed") or []]
    tgs = []
    for x, kind in rows:
        left = int(x["expires_in"]) if "expires_in" in x else None
        tgs.append(dict(tg=x.get("tg"), name=x.get("name") or "", kind=kind,
                        label=kind + (f" · TS{x['slot']}" if x.get("slot") else ""),
                        color={"static": "ok", "dynamic": hot, "timed": "info"}[kind],
                        expires=left is not None, left=left, left_text=f"{left // 60}:{left % 60:02d} left" if left is not None else ""))
    heard = []
    for row in a.get("last_heard") or []:
        tx = (row.get("dur") or "").upper() == "TX"
        tm = (row.get("time") or "").split(" ")
        src = row.get("src") or ""
        heard.append(dict(call=row.get("callsign") or "?", src=src, src_color="ok" if src == "RF" else "dim", rf=src == "RF",
                          tx=tx, color="orange" if tx else "text", time=" ".join(tm[:2]) if len(tm) >= 2 else "",
                          tg=(row.get("target") or "").replace("BM: ", ""),
                          dur="TX" if tx else (f"{row.get('dur')}s" if row.get("dur") else ""),
                          dur_color="orange" if tx else "muted"))
    auto = a.get("tg_autostatic")
    blocked = a.get("tg_blocked") or []
    hs["view"] = dict(
        on_air=on_air, call=lv.get("call") or a.get("last_call") or "—", name=lv.get("name") or "",
        location=lv.get("location") or "", tag="ON AIR" if on_air else ("LAST CALLER" if lv.get("call") else ""),
        tag_bg="orange" if on_air else "#9e5b21", tag_color="bg" if on_air else "text",
        tg=m_tg.group(1) if m_tg else (tgt or "—"), tg_name=m_tg.group(2) if m_tg else "",
        meta=" · ".join(x for x in (lv.get("mode"), lv.get("source"), dur.split(" (")[0],
                                     f"loss {lv['loss']}" if lv.get("loss") else None) if x),
        net="linked" if a.get("dmr_net") == "on" else "DOWN", net_color="ok" if a.get("dmr_net") == "on" else "warn",
        tgs=tgs, tg_empty="No talkgroups linked" if "tg_static" in a else "Waiting for BrandMeister…",
        autostatic=f"TG {auto}" if auto else "none",
        master=a.get("dmr_master") or (f"BM {a['bm_master']}" if a.get("bm_master") else "—"),
        blocked=", ".join(map(str, blocked)),
        footer=f"{(str(round(a['freq_mhz'], 3)) + '00')[:7] + ' MHz' if a.get('freq_mhz') else '—'} · {a.get('status') or '—'} · "
               f"{fmt(a.get('cpu_temp_c'), '°C')}",
        heard=heard, host=a.get("host", ""))
    if a.get("freq_mhz"):
        hs["view"]["footer"] = f"{a['freq_mhz']:.3f} MHz · {a.get('status') or '—'} · {fmt(a.get('cpu_temp_c'), '°C')}"

    # --- lights other than the rack strip
    m["lights"]["list"] = [dict(name=label, id=lt["id"], on=lt["on"], ok=lt["state"] not in BAD, dimmable="uiwled" in lt["id"],
                                name_color="text" if lt["state"] not in BAD else "dim")
                           for label, lt in m["lights"]["extra"] if lt]


# ----------------------------------------------------------------- UI
class UI:
    PAGES = ["overview", "power", "servers", "network", "ports", "time", "hotspot", "ha", "cooling", "lights"]
    TITLES = {"overview": "Overview", "power": "Power", "servers": "Servers",
              "network": "Network", "time": "Time", "cooling": "Cooling", "lights": "Lights",
              "hotspot": "Hotspot", "ha": "Home Assistant", "ports": "Port Hunt"}

    def __init__(self):
        # SDL's native Wayland backend segfaults under this Wayfire; XWayland is solid.
        os.environ.setdefault("SDL_VIDEODRIVER", "x11")
        os.environ.setdefault("DISPLAY", ":0")
        import pygame
        self.pg = pygame
        pygame.init()
        flags = pygame.FULLSCREEN if os.environ.get("RACKVIEW_WINDOWED") is None else 0
        self.screen = pygame.display.set_mode((W, H), flags)
        pygame.display.set_caption("RackView")
        pygame.mouse.set_visible(False)
        self.canvas = pygame.Surface((CW, H))
        self._fonts = {}
        self.events = queue.Queue()
        self.panel = Panel(self.events)
        self.data = Data()
        self.page = 0
        self.srv_idx = 0
        self.screen_on = True
        self.last_input = time.monotonic()
        self.acked = set()
        self.seen_crit = set()
        self.model = None
        self.hits = []          # (rect in canvas coords, callback) rebuilt every frame
        self.dialog = None      # dict(title, body, fn, hold, danger, until)
        self.hold_start = None  # touch hold on a hold-to-confirm button
        self.help = False
        self.toast = None       # (text, colour, until)
        self.pending = {}       # action key -> time sent (button shows "Sent")
        self.ups_test = None    # running UPS self-test: {start, refreshed}
        self.output = self._find_output()
        self.engine = eng.Engine(self)
        self.custom = {}           # screen id -> screen dict (screens drawn by the engine)
        self.screens_cfg = {}
        self.cdetail = None        # (detail id, item) while a detail screen is open
        self.hist = {}             # ref -> deque[(t, value)] for history graphs
        self.web_q = _queue.Queue()
        self.remote_q = _queue.Queue()  # touches from the live view: ("down" | "move" | "up", x, y)
        self.mirror = dict(seen=0.0, at=0.0, crc=None, frame=None, seq=0)   # latest JPEG of the screen
        self.mirror_cv = threading.Condition()
        self.draft = None          # (screen dict, time) being edited in the builder
        self.outage = None         # {"since": epoch} while the UPS runs on battery
        self.maint = None          # {"since": epoch, "source": "door" | "manual"} during maintenance
        self.prev_alerts = None    # alert key -> (sev, text), for the timeline
        self.prev_power = {}       # server id -> power state, for the timeline
        self.state_lock = threading.Lock()
        import history as _history
        self.history = _history.History(os.path.join(CONF_DIR, "history.db"), cfg("history_metrics", []))
        self.notes = self._load_json("notes.json", {"notes": []})
        self.known = self._load_json("known_devices.json", None)
        st_ = self._load_json("state.json", {})
        if (st_.get("maint") or {}).get("source") == "manual":
            self.maint = st_["maint"]
        self.load_screens()
        if os.environ.get("RACKVIEW_NO_WEB") is None:
            try:
                import webui
                webui.start(self, WEB_PORT)
                print(time.strftime("%H:%M:%S"), f"builder on http://{local_ip()}:{WEB_PORT}", flush=True)
            except Exception as e:
                print("web builder failed to start:", repr(e), flush=True)
        try:
            import mqttpub
            self.mqtt = mqttpub.start(self, CFG)
        except Exception as e:
            print("mqtt publisher failed to start:", repr(e), flush=True)
        self.wled = WledLink(WLED_HOST)
        self.door = None           # "open" / "closed" (from the WLED preset the door switch selects)
        self.door_ps = None
        self.sleep_at = None       # pending door-close sleep
        self.drags = []            # (rect, fn(x, y, phase)) rebuilt every frame, like hits
        self.active_drag = None
        self.light_tab = "rack"
        self.fan_detail = None     # Cooling page: controller name shown in detail, or None
        self.hunt_sel = None       # Port Hunt: selected client MAC
        self.hunt_until = 0.0      # blinking until (monotonic)
        self.hunt_port = None
        self._hunt_last = 0.0
        self.hunt_scroll = 0
        self.list_tab = "fx"
        self.list_scroll = {"fx": 0, "pal": 0, "ps": 0}
        self.list_centered = set()
        self.seg_sel = "all"
        self.local = {}            # optimistic values while dragging: key -> (value, until)
        self._last_send = 0.0
        self._zones = (0.0, None)
        self._wheel = None
        self.panel.live = self._live_led
        self._led_page = None
        self._page_at = 0.0
        # remote testing: kill -USR1 = bottom button, kill -USR2 = middle button
        signal.signal(signal.SIGUSR1, lambda *_: self.events.put(("short", 2)))
        signal.signal(signal.SIGUSR2, lambda *_: self.events.put(("short", 1)))
        signal.signal(signal.SIGHUP, lambda *_: self.events.put(("long", 2)))
        self._set_screen(True)

    # -- helpers
    def F(self, size, bold=False):
        key = (size, bold)
        if key not in self._fonts:
            path = "/usr/share/fonts/truetype/dejavu/DejaVuSans%s.ttf" % ("-Bold" if bold else "")
            self._fonts[key] = self.pg.font.Font(path if os.path.exists(path) else None, size)
        return self._fonts[key]

    def T(self, s, size, col, pos, anchor="topleft", bold=False, maxw=None, surf=None):
        surf = surf or self.canvas
        font = self.F(size, bold)
        s = str(s)
        if maxw:
            while font.size(s)[0] > maxw and len(s) > 1:
                s = s[:-2] + "…"
        img = font.render(s, True, col)
        r = img.get_rect(**{anchor: pos})
        surf.blit(img, r)
        return r

    def wrap(self, text, size, maxw, max_lines=4):
        font, lines, line = self.F(size), [], ""
        for wd in str(text).split():
            if line and font.size(line + " " + wd)[0] > maxw:
                lines.append(line)
                line = wd
                if len(lines) == max_lines:
                    lines[-1] = lines[-1] + " …"
                    return lines
            else:
                line = (line + " " + wd).strip()
        return lines + ([line] if line else [])

    def card(self, rect, col=CARD, surf=None):
        self.pg.draw.rect(surf or self.canvas, col, rect, border_radius=10)

    def bar(self, rect, frac, col, bg=LINE):
        r = self.pg.Rect(rect)
        self.pg.draw.rect(self.canvas, bg, r, border_radius=r.h // 2)
        if frac and frac > 0:
            f = r.copy()
            f.w = max(r.h, int(r.w * min(frac, 1.0)))
            self.pg.draw.rect(self.canvas, col, f, border_radius=r.h // 2)

    def dot(self, pos, col, r=6):
        self.pg.draw.circle(self.canvas, col, pos, r)

    def tint(self, col, k=0.18):
        return tuple(int(BG[i] + (col[i] - BG[i]) * k) for i in range(3))

    def kv(self, x, y, w, label, value, col=TEXT, size=16):
        self.T(label, size - 2, MUTED, (x, y + 1))
        self.T(value, size, col, (x + w, y), "topright", maxw=w - self.F(size - 2).size(label)[0] - 12)
        return y + size + 9

    def hit(self, rect, fn):
        self.hits.append((self.pg.Rect(rect), fn))

    def button(self, rect, label, fn=None, col=INFO, filled=False, enabled=True, size=15):
        r = self.pg.Rect(rect)
        if not enabled:
            col = DIM
        if filled:
            self.pg.draw.rect(self.canvas, col, r, border_radius=8)
            self.T(label, size, BG, r.center, "center", bold=True, maxw=r.w - 10)
        else:
            self.pg.draw.rect(self.canvas, self.tint(col, 0.25), r, border_radius=8)
            self.pg.draw.rect(self.canvas, col, r, 2, border_radius=8)
            self.T(label, size, col, r.center, "center", bold=True, maxw=r.w - 10)
        if fn and enabled:
            self.hit(r.inflate(6, 6), fn)
        return r

    def notify(self, text, col=OK, secs=4):
        self.toast = (text, col, time.monotonic() + secs)

    def ask(self, title, body, fn, hold=False, danger=False):
        self.dialog = dict(title=title, body=body, fn=fn, hold=hold, danger=danger,
                           until=time.monotonic() + 20)
        self.hold_start = None

    def confirm(self):
        d, self.dialog, self.hold_start = self.dialog, None, None
        if d:
            d["fn"]()

    def send(self, key, label, domain, service, data):
        self.pending[key] = time.monotonic()

        def done(ok, msg):
            self.notify(f"{label}: sent" if ok else f"{label} failed: {msg}", OK if ok else CRIT, 5)
        self.data.call(domain, service, data, done)

    # -- screen power
    def _find_output(self):
        try:
            out = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=5).stdout
            return out.split()[0]
        except Exception:
            return "DSI-1"

    def _set_screen(self, on):
        self.screen_on = on
        try:
            subprocess.run(["wlr-randr", "--output", self.output, "--on" if on else "--off"], timeout=5)
        except Exception as e:
            print("screen:", e, flush=True)

    # -- input
    def press(self, kind, btn, remote=False):
        self.last_input = time.monotonic()
        if not self.screen_on and not remote:
            self._set_screen(True)
            return
        if self.help:
            self.help = False
            return
        if self.dialog:
            if btn == 1 and (kind == "long") == self.dialog["hold"]:
                self.confirm()
            elif btn != 1:
                self.dialog = None
            return
        n = len(self.PAGES)
        if btn == 2 and kind == "long":
            self.help = True
            return
        if btn == 0:
            self.page = 0 if kind == "long" else (self.page - 1) % n
        elif btn == 2:
            if kind == "short":
                self.page = (self.page + 1) % n
        elif btn == 1:
            if kind == "long":
                self._set_screen(False)
            else:
                self.action()

    def action_label(self):
        p = self.PAGES[self.page]
        m = self.model
        if p in self.custom:
            return self.custom_action(p, dry=True)
        return "Overview"

    def action(self):
        p = self.PAGES[self.page]
        m = self.model
        if p in self.custom:
            return self.custom_action(p)
        self.page = 0

    def _unacked(self):
        if self.maint:                             # someone is working on the rack: no alarm, no wake-ups
            return False
        return bool(self.model) and any(a.sev and a.key not in self.acked for a in self.model["alerts"])

    # -- small JSON files in the config folder (notes, known devices, state)
    def _load_json(self, name, default):
        try:
            with open(os.path.join(CONF_DIR, name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _save_json(self, name, data):
        path = os.path.join(CONF_DIR, name)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(path + ".tmp", path)

    # -- maintenance mode
    def maint_start(self, source):
        if self.maint:
            return
        self.maint = {"since": time.time(), "source": source}
        self.acked |= {a.key for a in (self.model or {}).get("alerts", [])}
        self.history.event("maint", "Maintenance started" + (" (door opened)" if source == "door" else ""), 0)
        self._save_json("state.json", {"maint": self.maint})
        self.refresh_pages()

    def maint_end(self, why=""):
        if not self.maint:
            return
        mins = (time.time() - self.maint["since"]) / 60
        self.maint = None
        self.history.event("maint", f"Maintenance ended after {mins:.0f} min" + (f" ({why})" if why else ""), 0)
        self._save_json("state.json", {"maint": None})
        self.refresh_pages()

    # -- scratchbook
    def note_add(self, text):
        with self.state_lock:
            n = {"id": secrets.token_hex(4), "ts": time.time(), "text": str(text).strip()[:500], "pinned": False}
            self.notes.setdefault("notes", []).insert(0, n)
            self._save_json("notes.json", self.notes)
        self.history.event("note", "Note: " + n["text"][:80], 0)
        return n

    def note_update(self, nid, **kw):
        with self.state_lock:
            for n in self.notes.get("notes", []):
                if n["id"] == nid:
                    n.update({k: v for k, v in kw.items() if k in ("text", "pinned")})
            self._save_json("notes.json", self.notes)

    def note_delete(self, nid):
        with self.state_lock:
            self.notes["notes"] = [n for n in self.notes.get("notes", []) if n["id"] != nid]
            self._save_json("notes.json", self.notes)

    # -- new devices
    def device_ack(self, mac=None):
        with self.state_lock:
            for k, d in (self.known or {}).items():
                if mac in (None, k):
                    d["ack"] = True
            self._save_json("known_devices.json", self.known)

    def app_command(self, cmd):
        """Actions buttons in screens can run inside RackView ("app" buttons)."""
        name, _, arg = str(cmd).partition(":")
        if name == "maint_toggle":
            self.maint_end("ended on the touchscreen") if self.maint else self.maint_start("manual")
        elif name == "maint_on":
            self.maint_start("manual")
        elif name == "maint_off":
            self.maint_end()
        elif name == "dev_ack":
            self.device_ack(arg.lower())
        elif name == "dev_ack_all":
            self.device_ack(None)
        elif name == "note_pin":
            n = next((x for x in self.notes.get("notes", []) if x["id"] == arg), None)
            if n:
                self.note_update(arg, pinned=not n.get("pinned"))
        else:
            self.notify(f"Unknown action {name}", WARN, 3)

    @staticmethod
    def when(ts):
        t = time.localtime(ts)
        return time.strftime("%H:%M", t) if time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d") else time.strftime("%b %d %H:%M", t)

    def app_model(self, m):
        """Add RackView's own state to the model (outage, maintenance, notes, new devices, timeline) and
        record history and timeline events."""
        now = time.time()
        A = m["alerts"]
        # power outage: the UPS runs on battery
        ups = m["ups"]
        st_ = str(ups.get("status") or "")
        on_batt = ups.get("present") and st_ not in BAD and (
            "on battery" in st_.lower() or st_.upper().split()[:1] == ["OB"] or ("discharg" in st_.lower() and "online" not in st_.lower()))
        if os.environ.get("RACKVIEW_FAKE_OUTAGE"):
            on_batt = True
        if on_batt and not self.outage:
            self.outage = {"since": now}
            self.history.event("outage", "Power outage: UPS on battery", 2, f"battery {fmt(ups['batt'], '%')}, runtime {fmt(ups['runtime'], ' min')}")
            self.refresh_pages()
            if "outage" in self.PAGES:
                self.goto("outage")
                self.last_input = time.monotonic()
                if not self.screen_on:
                    self._set_screen(True)
        elif not on_batt and self.outage and not os.environ.get("RACKVIEW_FAKE_OUTAGE"):
            mins = (now - self.outage["since"]) / 60
            self.outage = None
            self.history.event("outage", f"Mains back after {mins:.0f} min", 0)
            self.refresh_pages()
        o = dict(active=bool(self.outage))
        if self.outage:
            mins = (now - self.outage["since"]) / 60
            runtime = ups.get("runtime")
            order = {sid: i for i, sid in enumerate(OUTAGE_ORDER)}
            servers = sorted(m["servers"], key=lambda s: (s["essential"], order.get(s["id"], 99), s["name"]))
            plan = []
            for s in servers:
                state = "on" if s["on"] else "off" if s["power"] == "off" else "?"
                plan.append(dict(id=s["id"], name=s["name"], watts=s["watts"], essential=s["essential"],
                                 can_soft=bool(s["can"].get("soft")) and not s["essential"], on=s["on"],
                                 note="essential · keep running" if s["essential"] else ("already off" if state == "off" else "running"),
                                 color="ok" if s["essential"] and s["on"] else "dim" if not s["on"] else "warn"))
            low = runtime is not None and runtime < OUTAGE_SUGGEST_MIN
            o.update(since_text=time.strftime("%H:%M", time.localtime(self.outage["since"])), minutes=mins,
                     runtime=runtime, low=low, plan=plan,
                     advice=(f"Runtime under {OUTAGE_SUGGEST_MIN} min: shut down the non-essential servers now"
                             if low else "Runtime is fine. Shut servers down early to stretch it."),
                     advice_color="crit" if low else "muted", runtime_color="crit" if low else "text")
        m["outage"] = o
        # maintenance
        if self.maint:
            mm = (now - self.maint["since"]) / 60
            m["maint"] = dict(active=True, minutes=mm, source=self.maint["source"],
                              text=f"MAINTENANCE · {mm:.0f} min" + (" · door open" if self.maint["source"] == "door" else ""))
        else:
            m["maint"] = dict(active=False, text="")
        # scratchbook
        notes = sorted(self.notes.get("notes", []), key=lambda n: (not n.get("pinned"), -n.get("ts", 0)))
        m["notes"] = [dict(n, when=self.when(n.get("ts", 0)), color="accent" if n.get("pinned") else "card2",
                           pin_label="Unpin" if n.get("pinned") else "Pin") for n in notes]
        # new devices
        ncs = m.get("netclients") or []
        if ncs:
            with self.state_lock:
                if self.known is None:            # first time: everything on the network now is known
                    self.known = {c["mac"]: {"name": c["name"], "first": now, "ack": True} for c in ncs}
                    self._save_json("known_devices.json", self.known)
                    self.history.event("devices", f"Learned {len(ncs)} devices on the network", 0)
                fresh = [c for c in ncs if c["mac"] not in self.known]
                for c in fresh:
                    self.known[c["mac"]] = {"name": c["name"], "first": now, "ack": False}
                    self.history.event("device", f"New device: {c['name']}", 1, f"{c.get('ip') or ''} · {c.get('at') or ''}")
                if fresh:
                    self._save_json("known_devices.json", self.known)
        byk = {c["mac"]: c for c in ncs}
        new = []
        for mac, d in (self.known or {}).items():
            if not d.get("ack"):
                c = byk.get(mac, {})
                new.append(dict(mac=mac, name=c.get("name") or d.get("name") or mac, ip=c.get("ip") or "—",
                                at=c.get("at") or "not connected now", net=c.get("net") or "", when=self.when(d.get("first", now))))
                A.append(Alert(1, "timeline", f"newdev-{mac}", f"New device: {c.get('name') or d.get('name') or mac}",
                               f"{c.get('ip') or ''} {c.get('at') or ''}".strip()))
        new.sort(key=lambda d: d["when"], reverse=True)
        m["devices_new"] = new
        m["devices_count"] = len(ncs)
        m["summary"]["clients"] = len(ncs) or None
        m["summary"]["alerts"] = len(A)
        if self.maint:
            for a in A:
                self.acked.add(a.key)
        # alerts again (new devices added some), sorted, per page
        A.sort(key=lambda a: -a.sev)
        m["page_sev"] = {}
        for a in A:
            m["page_sev"][a.page] = max(m["page_sev"].get(a.page, 0), a.sev)
        m["overall"] = max((a.sev for a in A), default=0)
        m["page_sev"]["overview"] = m["overall"]
        # timeline: alerts raised / cleared, servers powered on / off
        if m["ha_age"] is not None and m["ha_age"] < 30:
            cur = {a.key: (a.sev, a.text, a.detail) for a in A if not a.key.startswith("newdev-")}
            if self.prev_alerts is not None:
                for k, (sev, text, detail) in cur.items():
                    if k not in self.prev_alerts:
                        self.history.event("alert", text, sev, detail)
                for k, (sev, text, _d) in self.prev_alerts.items():
                    if k not in cur:
                        self.history.event("clear", "Cleared: " + text, 0)
            self.prev_alerts = cur
            for sv in m["servers"]:
                was = self.prev_power.get(sv["id"])
                if was and sv["power"] not in BAD and was != sv["power"]:
                    self.history.event("power", f"{sv['name']} powered {sv['power']}", 0)
                if sv["power"] not in BAD:
                    self.prev_power[sv["id"]] = sv["power"]
        evs = self.history.events(40)
        icon = {"alert": None, "clear": "ok", "door": "info", "maint": "purple", "outage": "crit", "device": "warn",
                "devices": "info", "note": "teal", "power": "info"}
        m["events"] = [dict(e, when=self.when(e["ts"]),
                            color=icon.get(e["kind"]) or {0: "info", 1: "warn", 2: "crit"}.get(e["sev"], "info"))
                       for e in evs]
        # history: one value per metric per minute
        vals = {}
        for key, _label, _unit in self.history.metrics:
            v = eng.dig(m, key) if not key.split("@")[0] in m.get("ents", {}) else self.engine.resolve(key)
            try:
                vals[key] = float(v)
            except (TypeError, ValueError):
                pass
        self.history.record(vals)

    # -- LEDs
    def _live_led(self, i):
        t, z = self._zones
        now = time.monotonic()
        if now - t > 0.04:
            z = self.wled.zone_colours()
            self._zones = (now, z)
        return z[i] if z else None

    def update_leds(self):
        m = self.model
        if not m:
            return
        page = self.PAGES[self.page]
        now = time.monotonic()
        if page != self._led_page:
            self._led_page, self._page_at = page, now
            self.fan_detail = None
            self.cdetail = None
        alarm = m["overall"] == 2 and self._unacked()
        if not self.screen_on:
            # door closed / asleep: mirror the rack strip; a new critical alarm still pulses red in the middle
            if self.wled.zone_colours() is not None:
                mid = (SEV_LED[2], "pulse") if alarm else ((0, 0, 0), "live")
                self.panel.pattern = [((0, 0, 0), "live"), mid, ((0, 0, 0), "live")]
            else:
                mid = (SEV_LED[2], "pulse") if alarm else (STALE_LED if m["stale"] else SEV_LED[m["overall"]], "breathe")
                self.panel.pattern = [((0, 0, 0), "steady"), mid, ((0, 0, 0), "steady")]
            return

        # middle: the one place rack health is shown
        if m["stale"]:
            mid = (STALE_LED, "breathe")
        elif alarm:
            mid = (SEV_LED[2], "pulse")
        elif page == "time":
            mid = (SEV_LED[m["overall"]], "tick")
        else:
            mid = (SEV_LED[m["overall"]], "glow")

        # arrows: the current page's colour (bright on arrival, then a soft glow)
        if self.outage:
            arrow_top = arrow_bot = (SEV_LED[1], "pulse")
        elif self.maint and page != "lights":
            arrow_top = arrow_bot = (tuple(int(c * 0.6) for c in MAINT_COLOR), "glow")
        elif page == "lights" and self.wled.zone_colours() is not None:
            arrow_top = arrow_bot = ((0, 0, 0), "live")
        else:
            led = PAGE_COLORS.get(page, ((0, 0, 0), (60, 60, 60)))[1]
            on_air = page == "hotspot" and m["hotspot"]["a"].get("on_air") == "on"
            if on_air:
                arrow = (tuple(min(255, int(c * 1.5)) for c in led), "pulse")
            elif now - self._page_at < 0.6:
                arrow = (tuple(min(255, int(c * 1.6)) for c in led), "steady")
            else:
                arrow = (led, "glow")
            arrow_top = arrow_bot = arrow
        self.panel.pattern = [arrow_top, mid, arrow_bot]

    # -- door (WLED preset switched by the rack door contact)
    def check_door(self, now):
        st = self.wled.state
        if not st:
            return
        ps = st.get("ps")
        if ps == self.door_ps:
            return
        first = self.door_ps is None
        self.door_ps = ps
        if time.time() - self.wled.sent_ps_at < 5:
            return                                  # RackView changed the preset itself; not the door
        if ps == DOOR_OPEN_PRESET:
            self.door, self.sleep_at = "open", None
            if not first:
                self.history.event("door", "Rack door opened", 0)
            if not self.maint:
                self.maint_start("door")
            if not first:
                self.last_input = now
                self.page = 0
                if not self.screen_on:
                    self._set_screen(True)
        elif ps == DOOR_CLOSED_PRESET:
            self.door = "closed"
            if not first:
                self.history.event("door", "Rack door closed", 0)
            if self.maint and self.maint.get("source") == "door":
                self.maint_end("door closed")
            self.sleep_at = now + (0 if first else DOOR_SLEEP_DELAY)
            self.last_input = max(self.last_input, now - DOOR_CLOSED_IDLE + DOOR_SLEEP_DELAY)

    # -- drawing
    def draw(self):
        m = self.model
        c = self.canvas
        c.fill(BG)
        self.hits = []
        self.drags = []
        page = self.PAGES[self.page]
        self.subtitle = None
        if m:
            if page in self.custom:
                self.draw_custom(page)
            else:
                self.T("This screen has no layout", 20, MUTED, (CW // 2, H // 2), "center")
        # header
        tr = self.T(self.TITLES[page], 24, TEXT, (14, 9), bold=True)
        self.pg.draw.rect(c, PAGE_COLORS.get(page, (MUTED, None))[0], (tr.x, tr.bottom + 1, tr.w, 3), border_radius=2)
        sub = self.subtitle or ""
        if self.maint and m:
            self.pg.draw.rect(c, MAINT_COLOR, (0, 0, CW, 3))
            if not sub:
                label = m["maint"]["text"]
                cw_ = self.F(12, True).size(label)[0] + 18
                chip = self.pg.Rect(tr.right + 14, 13, cw_, 22)
                self.pg.draw.rect(c, self.tint(MAINT_COLOR, 0.45), chip, border_radius=11)
                self.T(label, 12, TEXT, chip.center, "center", bold=True)
                self.hit(chip.inflate(10, 16), lambda: self.ask("End maintenance?", ["Alerts wake the screen and pulse again."],
                                                                lambda: self.maint_end("ended on the touchscreen")))
        if page in self.custom and self.cdetail:
            d = self.detail_screen(page, self.cdetail[0])
            it = self.cdetail[1]
            sub = " · ".join(x for x in ((d or {}).get("title"), it.get("name") if isinstance(it, dict) else None) if x)
        if sub:
            sx = 14 + self.F(24, True).size(self.TITLES[page])[0] + 14
            dots_x = CW // 2 - (len(self.PAGES) * 16) // 2 + 30 - 12      # keep clear of the page dots
            self.T(sub, 18 if dots_x - sx > 160 else 14, MUTED, (sx, 14), maxw=max(60, dots_x - sx))
        self.T(time.strftime("%H:%M"), 22, TEXT, (CW - 14, 10), "topright", bold=True)
        if m:
            age = m["ha_age"]
            txt, col = ("HA offline", CRIT) if age is None or age > 30 else (f"HA {age:.0f}s", DIM)
            self.T(txt, 13, col, (CW - 90, 17), "topright")
            # page dots
            x = CW // 2 - (len(self.PAGES) * 16) // 2 + 30
            for i, p in enumerate(self.PAGES):
                col = SEV_COL[m["page_sev"].get(p, 0)] if m["page_sev"].get(p, 0) else (TEXT if i == self.page else DIM)
                self.pg.draw.circle(c, col, (x + i * 16, 22), 5 if i == self.page else 3)
            hx = CW - 190
            self.pg.draw.circle(c, CARD2, (hx, 22), 14)
            self.pg.draw.circle(c, LINE, (hx, 22), 14, 2)
            self.T("?", 17, TEXT, (hx, 22), "center", bold=True)
            self.hit((hx - 24, 0, 48, 46), lambda: setattr(self, "help", True))
        else:
            self.T("Connecting to Home Assistant…", 22, MUTED, (CW // 2, H // 2), "center")
        if self.toast and time.monotonic() < self.toast[2]:
            text, col, _ = self.toast
            w = self.F(16, True).size(text)[0] + 40
            r = self.pg.Rect(0, 0, min(w, CW - 40), 40)
            r.midbottom = (CW // 2, H - 16)
            self.pg.draw.rect(c, CARD2, r, border_radius=20)
            self.pg.draw.rect(c, col, r, 2, border_radius=20)
            self.T(text, 16, col, r.center, "center", bold=True, maxw=r.w - 20)
        if self.dialog:
            self.draw_dialog()
        if self.help:
            self.draw_help()
        self.screen.fill(CARD)
        cx = 0 if RAIL_SIDE == "right" else RAIL_W
        self.screen.blit(c, (cx, 0))
        self.draw_rail(W - RAIL_W if RAIL_SIDE == "right" else 0)
        if not getattr(self, "_composing", False):
            self.pg.display.flip()

    def draw_rail(self, x0):
        s = self.screen
        m = self.model
        n = len(self.PAGES)
        cx = x0 + RAIL_W // 2
        ps = m["page_sev"] if m else {}
        self.pg.draw.line(s, LINE, (x0 if RAIL_SIDE == "right" else x0 + RAIL_W - 1, 0),
                          (x0 if RAIL_SIDE == "right" else x0 + RAIL_W - 1, H))
        if self.dialog or self.help:
            d = self.dialog
            mid = "Close" if self.help else ("Hold: confirm" if d["hold"] else "Confirm")
            side = "Close" if self.help else "Cancel"
            mcol = MUTED if self.help else (CRIT if d["danger"] else INFO)
            for y, label, col in ((80, side, MUTED), (240, mid, mcol), (400, side, MUTED)):
                self.pg.draw.circle(s, col, (cx, y - 14), 9, 3)
                self.T(label, 15, TEXT, (cx, y + 10), "midtop", surf=s, maxw=RAIL_W - 8)
            return
        short = lambda pg_: {"ha": "HA"}.get(pg_, self.TITLES[pg_])
        for zone, (y, arrow, label, sev) in enumerate([
                (80, "▲", short(self.PAGES[(self.page - 1) % n]), ps.get(self.PAGES[(self.page - 1) % n], 0)),
                (240, "●", self.action_label(), None),
                (400, "▼", short(self.PAGES[(self.page + 1) % n]), ps.get(self.PAGES[(self.page + 1) % n], 0))]):
            if sev is None:
                sev = m["overall"] if m else 0
                col = SEV_COL[sev] if sev else MUTED
                self.pg.draw.circle(s, col, (cx, y - 14), 9, 3)
                self.T(label, 15, TEXT, (cx, y + 10), "midtop", surf=s, maxw=RAIL_W - 8)
                self.T("hold: sleep", 11, DIM, (cx, y + 32), "midtop", surf=s)
            else:
                col = PAGE_COLORS.get(self.PAGES[self.page], (MUTED, None))[0]   # matches the arrow LEDs
                self.T(arrow, 26, col, (cx, y - 24), "midtop", surf=s)
                self.T(label, 15, TEXT, (cx, y + 10), "midtop", surf=s, maxw=RAIL_W - 8)
                if zone == 0:
                    self.T("hold: home", 11, DIM, (cx, y + 32), "midtop", surf=s)
                if zone == 2:
                    self.T("hold: help", 11, DIM, (cx, y + 32), "midtop", surf=s)

    # -- pages
    def tile(self, rect, label, value, sub, sev, vcol=None, stripe=None):
        r = self.pg.Rect(rect)
        self.card(r)
        self.pg.draw.rect(self.canvas, stripe or (SEV_COL[sev] if sev is not None else DIM), (r.x, r.y + 10, 4, r.h - 20),
                          border_radius=2)
        self.T(label, 13, MUTED, (r.x + 14, r.y + 10), maxw=r.w - 20)
        self.T(value, 28, vcol or TEXT, (r.x + 14, r.y + 28), bold=True, maxw=r.w - 20)
        self.T(sub, 13, MUTED, (r.x + 14, r.y + r.h - 22), maxw=r.w - 20)

    def start_ups_test(self):
        self.ups_test = dict(start=time.time(), refreshed=0.0)
        self.send("ups-test", "UPS self-test", "script", "turn_on", {"entity_id": self.model["ups_test"]})

    def track_ups_test(self):
        """Poll HA for a fresh result while a self-test runs; report it the moment it changes."""
        t = self.ups_test
        if not t or not self.model:
            return
        now = time.time()
        ups = self.model["ups"]
        res = ups.get("selftest") or ""
        lc = ups.get("selftest_lc") or 0
        if lc > t["start"] + 1 and "progress" not in res.lower():
            ok = res.lower().startswith("ok") or "passed" in res.lower()
            self.notify(f"UPS self-test: {res}", OK if ok else CRIT, 8)
            self.ups_test = None
        elif now - t["start"] > 60:
            # NUT never flagged a change; the UPS finished long ago, so report the standing result
            ok = res.lower().startswith("ok") or "passed" in res.lower()
            self.notify(f"UPS self-test: {res or 'no result'} (unchanged)", OK if ok else WARN, 8)
            self.ups_test = None
        elif now - t["refreshed"] >= 5 and now - t["start"] > 3:
            t["refreshed"] = now
            self.data.call("homeassistant", "update_entity",
                           {"entity_id": [f"sensor.{UPS_PREFIX}_self_test_result", f"sensor.{UPS_PREFIX}_status",
                                          f"sensor.{UPS_PREFIX}_battery"]})

    # -- screens.json
    _factory = None

    def default_screens(self):
        """The factory layouts shipped with the code (layouts/default_screens.json): every page as data."""
        if UI._factory is None:
            with open(FACTORY_FILE, encoding="utf-8") as f:
                UI._factory = json.load(f)
        return json.loads(json.dumps(UI._factory))

    def factory_screen(self, sid):
        return next((x for x in self.default_screens()["screens"] if x["id"] == sid), None)

    def add_new_factory(self, cfg_):
        """Add factory screens that appeared in a code update (once; deleting one later is respected)."""
        seen = set(cfg_.get("factory_seen") or [])
        have = {x.get("id") for x in cfg_.get("screens", [])}
        changed = False
        if not seen:                              # first run of this feature: everything present counts as seen
            seen = set(have) | {"overview", "power", "servers", "network", "ports", "time", "hotspot", "ha",
                                "cooling", "lights"}
            changed = True
        for x in self.default_screens()["screens"]:
            if x["id"] not in seen:
                seen.add(x["id"])
                changed = True
                if x["id"] not in have:
                    cfg_.setdefault("screens", []).append(x)
                    print("screens: added new factory screen", x["id"], flush=True)
        cfg_["factory_seen"] = sorted(seen)
        return changed

    def migrate_builtin(self, cfg_):
        """Replace old one-line "builtin" screens with their full factory layout. Returns True if changed."""
        changed = False
        for x in cfg_.get("screens", []):
            root = x.get("root") or {}
            if root.get("type") == "builtin":
                f = self.factory_screen((root.get("props") or {}).get("page") or x.get("id"))
                if f:
                    x["root"], x["details"] = f["root"], (x.get("details") or []) + f.get("details", [])
                    if x.get("middle") in (None, "builtin"):
                        x["middle"] = f.get("middle", "home")
                    for k in ("middle_label",):
                        if f.get(k):
                            x.setdefault(k, f[k])
                    changed = True
        return changed

    def load_screens(self):
        try:
            with open(SCREENS_FILE, encoding="utf-8") as f:
                cfg_ = json.load(f)
        except FileNotFoundError:
            cfg_ = self.default_screens()
            try:
                os.makedirs(os.path.dirname(SCREENS_FILE), exist_ok=True)
                with open(SCREENS_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg_, f, indent=1)
            except OSError as e:
                print("screens: cannot write default:", e, flush=True)
        except ValueError as e:
            print("screens: invalid JSON, using the factory layouts:", e, flush=True)
            cfg_ = self.default_screens()
        changed = self.migrate_builtin(cfg_)
        changed = self.add_new_factory(cfg_) or changed
        if changed:
            try:
                os.replace(SCREENS_FILE, SCREENS_FILE + ".pre-layouts")
                with open(SCREENS_FILE, "w", encoding="utf-8") as f:
                    json.dump(cfg_, f, indent=1)
                print("screens: updated to the current factory layouts (saved the old file as .pre-layouts)", flush=True)
            except OSError as e:
                print("screens: cannot save converted layouts:", e, flush=True)
        self.apply_screens(cfg_)

    def apply_screens(self, cfg_):
        cur = self.PAGES[self.page] if getattr(self, "PAGES", None) and self.page < len(self.PAGES) else None
        screens = [x for x in cfg_.get("screens", []) if x.get("enabled", True) and x.get("root")]
        if not screens:
            cfg_ = self.default_screens()
            screens = cfg_["screens"]
        self.screens_cfg = cfg_
        self.all_screens = screens
        self.PAGES = [x["id"] for x in screens if self.screen_visible(x)]
        self.TITLES = dict(UI.TITLES, **{x["id"]: x.get("title") or x["id"] for x in screens})
        self.custom = {}
        for x in screens:
            self.custom[x["id"]] = x
            col = x.get("color")
            if isinstance(col, str) and len(col) == 7 and col.startswith("#"):
                rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
                PAGE_COLORS[x["id"]] = (rgb, tuple(int(c * 0.55) for c in rgb))
        self.page = self.PAGES.index(cur) if cur in self.PAGES else min(self.page, len(self.PAGES) - 1)
        self.cdetail = None
        refs = eng.ref_strings(cfg_)
        self.data.extra_re = eng.entity_regex(refs)
        self.data.rules = cfg_.get("alerts") or []
        self.data.alert_label = cfg_.get("alertLabel") or ""
        self.refresh_watch()
        self.data.refresh.set()

    def screen_visible(self, x):
        when = x.get("show_when")
        return not when or (when == "outage" and self.outage) or (when == "maintenance" and self.maint)

    def refresh_pages(self):
        cur = self.PAGES[self.page] if self.PAGES else None
        self.PAGES = [x["id"] for x in self.all_screens if self.screen_visible(x)]
        self.page = self.PAGES.index(cur) if cur in self.PAGES else 0

    def refresh_watch(self):
        """Concrete entities (full attributes) and templates the custom screens need, expanded per item."""
        watch, tpls, sparks = set(), set(), set()

        def visit(n, item):
            if not isinstance(n, dict):
                return
            p = n.get("props") or {}
            vals = [p.get(k) for k in ("entity", "target", "value", "max", "min", "ref", "value_ref")]
            vals += list(p.get("entities") or [])
            vals += [(p.get("show_if") or {}).get("ref")] + [c.get("ref") for c in p.get("colors") or []]
            vals += [p.get(k) for k in ("label", "sub", "title", "text")]
            for v in vals:
                if not isinstance(v, str) or not v:
                    continue
                v2 = eng.templ(v, item)
                if "{{" in v2:
                    tpls.add(v2)
                    continue
                e = eng.entity_of(v2)
                if e and "*" not in e and "{" not in e:
                    watch.add(e)
            if n.get("type") == "spark" and p.get("entity"):
                sparks.add(eng.templ(p["entity"], item))
            if n.get("type") in ("repeater", "listdetail", "chips") and p.get("source"):
                its = self.engine.items(p, item) if self.model else []
                for it in its or [None]:
                    for c in n.get("children") or []:
                        visit(c, it)
                    if n.get("type") == "chips":
                        visit({"props": {"value_ref": p.get("value_ref"), "label": p.get("label")}}, it)
                return
            for c in n.get("children") or []:
                visit(c, item)
        for _, root in eng.screen_roots(self.screens_cfg):
            visit(root, None)
        draft = getattr(self, "draft", None)
        if draft and time.time() - draft[1] < 120:          # a screen being edited in the builder
            for _, root in eng.screen_roots({"screens": [draft[0]]}):
                visit(root, None)
        for rule in self.screens_cfg.get("alerts") or []:
            r_ = rule.get("ref") or rule.get("entity") or ""
            if "{{" in r_:
                tpls.add(r_)
            elif eng.entity_of(r_):
                watch.add(eng.entity_of(r_))
        self.data.watch = watch
        self.data.user_tpls = sorted(tpls)
        if draft and time.time() - draft[1] < 120:
            self.data.extra_re = eng.entity_regex(eng.ref_strings(self.screens_cfg) + eng.ref_strings({"screens": [draft[0]]}))
        self.spark_refs = sparks

    def detail_screen(self, page, did):
        x = self.custom.get(page) or {}
        return next((d for d in x.get("details") or [] if d.get("id") == did), None)

    def open_detail(self, did, item=None):
        page = self.PAGES[self.page]
        if self.detail_screen(page, did):
            self.cdetail = (did, item)

    def close_detail(self):
        self.cdetail = None

    def draw_custom(self, page):
        x = self.custom[page]
        root, item = x["root"], None
        if self.cdetail:
            d = self.detail_screen(page, self.cdetail[0])
            if d:
                root, item = d.get("root") or {}, self.cdetail[1]
        self.engine.draw(root, self.pg.Rect(12, 50, CW - 24, H - 60), item)

    def custom_action(self, page, dry=False):
        x = self.custom[page]
        mode = x.get("middle") or "home"
        if mode == "builtin":
            mode = (self.factory_screen(x["id"]) or {}).get("middle") or "home"
        if mode == "details":
            targets = self.engine.detail_targets(x["root"])
            if not targets:
                return "Overview" if dry else self.goto(self.PAGES[0])
            cur = self.cdetail
            idx = next((i for i, t in enumerate(targets) if cur and t[0] == cur[0] and t[1] == cur[1]), -1)
            nxt = targets[idx + 1] if idx + 1 < len(targets) else None
            if dry:
                if not nxt:
                    return "Back"
                d = self.detail_screen(page, nxt[0]) or {}
                return (nxt[1].get("name") if isinstance(nxt[1], dict) else d.get("title", "Details")) + " ›"
            self.cdetail = nxt
            return
        if self.cdetail:
            return "Back" if dry else self.close_detail()
        if mode == "silence":
            if dry:
                return "Silence" if self._unacked() else "Sleep"
            if self._unacked():
                self.acked |= {a.key for a in self.model["alerts"]}
            else:
                self._set_screen(False)
            return
        if mode == "reping":
            return "Re-ping" if dry else self.data.ping_now.set()
        if mode == "wled_power":
            if dry:
                return "LEDs on/off"
            st = self.wled.state
            return self.wled.send({"on": not (st and st.get("on"))})
        if mode == "hunt":
            if dry:
                return "Stop blink" if self.hunt_active() else ("Blink port" if self.hunt_sel else "Overview")
            if self.hunt_active():
                return self.hunt_stop()
            if self.hunt_sel:
                return self.hunt_start()
            return setattr(self, "page", 0)
        if mode == "next":
            if dry:
                rep_ = self.engine.first_repeater(x["root"])
                many = rep_ and len(self.engine.items(rep_.get("props") or {})) > 1
                return (x.get("middle_label") or "Next item") if many else "Overview"
            if not self.engine.next_item(x["root"]):
                self.page = 0
            return
        if mode == "back":
            return "Back" if dry else self.close_detail()
        return "Overview" if dry else setattr(self, "page", 0)

    # -- shared widgets used by the engine
    def alert_banner(self, r):
        m = self.model
        al, sev = m["alerts"], m["overall"]
        self.card(r, self.tint(SEV_COL[sev], 0.22))
        crit = sum(a.sev == 2 for a in al)
        warn = sum(a.sev == 1 for a in al)
        if not al:
            self.T("All systems normal", 24 if r.h >= 50 else 18, OK, (r.x + 16, r.centery), "midleft", bold=True)
            return
        if al[0].detail and r.h >= 50:
            self.T(al[0].text, 19, SEV_COL[al[0].sev], (r.x + 16, r.y + 7), bold=True, maxw=r.w - 170)
            self.T(al[0].detail, 13, TEXT, (r.x + 16, r.y + 32), maxw=r.w - 170)
        else:
            self.T(al[0].text, 21 if r.h >= 50 else 16, SEV_COL[al[0].sev], (r.x + 16, r.centery), "midleft",
                   bold=True, maxw=r.w - 170)
        parts = ([f"{crit} critical"] if crit else []) + ([f"{warn} warning"] if warn else [])
        self.T(" · ".join(parts), 15, TEXT, (r.right - 14, r.centery), "midright")

    def alert_list(self, r, maxn=6):
        al = self.model["alerts"]
        self.card(r)
        if not al:
            self.T("No active alerts", 16, MUTED, (r.x + 16, r.y + 16))
            return
        rows = max(1, min(maxn, (r.h - 16) // 28))
        for i, a in enumerate(al[:rows]):
            yy = r.y + 12 + i * 28
            if i == rows - 1 and len(al) > rows:
                self.T(f"+{len(al) - rows + 1} more", 15, MUTED, (r.x + 34, yy + 2))
                break
            self.dot((r.x + 18, yy + 11), SEV_COL[a.sev], 6)
            tr = self.T(a.text, 16, TEXT if a.key not in self.acked else MUTED, (r.x + 34, yy + 1), maxw=r.w - 150)
            if a.detail and tr.right < r.right - 150:
                self.T("— " + a.detail, 13, MUTED, (tr.right + 8, yy + 3), maxw=r.right - 110 - tr.right)
            self.T(self.TITLES.get(a.page, ""), 13, DIM, (r.right - 14, yy + 3), "topright")

    def sample_history(self):
        now = time.time()
        for ref in getattr(self, "spark_refs", ()):
            v = self.engine.resolve(ref)
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            dq = self.hist.setdefault(ref, deque(maxlen=1500))
            if not dq or now - dq[-1][0] >= 4.5:
                dq.append((now, v))

    def recent(self, ref, minutes):          # short in-memory history for sparklines (self.history is the database)
        cut = time.time() - minutes * 60
        return [v for t_, v in self.hist.get(ref, ()) if t_ >= cut]

    # -- web builder requests (run on the main thread)
    def handle_web(self):
        while not self.web_q.empty():
            req = self.web_q.get()
            try:
                if req[0] == "reload":
                    self.load_screens()
                elif req[0] == "app":
                    self.app_command(req[1])
                elif req[0] == "show":
                    _, page, did = req[:3]
                    if page in self.PAGES:
                        self.page = self.PAGES.index(page)
                        self._led_page = page
                        self.cdetail = (did, None) if did else None
                        self.last_input = time.monotonic()
                        if not self.screen_on:
                            self._set_screen(True)
                elif req[0] == "preview":
                    _, page, did, box = req
                    box["png"] = self.render_png(page, did)
                    box["done"].set()
                elif req[0] == "draft":
                    _, screen, did, state, box = req
                    box["png"], box["rects"] = self.render_draft(screen, did, state)
                    box["done"].set()
            except Exception as e:
                print(time.strftime("%H:%M:%S"), "web request failed:", repr(e), flush=True)
                if req[0] == "preview":
                    req[3]["done"].set()

    def render_draft(self, screen, did=None, state=None):
        """Render an unsaved screen from the builder; returns (png bytes, [(node id, rect)])."""
        state = state or {}
        new_refs = self.draft is None or json.dumps(self.draft[0], sort_keys=True) != json.dumps(screen, sort_keys=True)
        self.draft = (screen, time.time())
        if new_refs:
            self.refresh_watch()
            self.data.refresh.set()
        sid = screen.get("id") or "__draft__"
        saved = (list(self.PAGES), dict(self.custom), dict(self.TITLES), self.page, self.cdetail, self.help, self.dialog,
                 self.toast, self.hits, self.drags, dict(self.engine.tab), dict(self.engine.item))
        try:
            if sid not in self.PAGES:
                self.PAGES.append(sid)
            self.custom[sid] = screen
            self.TITLES[sid] = screen.get("title") or sid
            col = screen.get("color")
            if isinstance(col, str) and len(col) == 7:
                rgb = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
                PAGE_COLORS[sid] = (rgb, tuple(int(c * 0.55) for c in rgb))
            self.page = self.PAGES.index(sid)
            item = None
            if did and state.get("detail_item") is not None:
                its = self.engine.items(state.get("detail_item_src") or {})
                k = int(state["detail_item"])
                item = its[k] if 0 <= k < len(its) else None
            if did and item is None:
                tg = [t for t in self.engine.detail_targets(screen.get("root") or {}) if t[0] == did]
                item = tg[0][1] if tg else None
            self.cdetail = (did, item) if did else None
            self.engine.tab.update({k: int(v) for k, v in (state.get("tab") or {}).items()})
            self.engine.item.update({k: int(v) for k, v in (state.get("item") or {}).items()})
            self.help, self.dialog, self.toast = False, None, None
            self.engine.record = []
            self._composing = True
            self.draw()
            rects = [(nid, r) for nid, r in self.engine.record]
            buf = io.BytesIO()
            self.pg.image.save(self.screen, buf, "preview.png")
            return buf.getvalue(), rects
        finally:
            self.engine.record = None
            self._composing = False
            (self.PAGES, self.custom, self.TITLES, self.page, self.cdetail, self.help, self.dialog, self.toast,
             self.hits, self.drags, tab, item_) = saved
            self.engine.tab.clear(); self.engine.tab.update(tab)
            self.engine.item.clear(); self.engine.item.update(item_)

    def render_png(self, page, did=None):
        if page not in self.PAGES or not self.model:
            return None
        saved = (self.page, self.cdetail, self.help, self.dialog, self.toast, self.hits, self.drags)
        try:
            self.page = self.PAGES.index(page)
            self.cdetail = (did, None) if did else None
            self.help, self.dialog, self.toast = False, None, None
            self._composing = True
            self.draw()
            buf = io.BytesIO()
            self.pg.image.save(self.screen, buf, "preview.png")
            return buf.getvalue()
        finally:
            self._composing = False
            self.page, self.cdetail, self.help, self.dialog, self.toast, self.hits, self.drags = saved

    def goto(self, page, item=None):
        """Show a screen; with an item index, also select that item in the screen's first repeater."""
        self.list_centered.clear()
        if page not in self.PAGES:
            return
        self.page = self.PAGES.index(page)
        self.cdetail = None
        if item is not None and page in self.custom:
            rep_ = self.engine.first_repeater(self.custom[page]["root"])
            if rep_:
                self.engine.item[rep_["id"]] = int(item)

    def draw_dialog(self):
        d = self.dialog
        c = self.canvas
        shade = self.pg.Surface((CW, H), self.pg.SRCALPHA)
        shade.fill((0, 0, 0, 170))
        c.blit(shade, (0, 0))
        self.hits = [((self.pg.Rect(0, 0, CW, H)), lambda: None)]   # only the dialog is live
        r = self.pg.Rect(0, 0, 520, 270)
        r.center = (CW // 2, H // 2)
        self.card(r, CARD2)
        col = CRIT if d["danger"] else INFO
        self.pg.draw.rect(c, col, r, 2, border_radius=10)
        self.T(d["title"], 22, TEXT, (r.x + 22, r.y + 20), bold=True, maxw=r.w - 44)
        for i, line in enumerate(d["body"]):
            self.T(line, 15, WARN if "ESSENTIAL" in line else MUTED, (r.x + 22, r.y + 62 + i * 22), maxw=r.w - 44)
        bw = (r.w - 44 - 12) // 2
        self.button((r.x + 22, r.bottom - 66, bw, 48), "Cancel", lambda: setattr(self, "dialog", None), col=MUTED)
        cr = self.pg.Rect(r.x + 22 + bw + 12, r.bottom - 66, bw, 48)
        if d["hold"]:
            prog = 0 if self.hold_start is None else min(1, (time.monotonic() - self.hold_start) / HOLD_CONFIRM)
            self.pg.draw.rect(c, self.tint(col, 0.3), cr, border_radius=8)
            if prog:
                f = cr.copy()
                f.w = max(16, int(cr.w * prog))
                self.pg.draw.rect(c, col, f, border_radius=8)
            self.pg.draw.rect(c, col, cr, 2, border_radius=8)
            self.T("Hold to confirm", 16, TEXT, cr.center, "center", bold=True)
            self.hit(cr.inflate(6, 6), lambda: setattr(self, "hold_start", time.monotonic()))
        else:
            self.button(cr, "Confirm", self.confirm, col=col, filled=True, size=16)

    def draw_help(self):
        c = self.canvas
        c.fill(BG)
        self.hits = [(self.pg.Rect(0, 0, CW, H), lambda: setattr(self, "help", False))]
        self.T("Help", 24, TEXT, (14, 9), bold=True)
        self.T("tap anywhere to close", 13, DIM, (CW - 14, 17), "topright")
        r = self.pg.Rect(12, 50, 318, H - 60)
        self.card(r)
        self.T("Colour code", 15, MUTED, (r.x + 14, r.y + 10))
        y = r.y + 38
        for col, name, desc in [(OK, "Green", "normal / healthy"), (WARN, "Amber", "warning, needs a look"),
                                (CRIT, "Red", "critical / down"), (INFO, "Blue", "info, or no data from HA"),
                                (DIM, "Grey", "off, unused or not fitted")]:
            self.dot((r.x + 22, y + 9), col, 7)
            self.T(name, 15, TEXT, (r.x + 38, y), bold=True)
            self.T(desc, 14, MUTED, (r.x + 110, y + 1))
            y += 28
        y += 8
        self.T("Button LEDs", 15, MUTED, (r.x + 14, y))
        y += 26
        for line in ["Middle: whole-rack health.", "  Pulsing red = new critical alert;",
                     "  press Silence on Overview to stop.", "  Blue = HA unreachable. Time page:",
                     "  ticks every GPS second.", "Arrows: this page's colour (bar under",
                     "  the title); Lights: the live strip."]:
            self.T(line, 13, TEXT, (r.x + 14, y))
            y += 19
        r = self.pg.Rect(338, 50, CW - 350, H - 60)
        self.card(r)
        self.T("Buttons", 15, MUTED, (r.x + 14, r.y + 10))
        y = r.y + 36
        for b, short, hold in [("Top ▲", "previous page", "Overview"), ("Middle ●", "page action", "screen off"),
                               ("Bottom ▼", "next page", "this help")]:
            self.T(b, 14, TEXT, (r.x + 14, y), bold=True)
            self.T(f"press: {short}", 13, MUTED, (r.x + 105, y + 1))
            self.T(f"hold: {hold}", 13, DIM, (r.x + 105, y + 18))
            y += 42
        self.T("Touch", 15, MUTED, (r.x + 14, y + 4))
        y += 30
        for line in ["Tap a tile or server to open it; the right", "strip works like the buttons. Risky actions",
                     "need a 1.5 s hold. Door closed = sleep."]:
            self.T(line, 13, TEXT, (r.x + 14, y))
            y += 19
        # screen builder: address + password reset (needs someone at the rack)
        import webui
        y += 8
        self.T("Screen builder", 15, MUTED, (r.x + 14, y))
        self.T(f"http://{local_ip()}:{WEB_PORT}", 13, INFO, (r.x + 14, y + 22), maxw=r.w - 28)
        self.T("password set" if webui.has_password() else "no password yet: first visitor sets it", 12, DIM,
               (r.x + 14, y + 40), maxw=r.w - 28)
        self.button((r.x + 12, r.bottom - 92, r.w - 24, 36), "End maintenance" if self.maint else "Start maintenance",
                    lambda: (setattr(self, "help", False), self.app_command("maint_toggle")), col=(150, 90, 255), size=14)
        self.button((r.x + 12, r.bottom - 48, r.w - 24, 36), "Reset builder password",
                    lambda: setattr(self, "help", False) or self.ask("Reset the builder password?",
                                     ["The next person to open the builder page", "chooses a new password."],
                                     lambda: (webui.reset_password(), self.notify("Builder password cleared", OK, 4)),
                                     hold=True, danger=True),
                    col=WARN, size=14, enabled=webui.has_password())

    # -- touch widgets
    def drag(self, rect, fn):
        self.drags.append((self.pg.Rect(rect), fn))

    def throttle(self, phase, every=0.08):
        now = time.monotonic()
        if phase == "up" or now - self._last_send >= every:
            self._last_send = now
            return True
        return False

    def lval(self, key, actual):
        v = self.local.get(key)
        return v[0] if v and time.monotonic() < v[1] else actual

    def slider(self, rect, key, value, vmax, on_set, col=INFO, label=None, fmt_v=None):
        r = self.pg.Rect(rect)
        v = self.lval(key, value) or 0
        track = self.pg.Rect(r.x + 10, r.centery - 5, r.w - 20, 10)
        self.pg.draw.rect(self.canvas, LINE, track, border_radius=5)
        f = track.copy()
        f.w = max(10, int(track.w * v / vmax))
        self.pg.draw.rect(self.canvas, col, f, border_radius=5)
        self.pg.draw.circle(self.canvas, TEXT, (track.x + int(track.w * v / vmax), track.centery), 12)
        self.pg.draw.circle(self.canvas, col, (track.x + int(track.w * v / vmax), track.centery), 8)
        if label:
            self.T(label, 13, MUTED, (r.x + 10, r.y - 2))
            self.T(fmt_v(v) if fmt_v else str(int(v)), 13, TEXT, (r.right - 10, r.y - 2), "topright")

        def fn(x, y, phase):
            nv = int(max(0, min(1, (x - track.x) / track.w)) * vmax)
            self.local[key] = (nv, time.monotonic() + 1.5)
            if self.throttle(phase):
                on_set(nv)
        self.drag(r.inflate(0, 16), fn)

    def wheel_surface(self, rad):
        if self._wheel and self._wheel[0] == rad:
            return self._wheel[1]
        surf = self.pg.Surface((rad * 2, rad * 2), self.pg.SRCALPHA)
        for yy in range(rad * 2):
            for xx in range(rad * 2):
                dx, dy = xx - rad, yy - rad
                d = math.hypot(dx, dy)
                if d <= rad:
                    h = (math.atan2(-dy, dx) / (2 * math.pi)) % 1
                    r_, g_, b_ = colorsys.hsv_to_rgb(h, min(1, d / rad), 1)
                    surf.set_at((xx, yy), (int(r_ * 255), int(g_ * 255), int(b_ * 255), 255))
        self._wheel = (rad, surf)
        return surf

    def seg_ids(self, st):
        segs = [sg.get("id") for sg in (st or {}).get("seg", []) if sg.get("id") is not None]
        return segs if self.seg_sel == "all" else [self.seg_sel]

    def seg_send(self, st, **vals):
        self.wled.send({"seg": [dict(id=i, **vals) for i in self.seg_ids(st)]})

    def wled_panel(self, R):
        """Rack WLED controls (power, brightness, colour wheel, swatches, segments, effects/palettes/presets,
        speed, intensity) inside rect R. Special widget: talks to WLED directly, not through HA."""
        c = self.canvas
        st, _, _, connected = self.wled.snapshot()
        if not st:
            self.T("Connecting to WLED-Rack…" if not connected else "Waiting for WLED state…", 18, MUTED,
                   R.center, "center")
            return
        segs = st.get("seg", [])
        cur = next((sg for sg in segs if self.seg_sel == "all" or sg.get("id") == self.seg_sel), segs[0] if segs else {})
        on = st.get("on")

        # ---- left: power, brightness, colour wheel, swatches, segments
        r = self.pg.Rect(R.x, R.y, 268, R.h)
        self.card(r)
        pw = self.pg.Rect(r.x + 12, r.y + 12, 52, 52)
        self.pg.draw.circle(c, OK if on else LINE, pw.center, 26)
        self.pg.draw.circle(c, BG if on else MUTED, pw.center, 13, 3)
        self.pg.draw.line(c, BG if on else MUTED, (pw.centerx, pw.centery - 16), (pw.centerx, pw.centery - 4), 3)
        self.hit(pw.inflate(8, 8), lambda: self.wled.send({"on": not on}))
        self.slider((r.x + 70, r.y + 26, r.w - 80, 40), "bri", st.get("bri", 0), 255,
                    lambda v: self.wled.send({"on": True, "bri": max(1, v)}),
                    label="Brightness", fmt_v=lambda v: f"{round(v * 100 / 255)}%")

        rad = 92
        wc = (r.centerx, r.y + 80 + rad)
        c.blit(self.wheel_surface(rad), (wc[0] - rad, wc[1] - rad))
        col0 = tuple((cur.get("col") or [[255, 255, 255]])[0][:3])
        col0 = self.lval("col", col0)
        h_, s_, v_ = colorsys.rgb_to_hsv(*[x / 255 for x in col0])
        mx = wc[0] + int(math.cos(h_ * 2 * math.pi) * s_ * rad)
        my = wc[1] - int(math.sin(h_ * 2 * math.pi) * s_ * rad)
        self.pg.draw.circle(c, BG, (mx, my), 11)
        self.pg.draw.circle(c, TEXT, (mx, my), 9, 3)

        def wheel_fn(x, y, phase):
            dx, dy = x - wc[0], y - wc[1]
            hh = (math.atan2(-dy, dx) / (2 * math.pi)) % 1
            ss = min(1, math.hypot(dx, dy) / rad)
            rgb = tuple(int(v * 255) for v in colorsys.hsv_to_rgb(hh, ss, 1))
            self.local["col"] = (rgb, time.monotonic() + 1.5)
            if self.throttle(phase):
                self.seg_send(st, col=[list(rgb)])
        self.drag((wc[0] - rad - 6, wc[1] - rad - 6, rad * 2 + 12, rad * 2 + 12), wheel_fn)

        y = wc[1] + rad + 14
        sw = (r.w - 24 - 6 * 5) // 6
        for i, rgb in enumerate(SWATCHES):
            cr = self.pg.Rect(r.x + 12 + i * (sw + 6), y, sw, 30)
            self.pg.draw.rect(c, rgb, cr, border_radius=7)
            if col0 == rgb:
                self.pg.draw.rect(c, TEXT, cr.inflate(6, 6), 2, border_radius=9)
            self.hit(cr.inflate(4, 6), lambda rgb=rgb: self.seg_send(st, col=[list(rgb)]))
        y += 42
        self.T("Segment", 12, MUTED, (r.x + 12, y + 8))
        opts = ["all"] + [sg.get("id") for sg in segs]
        bw = (r.w - 90 - 6 * (len(opts) - 1)) // len(opts)
        for i, o in enumerate(opts):
            lab = "All" if o == "all" else f"{o + 1}"
            self.button((r.x + 78 + i * (bw + 6), y, bw, 32), lab, lambda o=o: setattr(self, "seg_sel", o),
                        col=OK if self.seg_sel == o else MUTED, filled=self.seg_sel == o, size=13)

        # ---- right: effects / palettes / presets list + speed / intensity
        r = self.pg.Rect(R.x + 276, R.y, R.w - 276, R.h)
        self.card(r)
        for i, (key, label) in enumerate((("fx", "Effects"), ("pal", "Palettes"), ("ps", "Presets"))):
            tw = (r.w - 24 - 12) // 3
            self.button((r.x + 12 + i * (tw + 6), r.y + 10, tw, 32), label,
                        lambda k=key: (setattr(self, "list_tab", k), self.list_centered.discard(k)),
                        col=INFO if self.list_tab == key else MUTED, filled=self.list_tab == key, size=13)
        if self.list_tab == "fx":
            items = [(i, n) for i, n in enumerate(self.wled.effects) if n and n != "RSVD" and n != "-"]
            sel = cur.get("fx")
            pick = lambda i: self.seg_send(st, fx=i)
        elif self.list_tab == "pal":
            items = [(i, n) for i, n in enumerate(self.wled.palettes) if n and n != "-"]
            sel = cur.get("pal")
            pick = lambda i: self.seg_send(st, pal=i)
        else:
            items = sorted(self.wled.presets.items())
            sel = st.get("ps")
            pick = lambda i: self.wled.send({"ps": i})
        lr = self.pg.Rect(r.x + 8, r.y + 50, r.w - 16, r.h - 50 - 112)
        rowh = 36
        maxs = max(0, len(items) * rowh - lr.h)
        if self.list_tab not in self.list_centered and items:     # open lists on the active item
            k = next((n for n, (idx, _) in enumerate(items) if idx == sel), 0)
            self.list_scroll[self.list_tab] = k * rowh - lr.h // 2 + rowh // 2
            self.list_centered.add(self.list_tab)
        scroll = self.list_scroll[self.list_tab] = max(0, min(maxs, self.list_scroll[self.list_tab]))
        clip = c.get_clip()
        c.set_clip(lr)
        first = scroll // rowh
        for k in range(first, min(len(items), first + lr.h // rowh + 2)):
            idx, name = items[k]
            ry = lr.y + k * rowh - scroll
            row = self.pg.Rect(lr.x, ry, lr.w, rowh - 4)
            active = idx == sel
            self.pg.draw.rect(c, self.tint(OK, 0.35) if active else CARD2, row, border_radius=7)
            if active:
                self.pg.draw.rect(c, OK, row, 2, border_radius=7)
            self.T(name, 15, OK if active else TEXT, (row.x + 12, row.centery), "midleft", bold=active, maxw=row.w - 24)
        c.set_clip(clip)
        if maxs:
            bar_h = max(30, lr.h * lr.h // (len(items) * rowh))
            by = lr.y + (lr.h - bar_h) * scroll // maxs
            self.pg.draw.rect(c, LINE, (lr.right - 3, by, 3, bar_h), border_radius=2)
        tab = self.list_tab

        def list_fn(x, y, phase, items=items, pick=pick, tab=tab):
            if phase == "down":
                self._ld = (y, self.list_scroll[tab], False)
            elif phase == "move":
                y0, s0, moved = self._ld
                if abs(y - y0) > 8:
                    moved = True
                self._ld = (y0, s0, moved)
                if moved:
                    self.list_scroll[tab] = s0 - (y - y0)
            elif phase == "up":
                y0, s0, moved = self._ld
                if not moved:
                    k = (y - lr.y + self.list_scroll[tab]) // rowh
                    if 0 <= k < len(items):
                        pick(items[k][0])
        self.drag(lr, list_fn)

        y = r.bottom - 100
        self.slider((r.x + 6, y + 18, r.w - 12, 34), "sx", cur.get("sx", 128), 255,
                    lambda v: self.seg_send(st, sx=v), label="Speed")
        self.slider((r.x + 6, y + 66, r.w - 12, 34), "ix", cur.get("ix", 128), 255,
                    lambda v: self.seg_send(st, ix=v), label="Intensity")

    # -- Port Hunt
    def hunt_client(self):
        pm = self.model["portmap"] if self.model else None
        return next((c for c in (pm["clients"] if pm else []) if c.get("mac") == self.hunt_sel), None)

    def hunt_active(self):
        return time.monotonic() < self.hunt_until

    def hunt_start(self):
        c = self.hunt_client()
        if not c or not c.get("port"):
            return
        self.hunt_port = int(c["port"])
        self.hunt_until = time.monotonic() + PORT_HUNT_SECONDS
        self._hunt_last = 0.0
        if self.hunt_port > 48:
            self.notify(f"SFP+ {self.hunt_port}: no Etherlight on SFP cages (far right of the switch)",
                        PAGE_COLORS["ports"][0], 5)
        else:
            self.notify(f"Blinking port {self.hunt_port} on the 48-port switch", PAGE_COLORS["ports"][0], 3)

    def hunt_stop(self):
        self.hunt_until = 0.0
        if self.hunt_port and self.hunt_port <= 48:
            self.data.call("uiwled", "clear_port", {"switch": PORT_HUNT_SWITCH, "port": self.hunt_port}, quiet=True)

    def hunt_tick(self):
        """While active, flash the jack: a short timed override every 0.8 s (each one clears itself)."""
        if not self.hunt_active() or not self.hunt_port or self.hunt_port > 48:   # SFP+ cages have no Etherlight
            return
        now = time.monotonic()
        if now - self._hunt_last >= 0.8:
            self._hunt_last = now
            self.data.call("uiwled", "set_port", {"switch": PORT_HUNT_SWITCH, "port": self.hunt_port,
                                                  "rgb_color": PORT_HUNT_RGB, "duration_ms": 400}, quiet=True)


    def _hunt_view(self):
        pm = self.model["portmap"] if self.model else {"clients": [], "ports": [], "present": False}
        return pm, self.hunt_client(), self.hunt_active() and (time.monotonic() % 0.8) < 0.45, PAGE_COLORS["ports"][0]

    def hunt_list(self, r):
        """Port Hunt: scrolling list of wired clients; tapping one starts blinking its jack."""
        pm, _, _, accent = self._hunt_view()
        c = self.canvas
        clients = pm["clients"]
        self.card(r)
        self.T(f"{len(clients)} wired clients", 15, MUTED, (r.x + 14, r.y + 10))
        lr = self.pg.Rect(r.x + 6, r.y + 36, r.w - 12, r.h - 42)
        rowh = 44
        maxs = max(0, len(clients) * rowh - lr.h)
        self.hunt_scroll = max(0, min(maxs, self.hunt_scroll))
        clip = c.get_clip()
        c.set_clip(lr)
        first = self.hunt_scroll // rowh
        for k in range(first, min(len(clients), first + lr.h // rowh + 2)):
            cl = clients[k]
            ry = lr.y + k * rowh - self.hunt_scroll
            row = self.pg.Rect(lr.x, ry, lr.w, rowh - 4)
            active = cl.get("mac") == self.hunt_sel
            self.pg.draw.rect(c, self.tint(accent, 0.35) if active else CARD2, row, border_radius=7)
            if active:
                self.pg.draw.rect(c, accent, row, 2, border_radius=7)
            self.T(str(cl.get("name") or "?"), 14, accent if active else TEXT, (row.x + 10, row.y + 4), bold=True,
                   maxw=row.w - 64)
            self.T(cl.get("ip") or cl.get("mac") or "", 11, MUTED, (row.x + 10, row.y + 22), maxw=row.w - 64)
            self.T(f"P{cl.get('port')}", 15, accent if active else TEXT, (row.right - 10, row.centery), "midright",
                   bold=True)
        c.set_clip(clip)
        if maxs:
            bar_h = max(30, lr.h * lr.h // (len(clients) * rowh))
            by = lr.y + (lr.h - bar_h) * self.hunt_scroll // maxs
            self.pg.draw.rect(c, LINE, (lr.right - 3, by, 3, bar_h), border_radius=2)

        def list_fn(x, y, phase, clients=clients):
            if phase == "down":
                self._hd = (y, self.hunt_scroll, False)
            elif phase == "move":
                y0, s0, moved = self._hd
                moved = moved or abs(y - y0) > 8
                self._hd = (y0, s0, moved)
                if moved:
                    self.hunt_scroll = s0 - (y - y0)
            elif phase == "up":
                y0, s0, moved = self._hd
                if not moved:
                    k = (y - lr.y + self.hunt_scroll) // rowh
                    if 0 <= k < len(clients):
                        if self.hunt_active():
                            self.hunt_stop()
                        self.hunt_sel = clients[k].get("mac")
                        self.hunt_start()                  # selecting a client starts the hunt
        self.drag(lr, list_fn)

    def hunt_faceplate(self, r):
        """Port Hunt: the 48-port switch faceplate (odd ports on top, like the real jacks) with the jack lit."""
        pm, sel, blink_on, accent = self._hunt_view()
        c = self.canvas
        self.card(r)
        self.T(pm.get("switch") or "48-port switch", 15, MUTED, (r.x + 14, r.y + 10))
        ports = {p.get("port"): p for p in pm["ports"]}
        up = sum(1 for p in pm["ports"] if p.get("up"))
        self.T(f"{up} up", 13, DIM, (r.right - 14, r.y + 12), "topright")
        cols, gap, jw = 24, 2, 12
        groupgap = 5
        sfp_w = 2 * (jw + 6) + 10                       # two columns of SFP+ cages (49-52)
        total = cols * jw + (cols - 1) * gap + 3 * groupgap + sfp_w
        fx = r.x + (r.w - total) // 2
        fy = r.y + 46
        for col in range(cols):
            gx = fx + col * (jw + gap) + (col // 6) * groupgap
            for row_i in range(2):
                port = col * 2 + 1 + row_i
                pr = self.pg.Rect(gx, fy + row_i * (jw + 10), jw, jw)
                pinfo = ports.get(port)
                if sel and int(sel.get("port") or 0) == port:
                    self.pg.draw.rect(c, TEXT if blink_on else accent, pr.inflate(4, 4), border_radius=3)
                elif pinfo and pinfo.get("up"):
                    self.pg.draw.rect(c, self.tint(OK, 0.55), pr, border_radius=2)
                else:
                    self.pg.draw.rect(c, LINE, pr, 1, border_radius=2)
        sx0 = fx + cols * (jw + gap) + 3 * groupgap + 8
        for k, port in enumerate((49, 50, 51, 52)):
            pr = self.pg.Rect(sx0 + (k // 2) * (jw + 8), fy + (k % 2) * (jw + 10), jw + 4, jw)
            pinfo = ports.get(port)
            if sel and int(sel.get("port") or 0) == port:
                self.pg.draw.rect(c, TEXT if blink_on else accent, pr.inflate(4, 4), border_radius=3)
            elif pinfo and pinfo.get("up"):
                self.pg.draw.rect(c, self.tint(INFO, 0.6), pr, border_radius=2)
            else:
                self.pg.draw.rect(c, LINE, pr, 1, border_radius=2)
        self.T("SFP+", 10, DIM, (sx0 + jw + 6, fy + 2 * (jw + 10) - 4), "midtop")
        if sel:
            port = int(sel.get("port") or 0)
            if port > 48:
                gx = sx0 + ((port - 49) // 2) * (jw + 8) + jw // 2
                label = f"SFP+ {port}"
            else:
                col = (port - 1) // 2
                gx = fx + col * (jw + gap) + (col // 6) * groupgap + jw // 2
                label = f"Port {port}"
            gx = max(r.x + 40, min(r.right - 40, gx))
            self.T(label, 13, accent, (gx, fy + 2 * (jw + 10) + 8), "midtop", bold=True)
        else:
            self.T("Pick a client to find its port", 13, DIM, (r.centerx, fy + 2 * (jw + 10) + 6), "midtop")

    def hunt_client_card(self, r):
        """Port Hunt: the selected client's details and the blink / stop button."""
        pm, sel, _, accent = self._hunt_view()
        self.card(r)
        if not sel:
            self.T("Tap a client on the left.", 16, MUTED, (r.x + 16, r.y + 16))
            self.T("Its jack lights up here and blinks", 14, DIM, (r.x + 16, r.y + 46))
            self.T("on the real switch.", 14, DIM, (r.x + 16, r.y + 66))
            return
        ports = {p.get("port"): p for p in pm["ports"]}
        w = r.w - 28
        self.T(str(sel.get("name") or "?"), 22, TEXT, (r.x + 14, r.y + 10), bold=True, maxw=w)
        y = r.y + 44
        pinfo = ports.get(int(sel.get("port") or 0)) or {}
        y = self.kv(r.x + 14, y, w, "IP · MAC", f"{sel.get('ip') or '—'} · {sel.get('mac') or ''}", size=14)
        sp = sel.get("speed") or pinfo.get("speed")
        poe = pinfo.get("poe_w")
        y = self.kv(r.x + 14, y, w, "Port", f"{sel.get('port')} · {pinfo.get('name') or ''}".strip(" ·"), accent, size=14)
        link = f"{sp} Mbps" if sp else "—"
        y = self.kv(r.x + 14, y, w, "Link · PoE", f"{link} · {poe} W" if poe else link, size=14)
        self.kv(r.x + 14, y, w, "Network", f"{sel.get('network') or '—'}" +
                (f" (VLAN {sel['vlan']})" if sel.get("vlan") else ""), size=14)
        if self.hunt_active():
            left = int(self.hunt_until - time.monotonic())
            self.button((r.x + 12, r.bottom - 50, w + 4, 40), f"STOP BLINKING · {left}s", self.hunt_stop,
                        col=accent, filled=True, size=15)
        else:
            self.button((r.x + 12, r.bottom - 50, w + 4, 40), f"BLINK PORT {sel.get('port')}", self.hunt_start,
                        col=accent, size=15)

    def w_ups_test(self, r):
        """Run-UPS-self-test button; the result is reported as soon as NUT has it."""
        ups = self.model["ups"]
        test = self.ups_test
        label = f"Testing… {time.time() - test['start']:.0f} s" if test else "Run UPS self-test"
        self.button(r, label,
                    lambda: self.ask("Run UPS self-test?",
                                     ["The UPS switches to battery for a short test,",
                                      "then reports the result. Load stays powered."],
                                     self.start_ups_test),
                    enabled=ups["present"] and not test)

    # -- touch input (the touchscreen, and the live view in a browser with remote=True)
    def touch_down(self, x, y, remote=False):
        self.last_input = time.monotonic()
        if not self.screen_on and not remote:    # first touch only wakes the screen; the live view works asleep
            self._set_screen(True)
            return
        on_rail = x >= W - RAIL_W if RAIL_SIDE == "right" else x < RAIL_W
        if on_rail:
            self.press("short", min(2, y * 3 // H), remote)
            return
        cx = x - (0 if RAIL_SIDE == "right" else RAIL_W)
        if not (self.dialog or self.help):
            hit_drag = next((fn for rect, fn in reversed(self.drags) if rect.collidepoint(cx, y)), None)
            if hit_drag:
                self.active_drag = (hit_drag, x - cx)
                hit_drag(cx, y, "down")
                return
        for rect, fn in reversed(self.hits):   # topmost (last drawn) first
            if rect.collidepoint(cx, y):
                fn()
                break

    def touch_move(self, x, y):
        if self.active_drag:
            self.last_input = time.monotonic()
            fn, dx = self.active_drag
            fn(x - dx, y, "move")

    def touch_up(self, x, y):
        self.hold_start = None
        if self.active_drag:
            fn, dx = self.active_drag
            self.active_drag = None
            fn(x - dx, y, "up")

    # -- live view: the browser page streams these JPEGs while someone watches
    def mirror_active(self, now=None):
        return (now or time.monotonic()) - self.mirror["seen"] < 4

    def mirror_capture(self, now):
        mi = self.mirror
        if now - mi["at"] < 1 / MIRROR_FPS:
            return
        mi["at"] = now
        crc = zlib.crc32(self.pg.image.tostring(self.screen, "RGB"))
        if crc == mi["crc"]:                     # unchanged screen: nothing to send
            return
        buf = io.BytesIO()
        self.pg.image.save(self.screen, buf, "frame.jpg")
        with self.mirror_cv:
            mi.update(crc=crc, frame=buf.getvalue(), seq=mi["seq"] + 1)
            self.mirror_cv.notify_all()

    # -- main loop
    def run(self):
        pg = self.pg
        clock = pg.time.Clock()
        keymap = {pg.K_UP: 0, pg.K_RETURN: 1, pg.K_SPACE: 1, pg.K_DOWN: 2}
        last_model = 0.0
        while True:
            for ev in pg.event.get():
                if ev.type == pg.QUIT:
                    return
                if ev.type == pg.KEYDOWN:
                    if ev.key == pg.K_ESCAPE:
                        return
                    if ev.key in keymap:
                        self.press("long" if ev.mod & pg.KMOD_SHIFT else "short", keymap[ev.key])
                elif ev.type == pg.MOUSEBUTTONUP:
                    self.touch_up(*ev.pos)
                elif ev.type == pg.MOUSEMOTION:
                    self.touch_move(*ev.pos)
                elif ev.type == pg.MOUSEBUTTONDOWN:
                    self.touch_down(*ev.pos)
            while not self.remote_q.empty():
                kind, x, y = self.remote_q.get()
                if kind == "down":
                    self.touch_down(x, y, remote=True)
                elif kind == "move":
                    self.touch_move(x, y)
                else:
                    self.touch_up(x, y)
            while not self.events.empty():
                kind, btn, *remote = self.events.get()
                self.press(kind, btn, bool(remote))

            now = time.monotonic()
            if now - last_model >= 1.0:
                last_model = now
                m_ = build_model(self.data)
                self.app_model(m_)
                self.model = m_
                self.refresh_watch()
                self.sample_history()
                crit = {a.key for a in self.model["alerts"] if a.sev == 2}
                new = crit - self.seen_crit - self.acked
                self.seen_crit = crit
                self.acked &= {a.key for a in self.model["alerts"]}   # forget cleared alerts
                if new and self.model["ha_age"] is not None and not self.maint:
                    self.page = 0
                    self.last_input = now
                    if not self.screen_on:
                        self._set_screen(True)
                idle = now - self.last_input
                limit = DOOR_CLOSED_IDLE if self.door == "closed" else SCREEN_OFF_AFTER
                if self.screen_on and idle > limit and (self.door == "closed" or not self._unacked()):
                    self._set_screen(False)
                if idle > HOME_AFTER and self.page != 0:
                    self.page = 0
            self.handle_web()
            self.check_door(now)
            self.track_ups_test()
            self.hunt_tick()
            # LED stream only when the buttons show the strip: asleep (mirror) or on the Lights page
            self.wled.want_live = not self.screen_on or self.PAGES[self.page] == "lights"
            if self.sleep_at and now >= self.sleep_at:
                self.sleep_at = None
                # only if nobody has touched it since; otherwise the idle rule takes over
                if (self.door == "closed" and self.screen_on and not self.dialog
                        and now - self.last_input >= DOOR_CLOSED_IDLE):
                    self._set_screen(False)
            if self.dialog:
                if self.hold_start and now - self.hold_start >= HOLD_CONFIRM:
                    self.confirm()
                elif now > self.dialog["until"]:
                    self.dialog = None
            self.update_leds()
            if self.screen_on or self.mirror_active(now):
                self.draw()
                if self.mirror_active(now):
                    self.mirror_capture(now)
            clock.tick(25 if self.dialog or self.hold_start or self.active_drag or self.hunt_active() else 10)


if __name__ == "__main__":
    UI().run()
