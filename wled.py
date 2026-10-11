"""Direct link to the rack's WLED controller.

- Receive: one WebSocket. WLED pushes its full state on connect and after every change, and while
  `want_live` is set it also streams the current LED colours ("L", version, then RGB per LED).
- Send: a background sender thread posts to /json/state. Rapid updates (dragging the colour wheel
  or a slider) are merged and rate-limited, and the UI thread never waits on the network.
"""
import json
import threading
import time

import requests

try:
    import websocket          # python3-websocket (websocket-client)
except ImportError:           # lets RackView run without it; WLED features just stay empty
    websocket = None

SEND_INTERVAL = 0.12          # max ~8 control updates per second


def log(*a):
    print(time.strftime("%H:%M:%S"), "wled:", *a, flush=True)


def merge(dst, src):
    """Merge a WLED state payload into a pending one; segments merge by id."""
    for k, v in src.items():
        if k == "seg" and isinstance(v, list):
            segs = {sg.get("id"): sg for sg in dst.get("seg", [])}
            for sg in v:
                segs.setdefault(sg.get("id"), {}).update(sg)
            dst["seg"] = list(segs.values())
        else:
            dst[k] = v
    return dst


class WledLink:
    def __init__(self, host):
        self.host = host
        self.lock = threading.Lock()
        self.state = None          # last full state dict pushed by WLED
        self.leds = []             # [(r, g, b), ...] from the live stream
        self.leds_at = 0.0
        self.effects = []
        self.palettes = []
        self.presets = {}          # id(int) -> name
        self.connected = False
        self.want_live = True      # stream LED colours (only needed while the buttons mirror the strip)
        self.sent_ps_at = 0.0      # when RackView itself last switched preset
        self._pending = {}
        self._cv = threading.Condition()
        for target in (self._rx, self._tx, self._load_lists):
            threading.Thread(target=target, daemon=True).start()

    # -- static lists (effects / palettes / presets)
    def _load_lists(self):
        while not (self.effects and self.palettes):
            try:
                base = f"http://{self.host}"
                self.effects = requests.get(base + "/json/eff", timeout=5).json()
                self.palettes = requests.get(base + "/json/pal", timeout=5).json()
                ps = requests.get(base + "/presets.json", timeout=5).json()
                self.presets = {int(k): v.get("n", f"Preset {k}") for k, v in ps.items() if k != "0" and v}
            except Exception:
                time.sleep(30)

    # -- receive (WebSocket)
    def _rx(self):
        if websocket is None:
            log("python3-websocket not installed")
            return
        while True:
            ws = None
            try:
                ws = websocket.create_connection(f"ws://{self.host}/ws", timeout=10)
                ws.settimeout(1)
                with self.lock:
                    self.connected = True
                live = None
                last_rx = last_ping = time.time()
                while True:
                    if live != self.want_live:
                        live = self.want_live
                        ws.send(json.dumps({"lv": live}))
                        if not live:
                            with self.lock:
                                self.leds = []
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        now = time.time()
                        if now - last_ping > 20:          # quiet is normal without the live stream
                            ws.ping()
                            last_ping = now
                        if now - last_rx > 90:
                            raise ConnectionError("no data from WLED for 90 s")
                        continue
                    last_rx = time.time()
                    if isinstance(msg, (bytes, bytearray)):
                        if len(msg) > 2 and msg[0] == ord("L"):
                            off = 2 if msg[1] == 1 else 4      # v2 (2D) adds width/height bytes
                            px = [tuple(msg[i:i + 3]) for i in range(off, len(msg) - 2, 3)]
                            with self.lock:
                                self.leds, self.leds_at = px, time.time()
                    elif msg:
                        d = json.loads(msg)
                        if "state" in d:
                            with self.lock:
                                self.state = d["state"]
            except Exception as e:
                log("socket:", e)
            with self.lock:
                self.connected = False
            try:
                if ws:
                    ws.close()
            except Exception:
                pass
            time.sleep(3)

    # -- send (background, merged, rate-limited)
    def send(self, payload):
        """Queue a state change. Returns immediately; rapid calls are merged."""
        if "ps" in payload:
            self.sent_ps_at = time.time()
        with self._cv:
            merge(self._pending, json.loads(json.dumps(payload)))
            self._cv.notify()
        # optimistic local state so the UI reflects the change before WLED echoes it
        with self.lock:
            if self.state is not None and "ps" not in payload:
                st = json.loads(json.dumps(self.state))
                self.state = merge(st, payload)
        return True

    def _tx(self):
        http = requests.Session()
        while True:
            with self._cv:
                while not self._pending:
                    self._cv.wait()
                payload, self._pending = self._pending, {}
            try:
                r = http.post(f"http://{self.host}/json/state", json=payload, timeout=3)
                r.raise_for_status()
            except Exception as e:
                log("send failed:", e)
            time.sleep(SEND_INTERVAL)

    def snapshot(self):
        with self.lock:
            return self.state, list(self.leds), self.leds_at, self.connected

    def zone_colours(self, zones=3, gain=2.2, cap=190):
        """Average colour of each third of the strip, boosted for the small button LEDs."""
        with self.lock:
            px, at = self.leds, self.leds_at
        if not px or time.time() - at > 2:
            return None
        n = len(px)
        out = []
        for z in range(zones):
            part = px[z * n // zones:(z + 1) * n // zones] or [(0, 0, 0)]
            avg = [sum(p[c] for p in part) / len(part) for c in range(3)]
            peak = max(max(p) for p in part)
            mx = max(avg)
            k = gain
            if 0 < mx < peak / 4:     # sparse effects (ripple, twinkle) average to near-black: boost more
                k = min(gain * 3, cap / mx)
            out.append(tuple(min(cap, int(v * k)) for v in avg))
        return out
