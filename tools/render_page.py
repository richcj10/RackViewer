"""Render RackView pages to PNG files off-screen, using live data but without touching hardware.

No I2C, no screen power changes, no Home Assistant service calls and no WLED commands are made.

    python3 tools/render_page.py [seconds-to-collect-data] page [page ...]

Pages are the names in UI.PAGES (overview, power, servers, network, ports, time, hotspot, ha,
cooling, lights). Extras: "lights:other" for the second Lights tab, "cooling:Fan_ctrl_2" for a fan
controller's detail view. Images are written to /tmp/page_<name>.png.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["RACKVIEW_WINDOWED"] = "1"
import rackview as rv  # noqa: E402


def fake_panel(self, events):
    self.pattern = [((0, 0, 0), "steady")] * 3
    self.live = None


rv.Panel.__init__ = fake_panel
rv.UI._set_screen = lambda self, on: setattr(self, "screen_on", on)
rv.Data.call = lambda self, *a, **k: print("blocked HA call", a[:2])
rv.WledLink.send = lambda self, *a, **k: print("blocked WLED send", a)

wait = float(sys.argv[1]) if len(sys.argv) > 1 else 8
ui = rv.UI()
time.sleep(wait)
ui.model = rv.build_model(ui.data)
for a in ui.model["alerts"]:
    print("alert", a.sev, a.page, a.text, "|", a.detail)
for arg in sys.argv[2:] or ["overview"]:
    page, _, sub = arg.partition(":")
    ui.page = ui.PAGES.index(page)
    if sub and page == "lights":
        ui.light_tab = sub
    if sub and page == "cooling":
        ui._led_page = "cooling"
        ui.fan_detail = sub.replace("_", " ")
    ui.draw()
    out = f"/tmp/page_{arg.replace(':', '_')}.png"
    ui.pg.image.save(ui.screen, out)
    print("saved", out)
