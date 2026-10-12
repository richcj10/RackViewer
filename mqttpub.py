"""Publish RackView's own data to Home Assistant over MQTT (discovery): rack door, pings from the rack,
this Pi's clock offset and sync, screen state and page, Port Hunt, and overall rack health.

Enabled when config.json has "mqtt_user" (an HA user; the Mosquitto add-on accepts HA logins).
"""
import json
import re
import socket
import threading
import time

try:
    import paho.mqtt.client as mqtt
except ImportError:          # python3-paho-mqtt not installed: publishing stays off
    mqtt = None

BASE = "rackview"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


class Publisher:
    def __init__(self, ui, host, port, user, password):
        self.ui = ui
        self.host, self.port, self.user, self.password = host, int(port or 1883), user, password
        self.node = slug(socket.gethostname()) or "rackviewer"
        self.uid = f"rackview_{self.node}"
        self.connected = False
        threading.Thread(target=self._run, daemon=True).start()

    def _client(self):
        try:                                   # paho-mqtt 2.x
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1, client_id=self.uid)
        except AttributeError:                 # paho-mqtt 1.x (Debian bookworm)
            c = mqtt.Client(client_id=self.uid)
        c.username_pw_set(self.user, self.password)
        c.will_set(f"{BASE}/{self.node}/status", "offline", retain=True)
        c.on_connect = lambda cl, u, f, rc: self._on_connect(cl, rc)
        c.on_message = lambda cl, u, msg: self._on_message(msg)
        c.on_disconnect = lambda cl, u, rc: setattr(self, "connected", False)
        return c

    def _on_connect(self, c, rc):
        if rc != 0:
            print(time.strftime("%H:%M:%S"), "mqtt: login refused (rc=%s); check mqtt_user / mqtt_password" % rc, flush=True)
            return
        self.connected = True
        c.publish(f"{BASE}/{self.node}/status", "online", retain=True)
        c.subscribe(f"{BASE}/{self.node}/maintenance/set")
        self.discovery(c)
        print(time.strftime("%H:%M:%S"), "mqtt: connected, publishing rack data", flush=True)

    def _on_message(self, msg):
        """HA flips the maintenance switch."""
        on = msg.payload.decode(errors="replace").strip().upper() in ("ON", "1", "TRUE")
        self.ui.web_q.put(("app", "maint_on" if on else "maint_off"))

    def entities(self):
        ents = [
            ("binary_sensor", "door", "Rack door", "door", {"device_class": "door", "payload_on": "on", "payload_off": "off"}),
            ("binary_sensor", "screen", "Screen", "screen", {"payload_on": "on", "payload_off": "off", "icon": "mdi:monitor"}),
            ("sensor", "page", "Screen page", "page", {"icon": "mdi:page-layout-body"}),
            ("sensor", "health", "Rack health", "health", {"icon": "mdi:shield-check"}),
            ("sensor", "alerts", "Active alerts", "alerts", {"state_class": "measurement", "icon": "mdi:alert"}),
            ("sensor", "pi_offset", "Clock offset", "pi_offset_us", {"unit_of_measurement": "µs", "state_class": "measurement", "icon": "mdi:clock-fast"}),
            ("binary_sensor", "pi_synced", "Clock synced", "pi_synced", {"payload_on": "on", "payload_off": "off", "icon": "mdi:clock-check"}),
            ("binary_sensor", "port_hunt", "Port Hunt blinking", "hunt", {"payload_on": "on", "payload_off": "off", "icon": "mdi:ethernet"}),
            ("sensor", "port_hunt_port", "Port Hunt port", "hunt_port", {"icon": "mdi:ethernet"}),
            ("switch", "maintenance", "Maintenance mode", "maint", {"payload_on": "on", "payload_off": "off", "icon": "mdi:wrench",
                                                                    "command_topic": f"{BASE}/{self.node}/maintenance/set",
                                                                    "state_on": "on", "state_off": "off"}),
            ("binary_sensor", "outage", "Power outage", "outage", {"device_class": "problem", "payload_on": "on",
                                                                   "payload_off": "off", "icon": "mdi:power-plug-off"}),
            ("sensor", "new_devices", "New network devices", "new_devices", {"state_class": "measurement", "icon": "mdi:devices"}),
            ("sensor", "last_event", "Last rack event", "last_event", {"icon": "mdi:timeline-text"}),
        ]
        import rackview as rv
        for label, _host, _crit in rv.PING_HOSTS:
            k = slug(label)
            ents.append(("sensor", f"ping_{k}", f"Ping {label}", f"ping_{k}", {"unit_of_measurement": "ms", "state_class": "measurement", "icon": "mdi:lan-pending"}))
            ents.append(("binary_sensor", f"reach_{k}", f"{label} reachable", f"reach_{k}", {"device_class": "connectivity", "payload_on": "on", "payload_off": "off"}))
        return ents

    def discovery(self, c):
        dev = {"identifiers": [self.uid], "name": "RackViewer", "manufacturer": "RackView", "model": "Rack display"}
        for comp, oid, name, key, extra in self.entities():
            cfg = {"name": name, "unique_id": f"{self.uid}_{oid}", "device": dev, "state_topic": f"{BASE}/{self.node}/state",
                   "value_template": "{{ value_json.%s }}" % key, "availability_topic": f"{BASE}/{self.node}/status"}
            cfg.update(extra)
            c.publish(f"homeassistant/{comp}/{self.uid}/{oid}/config", json.dumps(cfg), retain=True)

    def state(self):
        import rackview as rv
        ui, m = self.ui, self.ui.model or {}
        tm = m.get("time") or {}
        srv, pi = tm.get("server"), tm.get("pi") or {}
        st = {"door": "on" if ui.door == "open" else "off", "screen": "on" if ui.screen_on else "off",
              "page": ui.TITLES.get(ui.PAGES[ui.page], ui.PAGES[ui.page]) if ui.PAGES else None,
              "health": {0: "ok", 1: "warning", 2: "critical"}.get(m.get("overall"), "unknown"),
              "alerts": len(m.get("alerts") or []),
              "pi_offset_us": round(srv["offset"] * 1e6, 1) if srv and tm.get("fails", 9) < 2 else None,
              "pi_synced": "on" if pi.get("synced") and pi.get("server") == rv.TIME_SERVER else "off",
              "hunt": "on" if ui.hunt_active() else "off", "hunt_port": ui.hunt_port if ui.hunt_active() else None,
              "maint": "on" if ui.maint else "off", "outage": "on" if ui.outage else "off",
              "new_devices": len(m.get("devices_new") or []),
              "last_event": ((m.get("events") or [{}])[0].get("text") or "")[:250]}
        for p in m.get("pings") or []:
            k = slug(p["label"])
            st[f"ping_{k}"] = round(p["ms"], 1) if p.get("ms") is not None else None
            st[f"reach_{k}"] = "off" if p.get("fails", 0) >= 2 else "on"
        return st

    def _run(self):
        if mqtt is None:
            print("mqtt: python3-paho-mqtt not installed; not publishing", flush=True)
            return
        c = self._client()
        while True:
            try:
                c.connect(self.host, self.port, 60)
                c.loop_start()
                last = None
                while True:
                    if self.connected and self.ui.model:
                        st = json.dumps(self.state(), default=str)
                        if st != last:
                            c.publish(f"{BASE}/{self.node}/state", st, retain=True)
                            last = st
                    time.sleep(5)
            except Exception as e:
                print(time.strftime("%H:%M:%S"), "mqtt:", repr(e), flush=True)
                try:
                    c.loop_stop()
                except Exception:
                    pass
                time.sleep(30)


def start(ui, cfg):
    if not cfg.get("mqtt_user"):
        return None
    host = cfg.get("mqtt_host") or re.sub(r"^https?://([^:/]+).*$", r"\1", cfg.get("ha_url", "homeassistant.local"))
    return Publisher(ui, host, cfg.get("mqtt_port", 1883), cfg["mqtt_user"], cfg.get("mqtt_password", ""))
