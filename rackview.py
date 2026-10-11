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
        self.wpsd = None
        self.portmap = None
        self.ha_meta = {}
        self.ha_latency = None
        self.time = dict(server=None, fails=0, history=deque(maxlen=90), pi={})
        for target in (self._ha_loop, self._uiwled_loop, self._ping_loop, self._time_loop):
            threading.Thread(target=target, daemon=True).start()

    def _ha_loop(self):
        tpl = ("{\"e\": [{% for s in states if s.entity_id is search('" + ENTITY_RE.replace("\\", "\\\\") + "') %}"
               "[{{ s.entity_id|tojson }},{{ s.state|tojson }},"
               "{{ s.attributes.unit_of_measurement|default('')|tojson }},"
               "{% if s.domain in ['light', 'select'] %}{{ {'b': s.attributes.brightness|default(none), "
               "'rgb': s.attributes.rgb_color|default(none), 'fx': s.attributes.effect|default(none), "
               "'opts': s.attributes.options|default(none)}|tojson }}"
               "{% elif s.entity_id == 'sensor." + UPS_PREFIX + "_self_test_result' %}"
               "{{ {'lc': as_timestamp(s.last_changed)}|tojson }}{% else %}null{% endif %}]"
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
               "} | tojson }}}")
        while True:
            try:
                r = self.http.post(HA_URL + "/api/template", json={"template": tpl}, timeout=8)
                r.raise_for_status()
                j = r.json()
                ents = {e: {"s": st, "u": u, "a": a or {}} for e, st, u, a in j["e"]}
                with self.lock:
                    self.ents, self.s250, self.ha_ok_at, self.ha_err = ents, j.get("s250"), time.time(), ""
                    self.wpsd, self.ha_meta, self.ha_latency = j.get("wpsd"), j.get("ha") or {}, r.elapsed.total_seconds()
                    self.portmap = j.get("portmap")
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
            t = dict(self.time, history=list(self.time["history"]), s250=self.s250, wpsd=self.wpsd, portmap=self.portmap,
                     ha_meta=dict(self.ha_meta), ha_latency=self.ha_latency)
            return dict(self.ents), self.ha_ok_at, self.ha_err, list(self.switches), dict(self.pings), t


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

    m = {"alerts": [], "ha_age": time.time() - ha_ok_at if ha_ok_at else None, "ha_err": ha_err}
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
        fc = dict(name=d.replace("fancontrol", "Fan ctrl "), offline=offline, esp=nv(pre + "esp32_temp"),
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

    A.sort(key=lambda a: -a.sev)
    m["page_sev"] = {}
    for a in A:
        m["page_sev"][a.page] = max(m["page_sev"].get(a.page, 0), a.sev)
    m["overall"] = max((a.sev for a in A), default=0)
    m["page_sev"]["overview"] = m["overall"]
    m["stale"] = m["ha_age"] is None or m["ha_age"] > 30
    return m


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
    def press(self, kind, btn):
        self.last_input = time.monotonic()
        if not self.screen_on:
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
        if p == "overview":
            return "Silence" if self._unacked() else "Sleep"
        if p == "servers":
            return "Next server" if m and len(m["servers"]) > 1 else "Overview"
        if p == "network":
            return "Re-ping"
        if p == "lights":
            return "LEDs on/off"
        if p == "ports":
            return "Stop blink" if self.hunt_active() else ("Blink port" if self.hunt_sel else "Overview")
        if p == "cooling":
            nxt = self._next_fan_detail()
            return f"{nxt} ›" if nxt else "Back"
        return "Overview"

    def action(self):
        p = self.PAGES[self.page]
        m = self.model
        if p == "overview":
            if self._unacked():
                self.acked |= {a.key for a in m["alerts"]}
            else:
                self._set_screen(False)
        elif p == "servers" and m and len(m["servers"]) > 1:
            self.srv_idx = (self.srv_idx + 1) % len(m["servers"])
        elif p == "network":
            self.data.ping_now.set()
        elif p == "cooling":
            self.fan_detail = self._next_fan_detail()      # cycles controllers, then back to the overview
        elif p == "ports":
            if self.hunt_active():
                self.hunt_stop()
            elif self.hunt_sel:
                self.hunt_start()
            else:
                self.page = 0
        elif p == "lights":
            st = self.wled.state
            self.wled.send({"on": not (st and st.get("on"))})
        else:
            self.page = 0

    def _unacked(self):
        return bool(self.model) and any(a.sev and a.key not in self.acked for a in self.model["alerts"])

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
        if page == "lights" and self.wled.zone_colours() is not None:
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
                self.last_input = now
                self.page = 0
                if not self.screen_on:
                    self._set_screen(True)
        elif ps == DOOR_CLOSED_PRESET:
            self.door = "closed"
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
        # header
        tr = self.T(self.TITLES[page], 24, TEXT, (14, 9), bold=True)
        self.pg.draw.rect(c, PAGE_COLORS.get(page, (MUTED, None))[0], (tr.x, tr.bottom + 1, tr.w, 3), border_radius=2)
        sub = ""
        if page == "servers" and m and m["servers"]:
            s = m["servers"][self.srv_idx % len(m["servers"])]
            sub = f"{s['name']}  {self.srv_idx % len(m['servers']) + 1}/{len(m['servers'])}"
        if sub:
            self.T(sub, 18, MUTED, (14 + self.F(24, True).size(self.TITLES[page])[0] + 14, 14))
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
            getattr(self, "page_" + page)(m)
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
    def tile(self, rect, label, value, sub, sev, vcol=None):
        r = self.pg.Rect(rect)
        self.card(r)
        self.pg.draw.rect(self.canvas, SEV_COL[sev] if sev is not None else DIM, (r.x, r.y + 10, 4, r.h - 20), border_radius=2)
        self.T(label, 13, MUTED, (r.x + 14, r.y + 10), maxw=r.w - 20)
        self.T(value, 28, vcol or TEXT, (r.x + 14, r.y + 28), bold=True, maxw=r.w - 20)
        self.T(sub, 13, MUTED, (r.x + 14, r.y + r.h - 22), maxw=r.w - 20)

    def page_overview(self, m):
        x0, cw = 12, CW - 24
        al = m["alerts"]
        sev = m["overall"]
        r = self.pg.Rect(x0, 50, cw, 54)
        self.card(r, self.tint(SEV_COL[sev], 0.22))
        crit = sum(a.sev == 2 for a in al)
        warn = sum(a.sev == 1 for a in al)
        if not al:
            self.T("All systems normal", 24, OK, (r.x + 16, r.centery), "midleft", bold=True)
        else:
            if al[0].detail:
                self.T(al[0].text, 19, SEV_COL[al[0].sev], (r.x + 16, r.y + 7), bold=True, maxw=cw - 170)
                self.T(al[0].detail, 13, TEXT, (r.x + 16, r.y + 32), maxw=cw - 170)
            else:
                self.T(al[0].text, 21, SEV_COL[al[0].sev], (r.x + 16, r.centery), "midleft", bold=True, maxw=cw - 170)
            parts = ([f"{crit} critical"] if crit else []) + ([f"{warn} warning"] if warn else [])
            self.T(" · ".join(parts), 15, TEXT, (r.right - 14, r.centery), "midright")

        # tiles
        y, th = 112, 92
        tw = (cw - 3 * 8) // 4
        ups, pdu, fw = m["ups"], m["pdu"], m["fw"]
        self.tile((x0, y, tw, th), f"UPS · {ups['status'] or '?'}", fmt(ups["batt"], "%"),
                  f"{fmt(ups['load'], '% load')} · {fmt(ups['runtime'], ' min')}", ups["sev"])
        self.tile((x0 + (tw + 8), y, tw, th), "Rack power (PDU)", fmt(pdu["watts"], " W"),
                  f"of {fmt(pdu['budget'], ' W')} budget", pdu["sev"])
        inet = next((p for p in m["pings"] if p["label"] == "Internet"), None)
        self.tile((x0 + 2 * (tw + 8), y, tw, th), f"WAN · {fw['wan_gw'] or '?'}",
                  fmt(inet["ms"] if inet else None, " ms", none="DOWN"),
                  f"↓{fmt(fw['wan_in'], '', 1)} ↑{fmt(fw['wan_out'], '', 1)} {fw['rate_unit']}", m["page_sev"].get("network", 0))
        rpms = [f["rpm"] for fc in m["fanctl"] for f in fc["fans"] if f["fitted"] and f["rpm"]]
        temps = [s["temp"] for s in m["switches"] if s["temp"] is not None]
        self.tile((x0 + 3 * (tw + 8), y, tw, th), "Rack fans · avg rpm", fmt(sum(rpms) / len(rpms) if rpms else None),
                  f"switch max {fmt(max(temps) if temps else None, '°F')}", m["page_sev"].get("cooling", 0))
        for i, pg_name in enumerate(("power", "power", "network", "cooling")):
            self.hit((x0 + i * (tw + 8), y, tw, th), lambda p=pg_name: self.goto(p))

        # servers
        y = 212
        sv = m["servers"]
        if sv:
            n = len(sv)
            sw = (cw - (n - 1) * 8) // n
            for i, s in enumerate(sv):
                r = self.pg.Rect(x0 + i * (sw + 8), y, sw, 96)
                self.card(r)
                self.hit(r, lambda i=i: self.goto("servers", i))
                col = SEV_COL[s["sev"]] if s["sev"] else (OK if s["on"] else DIM)
                self.dot((r.x + 18, r.y + 22), col, 7)
                self.T(s["name"], 19, TEXT, (r.x + 32, r.y + 10), bold=True, maxw=sw - 40)
                state = ("ON" if s["on"] else "OFF") + (f" · {s['health']}" if s["health"] not in BAD else "")
                self.T(state, 15, col if s["sev"] else MUTED, (r.x + 14, r.y + 42), maxw=sw - 20)
                hot = s["temps"][0] if s["temps"] else None
                det = f"{fmt(s['watts'], ' W')}" + (f" · {hot[1]:.0f}{hot[2]} {hot[0]}" if hot and s["on"] else "")
                self.T(det, 13, MUTED, (r.x + 14, r.y + 68), maxw=sw - 20)

        # alerts
        y = 318
        r = self.pg.Rect(x0, y, cw, H - y - 10)
        self.card(r)
        if not al:
            self.T("No active alerts", 16, MUTED, (r.x + 16, r.y + 16))
        rows = (r.h - 16) // 28
        for i, a in enumerate(al[:rows]):
            yy = r.y + 12 + i * 28
            if i == rows - 1 and len(al) > rows:
                self.T(f"+{len(al) - rows + 1} more", 15, MUTED, (r.x + 34, yy + 2))
                break
            self.dot((r.x + 18, yy + 11), SEV_COL[a.sev], 6)
            tr = self.T(a.text, 16, TEXT if a.key not in self.acked else MUTED, (r.x + 34, yy + 1), maxw=cw - 150)
            if a.detail and tr.right < r.right - 150:
                self.T("— " + a.detail, 13, MUTED, (tr.right + 8, yy + 3), maxw=r.right - 110 - tr.right)
            self.T(self.TITLES.get(a.page, ""), 13, DIM, (r.right - 14, yy + 3), "topright")

    def page_power(self, m):
        x0 = 12
        ups, pdu = m["ups"], m["pdu"]
        # UPS card
        r = self.pg.Rect(x0, 50, 250, H - 60)
        self.card(r)
        self.T("UPS", 15, MUTED, (r.x + 14, r.y + 10))
        stc = SEV_COL[ups["sev"]] if ups["sev"] else OK
        self.T(ups["status"] or "no data", 15, stc, (r.right - 14, r.y + 10), "topright", bold=True)
        self.T(fmt(ups["batt"], "%"), 48, TEXT, (r.x + 14, r.y + 32), bold=True)
        self.bar((r.x + 14, r.y + 92, r.w - 28, 10), (ups["batt"] or 0) / 100, stc)
        self.T(fmt(ups["runtime"], " min runtime"), 16, TEXT, (r.x + 14, r.y + 110))
        self.T("Load", 14, MUTED, (r.x + 14, r.y + 140))
        self.T(fmt(ups["load"], "%"), 14, TEXT, (r.right - 14, r.y + 140), "topright")
        lc = WARN if (ups["load"] or 0) > 80 else INFO
        self.bar((r.x + 14, r.y + 160, r.w - 28, 8), (ups["load"] or 0) / 100, lc)
        y = r.y + 182
        w = r.w - 28
        y = self.kv(r.x + 14, y, w, "Input", f"{fmt(ups['vin'], ' V', 1)} {fmt(ups['freq'], ' Hz')}")
        y = self.kv(r.x + 14, y, w, "Output", fmt(ups["vout"], " V", 1))
        y = self.kv(r.x + 14, y, w, "Battery", fmt(ups["bvolt"], " V", 1))
        y = self.kv(r.x + 14, y, w, "Battery date", ups["bdate"] or "—")
        y = self.kv(r.x + 14, y, w, "UPS temp", fmt(ups["temp"], "°F", 1))
        y = self.kv(r.x + 14, y, w, "Self-test", ups["selftest"] or "—")
        self.T("Last transfer: " + (ups["reason"] or "—"), 12, MUTED, (r.x + 14, y + 2), maxw=w)
        test = self.ups_test
        label = f"Testing… {time.time() - test['start']:.0f} s" if test else "Run UPS self-test"
        self.button((r.x + 14, r.bottom - 52, w, 40), label,
                    lambda: self.ask("Run UPS self-test?",
                                     ["The UPS switches to battery for a short test,",
                                      "then reports the result. Load stays powered."],
                                     self.start_ups_test),
                    enabled=ups["present"] and not test)

        # PDU card
        r = self.pg.Rect(x0 + 258, 50, CW - 24 - 258, H - 60)
        self.card(r)
        self.T("PDU outlets", 15, MUTED, (r.x + 14, r.y + 10))
        pc = SEV_COL[pdu["sev"]] if pdu["sev"] else MUTED
        self.T(f"{fmt(pdu['watts'], ' W')} / {fmt(pdu['budget'], ' W')}", 15, pc if pdu["sev"] else TEXT,
               (r.right - 14, r.y + 10), "topright", bold=True)
        outs = pdu["outlets"]
        if not outs:
            self.T("No PDU data", 16, MUTED, (r.x + 14, r.y + 50))
            return
        top = max((w or 0) for _, w, _ in outs) or 1
        cols = 2
        rows = math.ceil(len(outs) / cols)
        cwid = (r.w - 28 - 16) // cols
        rh = min(26, (r.h - 44) // rows)
        for i, (o, w, sw) in enumerate(outs):
            cx = r.x + 14 + (i // rows) * (cwid + 16)
            cy = r.y + 40 + (i % rows) * rh
            off = sw == "off"
            self.T(f"{o:>2}", 13, DIM if off else MUTED, (cx, cy + 2))
            self.bar((cx + 26, cy + 7, cwid - 92, 7), (w or 0) / top, DIM if off else INFO)
            self.T("off" if off else fmt(w, " W", 0 if (w or 0) >= 10 else 1), 13,
                   DIM if off or not w else TEXT, (cx + cwid, cy + 2), "topright")

    def page_servers(self, m):
        x0 = 12
        sv = m["servers"]
        if not sv:
            self.T("No iDRAC servers found", 20, MUTED, (CW // 2, H // 2), "center")
            return
        s = sv[self.srv_idx % len(sv)]
        col = SEV_COL[s["sev"]] if s["sev"] else (OK if s["on"] else DIM)
        r = self.pg.Rect(x0, 50, 236, H - 60)
        self.card(r)
        self.dot((r.x + 22, r.y + 28), col, 10)
        self.T(s["name"], 26, TEXT, (r.x + 42, r.y + 12), bold=True, maxw=r.w - 52)
        self.T("POWERED ON" if s["on"] else ("POWERED OFF" if s["power"] == "off" else "NO DATA"), 15,
               OK if s["on"] else MUTED, (r.x + 14, r.y + 54), bold=True)
        w = r.w - 28
        y = r.y + 84
        hc = SEV_COL[s["sev"]] if s["sev"] else TEXT
        y = self.kv(r.x + 14, y, w, "Health", s["health"] or "—", hc)
        y = self.kv(r.x + 14, y, w, "Power", fmt(s["watts"], " W"))
        y = self.kv(r.x + 14, y, w, "PSU in", f"{fmt(s['psu_v'], ' V')} {fmt(s['psu_a'], ' A', 1)}")
        y = self.kv(r.x + 14, y, w, "OS", {"on": "running", "off": "not running"}.get(s["os"], "—"))
        y = self.kv(r.x + 14, y, w, "iDRAC", {"on": "reachable", "off": "unreachable"}.get(s["avail"], "—"),
                    CRIT if s["avail"] == "off" else TEXT)
        y = self.kv(r.x + 14, y, w, "Fan redund.", s["redund"] if s["redund"] not in BAD else "—")
        if s["sev"] and s["on"] and s["health"] not in BAD and s["health"].lower() != "ok":
            self.T("Why " + s["health"], 13, MUTED, (r.x + 14, y + 2))
            y += 20
            if s["reason"] in BAD:
                for line in self.wrap("Cause not reported. Update the iDRAC MQTT Poller add-on to 1.0.3.", 13, w, 3):
                    self.T(line, 13, DIM, (r.x + 14, y))
                    y += 17
            else:
                for line in self.wrap(s["reason"], 13, w, 4):
                    self.T(line, 13, hc, (r.x + 14, y))
                    y += 17
            y += 6
        acts = s["actions"]
        if acts:
            rows = (len(acts) + 1) // 2
            by = r.bottom - 14 - 40 * rows - 8 * (rows - 1)
            bw = (w - 8) // 2
            for i, (key, label, ent) in enumerate(acts):
                bx = r.x + 14 + (i % 2) * (bw + 8)
                byy = by + (i // 2) * 48
                bwid = w if len(acts) % 2 and i == len(acts) - 1 else bw
                pkey = f"{s['id']}-{key}"
                sent = time.monotonic() - self.pending.get(pkey, -999) < 30
                essential = s["id"] in ESSENTIAL_SERVERS
                col = OK if key == "on" else WARN if key == "soft" else INFO
                body = {"on": [f"Sends IPMI power-on to {s['name']}."],
                        "soft": [f"Asks {s['name']}'s OS to shut down cleanly",
                                 "(ACPI power button). Unsaved work is lost."],
                        "sel": [f"Clears {s['name']}'s iDRAC System Event Log.",
                                "Health may return to OK if old events caused it."]}[key]
                if essential and key == "soft":
                    body = body + ["", f"{s['name']} is ESSENTIAL: the network may go down."]
                short = {"soft": "Shut down"}.get(key, label)
                self.button((bx, byy, bwid, 40), "Sent ✓" if sent else short,
                            lambda key=key, label=label, ent=ent, body=body, pkey=pkey, essential=essential, s=s:
                            self.ask(f"{label}: {s['name']}?", body,
                                     lambda: self.send(pkey, f"{label} {s['name']}", "button", "press",
                                                       {"entity_id": ent}),
                                     hold=essential or key == "soft", danger=key == "soft"),
                            col=col, enabled=not sent, size=14)
            r = self.pg.Rect(r.x, r.y, r.w, by - r.y - 4)   # keep last error above the buttons
        if s["last_error"] not in BAD:
            self.T("Last error", 13, MUTED, (r.x + 14, y + 4))
            words, line, yy = s["last_error"].split(), "", y + 22
            for wd in words:
                if self.F(13).size(line + " " + wd)[0] > w:
                    self.T(line, 13, WARN, (r.x + 14, yy))
                    line, yy = wd, yy + 17
                    if yy > r.bottom - 20:
                        break
                else:
                    line = (line + " " + wd).strip()
            self.T(line, 13, WARN, (r.x + 14, yy))

        r = self.pg.Rect(x0 + 244, 50, CW - 24 - 244, H - 60)
        self.card(r)
        y = r.y + 10
        self.T("Temperatures", 15, MUTED, (r.x + 14, y))
        y += 26
        temps = s["temps"]
        if not temps:
            self.T("No readings" + ("" if s["on"] else " (server off)"), 15, DIM, (r.x + 14, y))
            y += 30
        maxrows = 8 if s["fans"] else 14
        for label, v, u in temps[:maxrows]:
            f = v if "F" in u else v * 9 / 5 + 32
            tc = CRIT if f >= 185 else WARN if f >= 158 else OK
            self.T(label, 14, TEXT, (r.x + 14, y))
            self.bar((r.x + 150, y + 6, r.w - 250, 7), (f - 60) / 140, tc)
            self.T(f"{v:.0f}{u}", 14, TEXT, (r.right - 14, y), "topright")
            y += 23
        if s["fans"]:
            y += 8
            self.T("Fans", 15, MUTED, (r.x + 14, y))
            y += 26
            for n, rpm in s["fans"]:
                if y > r.bottom - 22:
                    break
                self.T(f"Fan {n}", 14, TEXT if rpm else DIM, (r.x + 14, y))
                self.bar((r.x + 150, y + 6, r.w - 250, 7), (rpm or 0) / 15000, INFO)
                self.T(fmt(rpm, " rpm"), 14, TEXT if rpm else DIM, (r.right - 14, y), "topright")
                y += 23

    def page_network(self, m):
        x0 = 12
        fw = m["fw"]
        # firewall
        r = self.pg.Rect(x0, 50, 330, 248)
        self.card(r)
        self.T("Firewall", 15, MUTED, (r.x + 14, r.y + 10))
        y = r.y + 36
        w = r.w - 28

        def updown(v, good):
            return (v or "—"), (OK if v == good else CRIT if v not in BAD else MUTED)
        for label, (v, c) in [("WAN gateway", updown(fw["wan_gw"], "online")),
                              ("Backup WAN", updown(fw["bwan_gw"], "online")),
                              ("LTE (WWAN)", updown(fw["wwan"], "up")),
                              ("OpenVPN", updown(fw["vpn"], "up"))]:
            self.dot((r.x + 20, y + 9), c, 5)
            self.T(label, 14, TEXT, (r.x + 32, y))
            self.T(v, 14, c, (r.right - 14, y), "topright")
            y += 24
        y += 6
        ru = fw["rate_unit"]
        y = self.kv(r.x + 14, y, w, "WAN ↓ / ↑", f"{fmt(fw['wan_in'], '', 2)} / {fmt(fw['wan_out'], '', 2)} {ru}", size=15)
        y = self.kv(r.x + 14, y, w, "LAN ↓ / ↑", f"{fmt(fw['lan_in'], '', 1)} / {fmt(fw['lan_out'], '', 1)} {ru}", size=15)
        y = self.kv(r.x + 14, y, w, "CPU · mem · temp",
                    f"{fmt(fw['cpu'], '%')} · {fmt(fw['mem'], '%')} · {fmt(fw['temp'], fw['temp_u'])}", size=15)

        # ping
        r = self.pg.Rect(x0 + 338, 50, CW - 24 - 338, 248)
        self.card(r)
        self.T("Ping from rack", 15, MUTED, (r.x + 14, r.y + 10))
        y = r.y + 36
        for p in m["pings"]:
            c = SEV_COL[p["sev"]] if p["sev"] else (OK if p["ms"] is not None else MUTED)
            self.dot((r.x + 20, y + 9), c, 5)
            self.T(p["label"], 14, TEXT, (r.x + 32, y))
            self.T(fmt(p["ms"], " ms", 1, none="no reply" if p["fails"] else "…"), 14, c if p["sev"] else TEXT,
                   (r.right - 14, y), "topright")
            self.T(p["host"], 11, DIM, (r.x + 32, y + 17))
            y += 36

        # switches
        y0 = 306
        sws = m["switches"]
        ether = {s.get("hostname", ""): s for s in m["etherlight"]}
        n = max(1, len(sws))
        sw_w = (CW - 24 - (n - 1) * 8) // n
        for i, s in enumerate(sws):
            r = self.pg.Rect(x0 + i * (sw_w + 8), y0, sw_w, H - y0 - 10)
            self.card(r)
            c = SEV_COL[s["sev"]] if s["sev"] else OK
            self.dot((r.x + 18, r.y + 20), c, 6)
            self.T(s["name"], 16, TEXT, (r.x + 32, r.y + 10), bold=True)
            self.T(s["state"] or "—", 13, c, (r.right - 14, r.y + 12), "topright")
            w = r.w - 28
            y = r.y + 40
            y = self.kv(r.x + 14, y, w, "Temp", fmt(s["temp"], s["temp_u"], 1), size=15)
            y = self.kv(r.x + 14, y, w, "CPU / mem", f"{fmt(s['cpu'], '%')} / {fmt(s['mem'], '%')}", size=15)
            el = next((e for k, e in ether.items() if k.replace("-", "_").lower().startswith(
                s["name"].replace(" ", "_").lower())), None)
            if el:
                on = el.get("state", {}).get("On")
                bri = el.get("state", {}).get("Brightness")
                self.kv(r.x + 14, y, w, "Etherlight", f"on · {round(bri * 100 / 255)}%" if on and bri is not None else ("on" if on else "off"), size=15)
        if not sws:
            self.T("No switch data", 15, MUTED, (x0 + 14, y0 + 10))

    def _next_fan_detail(self):
        """Controller the middle button opens next: overview -> 1st online -> 2nd online ... -> overview."""
        m = self.model
        names = [fc["name"] for fc in (m["fanctl"] if m else []) if not fc["offline"]]
        if not names:
            return None
        if self.fan_detail not in names:
            return names[0] if self.fan_detail is None else None
        i = names.index(self.fan_detail)
        return names[i + 1] if i + 1 < len(names) else None

    def fan_toggle(self, fc, fans, turn_on):
        """Turn a set of fans on (immediately) or off (after confirming)."""
        ents = [f["entity"] for f in fans if f["entity"] and f["state"] not in BAD]
        if not ents:
            return
        label = f"{fc['name']} fan {fans[0]['n']}" if len(fans) == 1 else f"all {fc['name']} fans"
        key = "fans-" + ",".join(ents)
        if turn_on:
            self.send(key, f"{label} on", "fan", "turn_on", {"entity_id": ents})
        else:
            self.ask(f"Turn off {label}?", ["The fan" + ("s stop" if len(ents) > 1 else " stops") +
                                            " cooling the rack until", "you turn " +
                                            ("them" if len(ents) > 1 else "it") + " back on."],
                     lambda: self.send(key, f"{label} off", "fan", "turn_off", {"entity_id": ents}), danger=True)

    def page_cooling(self, m):
        x0 = 12
        fcs = m["fanctl"]
        detail = next((fc for fc in fcs if fc["name"] == self.fan_detail), None)
        if detail:
            return self.cooling_detail(detail)
        self.fan_detail = None
        n = max(1, len(fcs))
        cw = (CW - 24 - (n - 1) * 8) // n
        for i, fc in enumerate(fcs):
            r = self.pg.Rect(x0 + i * (cw + 8), 50, cw, 262)
            self.card(r)
            c = SEV_COL[fc["sev"]] if fc["sev"] else OK
            self.dot((r.x + 18, r.y + 20), c, 6)
            tr = self.T(f"{fc['name']} ›", 16, TEXT, (r.x + 32, r.y + 10), bold=True)
            self.hit(tr.inflate(40, 20).move(-10, 0), lambda fc=fc: setattr(self, "fan_detail", fc["name"]))
            self.T("OFFLINE" if fc["offline"] else f"ESP32 {fmt(fc['esp'], fc['esp_u'])}", 13,
                   WARN if fc["offline"] else MUTED, (r.right - 14, r.y + 12), "topright")
            y = r.y + 40
            top = max([f["rpm"] or 0 for f in fc["fans"]] + [3000])
            for f in fc["fans"]:
                fit = f["fitted"] and not fc["offline"]
                on = f["state"] == "on"
                fcol = CRIT if f["alert"] else INFO if on or f["state"] in BAD else DIM
                self.T(f"Fan {f['n']}", 14, TEXT if fit else DIM, (r.x + 14, y))
                if fit:
                    self.bar((r.x + 70, y + 6, r.w - 200, 7), (f["rpm"] or 0) / top if on or f["state"] in BAD else 0,
                             fcol)
                    if on or f["state"] in BAD:
                        self.T(f"{fmt(f['rpm'])} rpm", 14, TEXT, (r.right - 54, y), "topright")
                        self.T(fmt(f["pwm"], "%"), 13, MUTED, (r.right - 14, y + 1), "topright")
                    else:
                        self.T("off", 14, MUTED, (r.right - 14, y), "topright", bold=True)
                else:
                    self.T("—", 14, DIM, (r.right - 14, y), "topright")
                y += 30
            fitted = [f for f in fc["fans"] if f["fitted"]]
            ctl = [f for f in fitted if f["entity"] and f["state"] not in BAD]
            any_on = any(f["state"] == "on" for f in fitted)
            btn = self.pg.Rect(r.x + 12, r.bottom - 52, r.w - 24, 40)
            if fc["offline"] or not ctl:
                self.pg.draw.rect(self.canvas, CARD2, btn, border_radius=8)
                self.T("Controller offline" if fc["offline"] else "No fan control", 14, DIM, btn.center, "center")
            else:
                running = sum(1 for f in fitted if f["state"] == "on")
                self.button(btn, f"ALL FANS {'ON' if any_on else 'OFF'}  ·  {running}/{len(fitted)} running",
                            lambda fc=fc, ctl=ctl, any_on=any_on: self.fan_toggle(fc, ctl, not any_on),
                            col=OK if any_on else MUTED, filled=any_on, size=15)
        # device temperatures
        r = self.pg.Rect(x0, 320, CW - 24, H - 330)
        self.hit(r, lambda: None)
        self.card(r)
        self.T("Equipment temperatures", 15, MUTED, (r.x + 14, r.y + 10))
        items = []
        for s in m["switches"]:
            items.append((s["name"], s["temp"], s["temp_u"]))
        fw = m["fw"]
        if fw["present"]:
            items.append(("Firewall CPU", fw["temp"], fw["temp_u"]))
        if m["ups"]["present"]:
            items.append(("UPS", m["ups"]["temp"], "°F"))
        for s in m["servers"]:
            if s["on"] and s["temps"]:
                items.append((f"{s['name']} (hottest)", s["temps"][0][1], s["temps"][0][2]))
        cols = 2
        colw = (r.w - 28 - 20) // cols
        for i, (label, v, u) in enumerate(items[:8]):
            cx = r.x + 14 + (i % cols) * (colw + 20)
            cy = r.y + 36 + (i // cols) * 26
            f = None if v is None else (v if "F" in u else v * 9 / 5 + 32)
            tc = MUTED if f is None else CRIT if f >= 185 else WARN if f >= 150 else OK
            self.dot((cx + 6, cy + 9), tc, 5)
            self.T(label, 14, TEXT, (cx + 18, cy), maxw=colw - 90)
            self.T(fmt(v, u, 1), 14, TEXT, (cx + colw, cy), "topright")

    def cooling_detail(self, fc):
        x0 = 12
        r = self.pg.Rect(x0, 50, CW - 24, H - 60)
        self.card(r)
        self.button((r.x + 12, r.y + 10, 90, 32), "‹ Back", lambda: setattr(self, "fan_detail", None),
                    col=MUTED, size=14)
        c = SEV_COL[fc["sev"]] if fc["sev"] else OK
        self.dot((r.x + 122, r.y + 26), c, 6)
        self.T(fc["name"], 18, TEXT, (r.x + 136, r.y + 14), bold=True)
        self.T("OFFLINE" if fc["offline"] else f"ESP32 {fmt(fc['esp'], fc['esp_u'])}", 14,
               WARN if fc["offline"] else MUTED, (r.right - 14, r.y + 16), "topright")
        y = r.y + 60
        top = max([f["rpm"] or 0 for f in fc["fans"]] + [3000])
        for f in fc["fans"]:
            fit = f["fitted"] and not fc["offline"]
            on = f["state"] == "on"
            row = self.pg.Rect(r.x + 10, y, r.w - 20, 64)
            self.pg.draw.rect(self.canvas, CARD2 if fit else CARD, row, border_radius=10)
            self.T(f"Fan {f['n']}", 18, TEXT if fit else DIM, (row.x + 16, row.y + 10), bold=True)
            if not fit:
                self.T("not connected" if not fc["offline"] else "offline", 14, DIM, (row.x + 16, row.y + 36))
                y += 72
                continue
            fcol = CRIT if f["alert"] else INFO if on or f["state"] in BAD else DIM
            self.T("ALARM" if f["alert"] else ("running" if on else "off"), 13,
                   CRIT if f["alert"] else OK if on else MUTED, (row.x + 16, row.y + 38), bold=True)
            self.bar((row.x + 120, row.y + 18, row.w - 330, 9), (f["rpm"] or 0) / top if on else 0, fcol)
            self.T(f"{fmt(f['rpm'])} rpm" if on else "—", 17, TEXT if on else MUTED,
                   (row.right - 110, row.y + 10), "topright", bold=on)
            self.T(f"PWM {fmt(f['pwm'], '%')}" if on else "", 13, MUTED, (row.right - 110, row.y + 36), "topright")
            if f["entity"]:
                self.button((row.right - 96, row.y + 12, 84, 40), "ON" if on else "OFF",
                            lambda f=f, on=on: self.fan_toggle(fc, [f], not on),
                            col=OK if on else MUTED, filled=on, size=16, enabled=f["state"] not in BAD)
            y += 72

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

    def goto(self, page, srv=None):
        self.list_centered.clear()
        self.page = self.PAGES.index(page)
        if srv is not None:
            self.srv_idx = srv

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
        for line in ["Tap a tile or server to open it.", "Tap the right strip = the buttons.",
                     "Actions ask to confirm first;", "risky ones need a 1.5 s hold.",
                     "Any press wakes the screen.",
                     "Door closed = sleep; open = wake."]:
            self.T(line, 13, TEXT, (r.x + 14, y))
            y += 19

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

    def page_lights(self, m):
        c = self.canvas
        x0 = 12
        # sub-tabs
        for i, (key, label) in enumerate((("rack", "Rack LEDs"), ("other", "Switch & AP lights"))):
            self.button((x0 + i * 168, 50, 160, 30), label, lambda k=key: setattr(self, "light_tab", k),
                        col=OK if self.light_tab == key else MUTED, filled=self.light_tab == key, size=13)
        if self.light_tab == "other":
            return self.page_lights_other(m)

        st, _, _, connected = self.wled.snapshot()
        if not st:
            self.T("Connecting to WLED-Rack…" if not connected else "Waiting for WLED state…", 18, MUTED,
                   (CW // 2, H // 2 + 20), "center")
            return
        segs = st.get("seg", [])
        cur = next((sg for sg in segs if self.seg_sel == "all" or sg.get("id") == self.seg_sel), segs[0] if segs else {})
        on = st.get("on")

        # ---- left: power, brightness, colour wheel, swatches, segments
        r = self.pg.Rect(x0, 88, 268, H - 98)
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
        r = self.pg.Rect(x0 + 276, 88, CW - 24 - 276, H - 98)
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

    def page_lights_other(self, m):
        L = m["lights"]
        r = self.pg.Rect(12, 88, CW - 24, H - 98)
        self.card(r)
        y = r.y + 20
        w = r.w - 28
        for label, lt in L["extra"]:
            if not lt:
                continue
            on = lt["on"]
            ok = lt["state"] not in BAD
            self.T(label, 17, TEXT if ok else DIM, (r.x + 16, y + 6), bold=True)
            self.button((r.right - 100, y, 84, 36), "ON" if on else "OFF",
                        lambda lt=lt, on=on: self.data.call("light", "turn_off" if on else "turn_on",
                                                            {"entity_id": lt["id"]}),
                        col=OK if on else MUTED, filled=on, size=14, enabled=ok)
            if "uiwled" in lt["id"]:          # Etherlight supports brightness
                self.slider((r.x + 170, y, w - 290, 36), lt["id"], lt["b"] if on else 0, 255,
                            lambda v, lt=lt: self.data.call("light", "turn_on" if v else "turn_off",
                                                            {"entity_id": lt["id"], **({"brightness": v} if v else {})}),
                            )
            y += 70

    def page_hotspot(self, m):
        hs = m["hotspot"]
        a = hs["a"]
        c = self.canvas
        x0 = 12
        if not hs["present"]:
            self.T("No hotspot data in HA", 20, MUTED, (CW // 2, H // 2 - 14), "center")
            self.T("Install / start the WPSD Hotspot Monitor add-on", 14, DIM, (CW // 2, H // 2 + 14), "center")
            return
        orange = (255, 140, 40)
        lv = a.get("live") or {}
        on_air = a.get("on_air") == "on"

        # ---- caller banner (WPSD /live/ style)
        r = self.pg.Rect(x0, 50, CW - 24, 124)
        k = 0.55 + 0.3 * (0.5 + 0.5 * math.sin(time.time() * 5)) if on_air else 0.32
        self.pg.draw.rect(c, self.tint(orange, k), r, border_radius=12)
        if on_air:
            self.pg.draw.rect(c, orange, r, 3, border_radius=12)
        call = lv.get("call") or a.get("last_call") or "—"
        self.T(call, 46, TEXT, (r.x + 18, r.y + 8), bold=True, maxw=300)
        self.T(lv.get("name") or "", 20, TEXT, (r.x + 20, r.y + 64), bold=True, maxw=300)
        self.T(lv.get("location") or "", 14, (240, 220, 200), (r.x + 20, r.y + 92), maxw=300)
        tag = "ON AIR" if on_air else ("LAST CALLER" if lv.get("call") else "")
        if tag:
            tw = self.F(12, True).size(tag)[0] + 18
            chip = self.pg.Rect(r.right - 14 - tw, r.y + 10, tw, 22)
            self.pg.draw.rect(c, orange if on_air else self.tint(orange, 0.6), chip, border_radius=11)
            self.T(tag, 12, BG if on_air else TEXT, chip.center, "center", bold=True)
        tgt = (lv.get("target") or a.get("last_target") or "").replace("BM: ", "")
        m_tg = re.match(r"(TG\s*\d+)\s*(.*)", tgt)
        rx = r.x + 340
        self.T(m_tg.group(1) if m_tg else (tgt or "—"), 30, TEXT, (rx, r.y + 34), bold=True, maxw=r.right - rx - 14)
        self.T(m_tg.group(2) if m_tg else "", 15, (240, 220, 200), (rx, r.y + 70), maxw=r.right - rx - 14)
        dur = lv.get("duration") or ""
        meta = " · ".join(x for x in (lv.get("mode"), lv.get("source"), dur.split(" (")[0],
                                       f"loss {lv['loss']}" if lv.get("loss") else None) if x)
        self.T(meta, 13, (240, 220, 200), (rx, r.y + 96), maxw=r.right - rx - 14)

        # ---- talkgroups (BrandMeister)
        r = self.pg.Rect(x0, 182, 330, H - 192)
        self.card(r)
        self.T("Talkgroups · BrandMeister", 15, MUTED, (r.x + 14, r.y + 10))
        net_ok = a.get("dmr_net") == "on"
        self.T("linked" if net_ok else "DOWN", 13, OK if net_ok else WARN, (r.right - 14, r.y + 12), "topright", bold=True)
        y = r.y + 36
        w = r.w - 28
        rows = [(t, "static") for t in a.get("tg_static") or []] + [(t, "dynamic") for t in a.get("tg_dynamic") or []] \
            + [(t, "timed") for t in a.get("tg_timed") or []]
        if not rows:
            self.T("No talkgroups linked" if "tg_static" in a else "Waiting for BrandMeister…", 14, DIM, (r.x + 14, y + 4))
            y += 30
        for t, kind in rows[:4]:
            col = {"static": OK, "dynamic": PAGE_COLORS["hotspot"][0], "timed": INFO}[kind]
            box = self.pg.Rect(r.x + 10, y, r.w - 20, 52)
            self.pg.draw.rect(c, CARD2, box, border_radius=8)
            self.pg.draw.rect(c, col, (box.x, box.y + 8, 4, box.h - 16), border_radius=2)
            self.T(f"TG {t['tg']}", 17, TEXT, (box.x + 14, box.y + 5), bold=True)
            self.T(t.get("name") or "", 13, MUTED, (box.x + 14, box.y + 26), maxw=box.w - 110)
            label = kind
            if t.get("slot"):
                label += f" · TS{t['slot']}"
            self.T(label, 12, col, (box.right - 10, box.y + 6), "topright", bold=True)
            if "expires_in" in t:
                left = int(t["expires_in"])
                self.T(f"{left // 60}:{left % 60:02d} left", 12, MUTED, (box.right - 10, box.y + 26), "topright")
                self.bar((box.x + 14, box.bottom - 6, box.w - 24, 3), left / 900, col)
            y += 58
        y += 4
        auto = a.get("tg_autostatic")
        y = self.kv(r.x + 14, y, w, "Auto-static", f"TG {auto}" if auto else "none", size=14)
        y = self.kv(r.x + 14, y, w, "Master", a.get("dmr_master") or (f"BM {a['bm_master']}" if a.get("bm_master") else "—"),
                    size=14)
        blocked = a.get("tg_blocked") or []
        if blocked:
            y = self.kv(r.x + 14, y, w, "Blocked", ", ".join(map(str, blocked)), WARN, size=14)
        freq = f"{a['freq_mhz']:.3f} MHz" if a.get("freq_mhz") else "—"
        self.T(f"{freq} · {a.get('status') or '—'} · {fmt(a.get('cpu_temp_c'), '°C')}", 12, DIM,
               (r.x + 14, r.bottom - 22), maxw=w)

        # ---- last heard
        r = self.pg.Rect(x0 + 338, 182, CW - 24 - 338, H - 192)
        self.card(r)
        self.T("Last heard", 15, MUTED, (r.x + 14, r.y + 10))
        self.T(f"{a.get('host', '')}", 12, DIM, (r.right - 14, r.y + 12), "topright")
        y = r.y + 36
        lh = a.get("last_heard") or []
        if not lh:
            self.T("Nothing heard yet", 14, DIM, (r.x + 14, y))
        for row in lh[:6]:
            tx = (row.get("dur") or "").upper() == "TX"
            call = row.get("callsign") or "?"
            src = row.get("src") or ""
            if tx:
                self.pg.draw.rect(c, self.tint(orange, 0.3), (r.x + 8, y - 3, r.w - 16, 40), border_radius=7)
            self.T(call, 15, orange if tx else TEXT, (r.x + 14, y), bold=True)
            self.T(src, 10, OK if src == "RF" else DIM, (r.x + 14 + self.F(15, True).size(call)[0] + 6, y + 4),
                   bold=src == "RF")
            tm = (row.get("time") or "").split(" ")
            self.T(" ".join(tm[:2]) if len(tm) >= 2 else "", 11, MUTED, (r.right - 14, y + 2), "topright")
            tg = (row.get("target") or "").replace("BM: ", "")
            self.T(tg, 12, MUTED, (r.x + 14, y + 18), maxw=r.w - 90)
            self.T("TX" if tx else (f"{row.get('dur')}s" if row.get("dur") else ""), 12, orange if tx else MUTED,
                   (r.right - 14, y + 18), "topright", bold=tx)
            y += 42

    def page_ha(self, m):
        ha = m["ha"]
        hm = ha["meta"]
        x0 = 12
        sy, su = ha["sys"], ha["sys_u"]
        # ---- left: core status
        r = self.pg.Rect(x0, 50, 300, H - 60)
        self.card(r)
        self.T("Home Assistant", 15, MUTED, (r.x + 14, r.y + 10))
        okc = OK if m["ha_age"] is not None and m["ha_age"] < 30 else CRIT
        pill = self.pg.Rect(r.x + 14, r.y + 36, r.w - 28, 46)
        self.pg.draw.rect(self.canvas, self.tint(okc, 0.3), pill, border_radius=10)
        self.pg.draw.rect(self.canvas, okc, pill, 2, border_radius=10)
        self.dot((pill.x + 22, pill.centery), okc, 8)
        self.T("RUNNING" if okc == OK else "UNREACHABLE", 21, okc, (pill.x + 40, pill.centery), "midleft", bold=True)
        lat = ha["latency"]
        self.T(f"{lat * 1000:.0f} ms" if lat else "", 13, MUTED, (pill.right - 12, pill.centery), "midright")
        y = r.y + 94
        w = r.w - 28
        upd_core = hm.get("core_latest") and hm.get("core_latest") != hm.get("core")
        y = self.kv(r.x + 14, y, w, "Core", (hm.get("core") or "—") + (f" → {hm['core_latest']}" if upd_core else ""),
                    WARN if upd_core else TEXT, size=15)
        y = self.kv(r.x + 14, y, w, "Supervisor", hm.get("supervisor") or "—", size=15)
        y = self.kv(r.x + 14, y, w, "OS", hm.get("os") or "—", size=15)
        y = self.kv(r.x + 14, y, w, "Entities", f"{hm.get('total', '—')}", size=15)
        un = hm.get("unavailable")
        y = self.kv(r.x + 14, y, w, "Unavailable", f"{un}" if un is not None else "—", WARN if un else TEXT, size=15)
        nt = hm.get("notifications")
        y = self.kv(r.x + 14, y, w, "Notifications", f"{nt}" if nt is not None else "—", WARN if nt else TEXT, size=15)
        z = ha["z2m"]
        y = self.kv(r.x + 14, y, w, "Zigbee2MQTT", {"on": "connected", "off": "DISCONNECTED"}.get(z, "—"),
                    OK if z == "on" else WARN if z == "off" else DIM, size=15)
        y += 6
        self.T("Add-ons reporting", 13, MUTED, (r.x + 14, y))
        y += 22
        for label, ok in ha["addons"]:
            col = OK if ok else DIM if ok is None else WARN
            self.dot((r.x + 20, y + 8), col, 5)
            self.T(label, 14, TEXT, (r.x + 32, y))
            self.T("ok" if ok else "—" if ok is None else "no data", 13, col, (r.right - 14, y + 1), "topright")
            y += 24

        # ---- right top: host
        rx = x0 + 308
        r = self.pg.Rect(rx, 50, CW - 24 - 308, 196)
        self.card(r)
        self.T("Host", 15, MUTED, (r.x + 14, r.y + 10))
        w = r.w - 28
        y = r.y + 38

        def meter(label, val, txt, frac, col):
            nonlocal y
            self.T(label, 14, MUTED, (r.x + 14, y))
            self.T(txt, 14, TEXT, (r.right - 14, y), "topright")
            self.bar((r.x + 14, y + 22, w, 8), frac, col)
            y += 38
        cpu = sy["processor_use"]
        meter("CPU", cpu, f"{fmt(cpu, '%')} · load {fmt(sy['load_1m'], '', 2)}", (cpu or 0) / 100,
              WARN if (cpu or 0) > 80 else INFO)
        mem = sy["memory_use_percent"]
        meter("Memory", mem, fmt(mem, "%"), (mem or 0) / 100, WARN if (mem or 0) > 90 else INFO)
        df, du = sy["disk_free"], sy["disk_use"]
        dpct = 100 * du / (df + du) if df is not None and du else None
        meter("Disk", dpct, f"{fmt(du, ' GiB', 1)} used · {fmt(df, ' GiB', 1)} free", (dpct or 0) / 100,
              WARN if (dpct or 0) > 90 else INFO)
        t = sy["processor_temperature"]
        self.T("CPU temp", 14, MUTED, (r.x + 14, y))
        self.T(fmt(t, su["processor_temperature"].replace("Â", "") or "°", 1), 14, TEXT, (r.right - 14, y), "topright")

        # ---- right bottom: backups + updates
        r = self.pg.Rect(rx, 254, CW - 24 - 308, H - 264)
        self.card(r)
        self.T("Backups & updates", 15, MUTED, (r.x + 14, r.y + 10))
        y = r.y + 36
        w = r.w - 28
        ah = ha["backup_age_h"]
        bcol = OK if ah is not None and ah < 26 else WARN if ah is not None else DIM
        y = self.kv(r.x + 14, y, w, "Last backup",
                    "—" if ah is None else (f"{ah:.0f} h ago" if ah < 48 else f"{ah / 24:.0f} days ago"), bcol, size=15)
        nxt = ha["backup_next"]
        try:
            import datetime as _dt
            nx = _dt.datetime.fromisoformat(nxt).astimezone().strftime("%a %H:%M")
        except (TypeError, ValueError):
            nx = "—"
        y = self.kv(r.x + 14, y, w, "Next backup", nx, size=15)
        ups = hm.get("updates") or []
        if ups:
            self.T(f"{len(ups)} update{'s' if len(ups) > 1 else ''} available", 14, WARN, (r.x + 14, y + 4), bold=True)
            y += 26
            for u in ups[:3]:
                self.T("• " + u.replace(" Update", "").replace(" Firmware", ""), 13, TEXT, (r.x + 14, y), maxw=w)
                y += 20
            if len(ups) > 3:
                self.T(f"+{len(ups) - 3} more", 12, MUTED, (r.x + 14, y))
        else:
            self.T(f"All {hm.get('update_count', '')} components up to date", 14, OK, (r.x + 14, y + 4))

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

    def page_ports(self, m):
        pm = m["portmap"]
        c = self.canvas
        x0 = 12
        accent = PAGE_COLORS["ports"][0]
        if not pm["present"]:
            self.T("No port map in HA yet", 20, MUTED, (CW // 2, H // 2 - 14), "center")
            self.T("Install / configure the UniFi Port Map add-on", 14, DIM, (CW // 2, H // 2 + 14), "center")
            return
        clients = pm["clients"]
        sel = self.hunt_client()
        blink_on = self.hunt_active() and (time.monotonic() % 0.8) < 0.45

        # ---- left: client list
        r = self.pg.Rect(x0, 50, 262, H - 60)
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

        # ---- right top: switch faceplate (odd ports on top, even below, like the real jacks)
        rx = x0 + 270
        r = self.pg.Rect(rx, 50, CW - 24 - 270, 150)
        self.card(r)
        self.T(pm["switch"] or "48-port switch", 15, MUTED, (r.x + 14, r.y + 10))
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
                is_sel = sel and int(sel.get("port") or 0) == port
                if is_sel:
                    colr = TEXT if blink_on else accent
                    self.pg.draw.rect(c, colr, pr.inflate(4, 4), border_radius=3)
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

        # ---- right bottom: selected client + blink control
        r = self.pg.Rect(rx, 208, CW - 24 - 270, H - 218)
        self.card(r)
        if not sel:
            self.T("Tap a client on the left.", 16, MUTED, (r.x + 16, r.y + 16))
            self.T("Its jack lights up here and blinks", 14, DIM, (r.x + 16, r.y + 46))
            self.T("on the real switch.", 14, DIM, (r.x + 16, r.y + 66))
            return
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
        y = self.kv(r.x + 14, y, w, "Network", f"{sel.get('network') or '—'}" +
                    (f" (VLAN {sel['vlan']})" if sel.get("vlan") else ""), size=14)
        if self.hunt_active():
            left = int(self.hunt_until - time.monotonic())
            self.button((r.x + 12, r.bottom - 50, w + 4, 40), f"STOP BLINKING · {left}s", self.hunt_stop,
                        col=accent, filled=True, size=15)
        else:
            self.button((r.x + 12, r.bottom - 50, w + 4, 40), f"BLINK PORT {sel.get('port')}", self.hunt_start,
                        col=accent, size=15)

    def page_time(self, m):
        t = m["time"]
        srv = t["server"]
        x0 = 12
        c = self.canvas
        # --- server status card
        r = self.pg.Rect(x0, 50, 288, H - 60)
        self.card(r)
        sn = t["snmp"]
        self.T("GPS time server", 15, MUTED, (r.x + 14, r.y + 10))
        self.T("via HA" if t["source"] == "ha" else "direct", 13, DIM, (r.right - 14, r.y + 12), "topright")
        if t["fails"] >= 2:
            state, col = "NO REPLY", CRIT
        elif not srv:
            state, col = "WAITING…", MUTED
        elif t["locked"]:
            state, col = "GPS LOCKED", OK
        else:
            state, col = "NOT LOCKED", CRIT
        pill = self.pg.Rect(r.x + 14, r.y + 36, r.w - 28, 46)
        self.pg.draw.rect(c, self.tint(col, 0.3), pill, border_radius=10)
        self.pg.draw.rect(c, col, pill, 2, border_radius=10)
        self.dot((pill.x + 22, pill.centery), col, 8)
        self.T(state, 22, col, (pill.x + 40, pill.centery), "midleft", bold=True)
        y = r.y + 92
        w = r.w - 28
        sz = 15
        if sn:
            hc = sn["health_code"]
            y = self.kv(r.x + 14, y, w, "GPS receiver", GPS_HEALTH.get(hc, sn.get("gps_health", "?")),
                        OK if hc == 8 else WARN if hc in (4, 5, 6, 7) else CRIT, size=sz)
            y = self.kv(r.x + 14, y, w, "Receiver mode",
                        {"Hold": "Position hold", "Survey": "Surveying", "Dynamic": "Dynamic"}.get(sn["mode"], sn["mode"]),
                        size=sz)
            y = self.kv(r.x + 14, y, w, "Timing", sn.get("timing_status") or "—",
                        OK if (sn.get("timing_status") or "").lower() == "good" else WARN, size=sz)
            y = self.kv(r.x + 14, y, w, "Holdover", "no (GPS disciplined)" if not sn["fly"] else f"{sn['fly']} s",
                        TEXT if not sn["fly"] else WARN, size=sz)
            if sn["sat_count"]:
                used = sum(1 for x in sn["sats"] if x["used"])
                y = self.kv(r.x + 14, y, w, "Satellites", f"{used} used / {sn['sat_count']} tracked", size=sz)
            else:
                y = self.kv(r.x + 14, y, w, "Satellites", "not reported (locked)" if hc == 8 else "none", DIM if hc == 8 else CRIT, size=sz)
            y = self.kv(r.x + 14, y, w, "GPS − UTC", f"{sn.get('utc_offset', '?')} s (leap secs)", size=sz)
            y = self.kv(r.x + 14, y, w, "Oscillator", (sn.get("freq") or "—").replace(" PPM", " ppm"), size=sz)
        if srv:
            leap = {0: "none", 1: "+1 s pending", 2: "−1 s pending", 3: "unsynchronised"}[srv["leap"]]
            y = self.kv(r.x + 14, y, w, "Stratum", f"{srv['stratum']} · ref {srv['refid']}", size=sz)
            y = self.kv(r.x + 14, y, w, "Leap second", leap, WARN if srv["leap"] else TEXT, size=sz)
            prec = 2.0 ** srv["precision"]
            y = self.kv(r.x + 14, y, w, "Precision · disp.",
                        f"±{prec * 1e6:.0f} µs · {srv['root_disp'] * 1000:.2f} ms", size=sz)
            if not sn:
                age = srv["ref_age"]
                y = self.kv(r.x + 14, y, w, "Last GPS update", "—" if age is None else f"{age:.0f} s ago",
                            WARN if age and age > 300 else TEXT, size=sz)
        else:
            self.T("Waiting for first reply…", 15, DIM, (r.x + 14, y))
        if sn:
            up = re.match(r"(\d+)(?: days?|:)", sn.get("uptime") or "")
            self.T(f"fw {sn.get('firmware', '?')}" + (f" · up {up.group(1)} d" if up else ""), 11, DIM,
                   (r.x + 14, r.bottom - 58))
        if t["ports"]:
            self.T("LAN ports", 14, MUTED, (r.x + 14, r.bottom - 40))
            for i, st in enumerate(t["ports"]):
                pc = OK if st == "up" else DIM
                bx = r.right - 14 - (len(t["ports"]) - i) * 52
                self.pg.draw.rect(c, self.tint(pc, 0.35), (bx, r.bottom - 44, 44, 26), border_radius=6)
                self.T(f"{i + 1} {'up' if st == 'up' else '—'}", 12, pc if st == "up" else MUTED,
                       (bx + 22, r.bottom - 31), "center", bold=True)

        # --- rack Pi vs server
        rx = x0 + 296
        r = self.pg.Rect(rx, 50, CW - 24 - 296, 196)
        self.card(r)
        pi = t["pi"]
        self.T("This Pi vs GPS clock", 15, MUTED, (r.x + 14, r.y + 10))
        ok_pi = pi.get("synced") and pi.get("server") == TIME_SERVER
        self.T(f"synced ✓ ({pi.get('client')})" if ok_pi else "NOT synced" if pi else "…", 13,
               OK if ok_pi else WARN if pi else DIM, (r.right - 14, r.y + 12), "topright", bold=True)
        if srv and t["fails"] < 2:
            off = srv["offset"] * 1000
            big = f"{off * 1000:+.0f} µs" if abs(off) < 1 else f"{off:+.3f} ms"
            self.T(big, 30, TEXT, (r.x + 14, r.y + 34), bold=True)
            sub = "offset (measured)"
            if pi.get("est") is not None:
                sub += f" · chrony est. {pi['est'] * 1e6:.0f} µs, rms {pi['rms'] * 1e6:.0f} µs"
            self.T(sub, 12, MUTED, (r.x + 16, r.y + 72), maxw=r.w - 30)
            jit = t["jitter"]
            self.T(f"delay {srv['delay'] * 1000:.2f} ms", 14, TEXT, (r.right - 14, r.y + 38), "topright")
            self.T(f"jitter {jit * 1000:.3f} ms" if jit is not None else "jitter …", 14, TEXT,
                   (r.right - 14, r.y + 58), "topright")
        hist = t["history"]
        g = self.pg.Rect(r.x + 14, r.y + 92, r.w - 28, r.h - 122)
        self.pg.draw.rect(c, BG, g, border_radius=6)
        self.pg.draw.line(c, LINE, (g.x, g.centery), (g.right, g.centery))
        if len(hist) > 1:
            offs = [o * 1000 for _, o, _ in hist]
            span = max(0.5, max(abs(o) for o in offs))         # ms, symmetric around 0
            pts = [(g.x + 4 + i * (g.w - 8) / (len(offs) - 1), g.centery - o / span * (g.h / 2 - 6))
                   for i, o in enumerate(offs)]
            self.pg.draw.lines(c, INFO, False, pts, 2)
            self.T(f"scale ±{span:.2f} ms", 11, DIM, (g.right, g.bottom + 3), "topright")
            mins = (hist[-1][0] - hist[0][0]) / 60
            self.T(f"offset history · last {max(1, round(mins))} min", 11, DIM, (g.x, g.bottom + 3))

        # --- network clients
        r = self.pg.Rect(rx, 254, CW - 24 - 296, H - 264)
        self.card(r)
        self.T("Devices following it", 15, MUTED, (r.x + 14, r.y + 10))
        y = r.y + 36
        for cl in t["clients"]:
            st = cl["state"]
            col = {"gps": OK, "other": WARN, "unreachable": DIM, "pending": DIM}[st]
            self.dot((r.x + 20, y + 9), col, 5)
            self.T(cl["label"], 14, TEXT, (r.x + 32, y))
            rr = cl["r"]
            txt = {"gps": f"stratum {rr['stratum']} ✓" if rr else "", "other": f"via {rr['refid']}" if rr else "",
                   "unreachable": "no NTP reply", "pending": "…"}[st]
            self.T(txt, 13, col if st != "gps" else MUTED, (r.right - 14, y + 1), "topright")
            y += 34

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
                    self.hold_start = None
                    if self.active_drag:
                        fn, dx = self.active_drag
                        self.active_drag = None
                        fn(ev.pos[0] - dx, ev.pos[1], "up")
                elif ev.type == pg.MOUSEMOTION and self.active_drag:
                    self.last_input = time.monotonic()
                    fn, dx = self.active_drag
                    fn(ev.pos[0] - dx, ev.pos[1], "move")
                elif ev.type == pg.MOUSEBUTTONDOWN:
                    x, y = ev.pos
                    self.last_input = time.monotonic()
                    if not self.screen_on:
                        self._set_screen(True)
                        continue
                    on_rail = x >= W - RAIL_W if RAIL_SIDE == "right" else x < RAIL_W
                    if on_rail:
                        self.press("short", min(2, y * 3 // H))
                        continue
                    cx = x - (0 if RAIL_SIDE == "right" else RAIL_W)
                    if not (self.dialog or self.help):
                        hit_drag = next((fn for rect, fn in reversed(self.drags) if rect.collidepoint(cx, y)), None)
                        if hit_drag:
                            self.active_drag = (hit_drag, x - cx)
                            hit_drag(cx, y, "down")
                            continue
                    for rect, fn in reversed(self.hits):   # topmost (last drawn) first
                        if rect.collidepoint(cx, y):
                            fn()
                            break
            while not self.events.empty():
                kind, btn = self.events.get()
                self.press(kind, btn)

            now = time.monotonic()
            if now - last_model >= 1.0:
                last_model = now
                self.model = build_model(self.data)
                crit = {a.key for a in self.model["alerts"] if a.sev == 2}
                new = crit - self.seen_crit - self.acked
                self.seen_crit = crit
                self.acked &= {a.key for a in self.model["alerts"]}   # forget cleared alerts
                if new and self.model["ha_age"] is not None:
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
            if self.screen_on:
                self.draw()
            clock.tick(25 if self.dialog or self.hold_start or self.active_drag or self.hunt_active() else 10)


if __name__ == "__main__":
    UI().run()
