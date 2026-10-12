"""Render RackView screens to PNG files off-screen, using live data but without touching hardware.

No I2C, no screen power changes, no Home Assistant service calls, no WLED commands, no web server.

    python3 tools/render_page.py [seconds-to-collect-data] screen [screen ...]

Screens are the ids in screens.json (overview, power, servers, ...). Extras: "servers:2" selects the 3rd item
of the screen's repeater, "cooling:fan-detail" opens that detail screen for the first item, "lights:1" shows
the second tab. Images go to /tmp/<prefix>page_<name>.png (prefix from RENDER_PREFIX).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["RACKVIEW_WINDOWED"] = "1"
os.environ["RACKVIEW_NO_WEB"] = "1"
import rackview as rv  # noqa: E402


def fake_panel(self, events):
    self.pattern = [((0, 0, 0), "steady")] * 3
    self.live = None


rv.Panel.__init__ = fake_panel
rv.UI._set_screen = lambda self, on: setattr(self, "screen_on", on)
rv.Data.call = lambda self, *a, **k: print("blocked HA call", a[:2])
rv.WledLink.send = lambda self, *a, **k: print("blocked WLED send", a)

args = sys.argv[1:]
wait = float(args.pop(0)) if args and args[0].replace(".", "").isdigit() else 8
ui = rv.UI()
time.sleep(wait)
m = rv.build_model(ui.data)
ui.app_model(m)
ui.model = m
ui.refresh_watch()
for a in ui.model["alerts"]:
    print("alert", a.sev, a.page, a.text, "|", a.detail)
prefix = os.environ.get("RENDER_PREFIX", "")
for arg in args or ["overview"]:
    page, _, sub = arg.partition(":")
    ui.goto(page)
    root = ui.custom[page]["root"]
    if sub.isdigit() and root.get("type") == "tabs":
        ui.engine.tab[root["id"]] = int(sub)
    elif sub.isdigit():
        ui.goto(page, int(sub))
    elif sub:
        tg = [t for t in ui.engine.detail_targets(root) if t[0] == sub]
        ui.cdetail = tg[0] if tg else (sub, None)
    ui.draw()
    out = f"/tmp/{prefix}page_{arg.replace(':', '_')}.png"
    ui.pg.image.save(ui.screen, out)
    print("saved", out)
