"""History: one value per metric per minute, plus a timeline of events, kept 30 days in SQLite.

Metrics are data-model paths ("ups.load", "pings.1.ms", ...) or HA entity refs; screens draw them with the
graph widget ({"history": "ups.load", "minutes": 1440}). Events are what happened: alerts raised and cleared,
door, maintenance, power outage, new devices, notes.
"""
import os
import sqlite3
import threading
import time

KEEP_DAYS = 30
# (key, label, unit) recorded every minute; config.json "history_metrics" adds more [key, label, unit]
METRICS = [
    ("ups.batt", "UPS battery", "%"), ("ups.load", "UPS load", "%"), ("ups.runtime", "UPS runtime", "min"),
    ("ups.vin", "Mains voltage", "V"), ("pdu.watts", "Rack power", "W"), ("fw.wan_in", "WAN in", ""),
    ("fw.wan_out", "WAN out", ""), ("inet.ms", "Internet ping", "ms"), ("summary.fan_avg", "Fan avg", "rpm"),
    ("summary.switch_tmax", "Switch max temp", "°F"), ("summary.server_tmax", "Server max temp", "°F"),
    ("time.offset_us", "Pi clock offset", "µs"), ("ha.sys.processor_use", "HA CPU", "%"),
    ("ha.sys.memory_use_percent", "HA memory", "%"), ("summary.alerts", "Active alerts", ""),
    ("summary.clients", "Network clients", ""),
]
SEV_COLOR = {0: "info", 1: "warn", 2: "crit"}


class History:
    def __init__(self, path, extra=None):
        self.path = path
        self.lock = threading.Lock()
        self.metrics = list(METRICS) + [tuple(x) for x in (extra or [])]
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS samples (ts INTEGER, key TEXT, value REAL);
            CREATE INDEX IF NOT EXISTS samples_key_ts ON samples (key, ts);
            CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, sev INTEGER, text TEXT, detail TEXT);
            CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
        """)
        self.last_minute = 0
        self.cache = {}
        self.last_prune = 0

    def record(self, values):
        """values: {key: number}; stored once per minute."""
        minute = int(time.time() // 60 * 60)
        if minute == self.last_minute:
            return
        self.last_minute = minute
        rows = [(minute, k, float(v)) for k, v in values.items() if isinstance(v, (int, float)) and not isinstance(v, bool)]
        with self.lock:
            self.db.executemany("INSERT INTO samples VALUES (?, ?, ?)", rows)
            if time.time() - self.last_prune > 3600:
                self.last_prune = time.time()
                cut = time.time() - KEEP_DAYS * 86400
                self.db.execute("DELETE FROM samples WHERE ts < ?", (cut,))
                self.db.execute("DELETE FROM events WHERE ts < ?", (cut,))
            self.db.commit()

    def series(self, key, minutes=1440, points=120):
        """[(ts, value)] averaged into at most `points` buckets (cached 30 s: screens redraw 10x a second)."""
        ck = (key, int(minutes), int(points))
        hit = self.cache.get(ck)
        if hit and time.time() - hit[0] < 30:
            return hit[1]
        rows = self._series(key, minutes, points)
        self.cache[ck] = (time.time(), rows)
        return rows

    def _series(self, key, minutes, points):
        since = time.time() - minutes * 60
        step = max(60, int(minutes * 60 / points))
        with self.lock:
            rows = self.db.execute("SELECT (ts / ?) * ?, AVG(value) FROM samples WHERE key = ? AND ts >= ? "
                                   "GROUP BY ts / ? ORDER BY 1", (step, step, key, since, step)).fetchall()
        return rows

    def event(self, kind, text, sev=0, detail=""):
        with self.lock:
            self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)", (time.time(), kind, int(sev), text, detail or ""))
            self.db.commit()

    def events(self, limit=50, since=None):
        q, args = "SELECT ts, kind, sev, text, detail FROM events", []
        if since:
            q += " WHERE ts >= ?"
            args.append(since)
        with self.lock:
            rows = self.db.execute(q + " ORDER BY ts DESC LIMIT ?", args + [limit]).fetchall()
        return [dict(ts=ts, kind=k, sev=s, text=t, detail=d) for ts, k, s, t, d in rows]

    def size_mb(self):
        try:
            return os.path.getsize(self.path) / 1e6
        except OSError:
            return 0
