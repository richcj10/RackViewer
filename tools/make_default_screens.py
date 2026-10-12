"""Writes layouts/default_screens.json: RackView's factory screens as layout data.

Every page is a tree of placed widgets (positions in px inside the 658 x 420 content area), the values they
show (${...} refs into the data model or HA) and their colours. The app draws whatever screens.json says;
this file only produces the starting point. Run:  python3 tools/make_default_screens.py
"""
import json
import os

SEV = {"0": "ok", "1": "warn", "2": "crit"}
_n = [0]
_prefix = ["x"]


def N(t, props=None, children=None):
    _n[0] += 1
    node = {"id": f"{_prefix[0]}{_n[0]}", "type": t, "props": {k: v for k, v in (props or {}).items() if v is not None}}
    if children is not None:
        node["children"] = children
    return node


def pos(x=None, y=None, w=None, h=None, r=None, b=None):
    return dict(x=x, y=y, w=w, h=h, r=r, b=b)


def show(ref, op="not_empty", value=None):
    return {"ref": ref, "op": op, "value": value} if value is not None else {"ref": ref, "op": op}


def free(x, y, w, h, kids, bg=None, **p):
    return N("free", dict(pos(x, y, w, h), bg=bg, **p), kids)


def L(text, x=0, y=0, size=15, color=None, bold=None, align=None, valign=None, w=None, h=None, r=None, **p):
    return N("label", dict(pos(x, y, w, h, r), text=text, size=size, color=color, bold=bold, align=align, valign=valign, **p))


def KV(label, text, x=14, y=0, w=None, r=None, size=16, color=None, **p):
    return N("kv", dict(pos(x, y, w, None, r), label=label, text=text, size=size, color=color, **p))


def BAR(value, x, y, w=None, h=8, r=None, max=100, min=None, color=None, **p):
    return N("bar", dict(pos(x, y, w, h, r), value=value, max=max, min=min, color=color, **p))


def DOT(color, cx, cy, rad):
    return N("dot", dict(pos(cx - rad, cy - rad, 2 * rad, 2 * rad), color=color, radius=rad))


def BOX(x, y, w, h, color, r=None, **p):
    return N("box", dict(pos(x, y, w, h, r), color=color, **p))


def REP(path, x, y, w, h, kids, r=None, **p):
    return N("repeater", dict(pos(x, y, w, h, r), source="model", path=path, empty="", **p), kids)


def COL(x, y, w, h, kids, r=None, gap=0, **p):
    return N("column", dict(pos(x, y, w, h, r), gap=gap, **p), kids)


def card(x, y, w, h, kids, **p):
    return free(x, y, w, h, kids, bg="card", **p)


def sevmap(ref, default="ok"):
    return {"ref": ref, "map": SEV, "default": default}


def screen(sid, title, color, root, middle="home", details=None, **extra):
    return dict({"id": sid, "title": title, "color": color, "enabled": True, "middle": middle, "root": root,
                 "details": details or []}, **extra)


def start(prefix):
    _prefix[0], _n[0] = prefix, 0


# ============================================================ Overview
start("ov")
overview = screen("overview", "Overview", "#dce1eb", free(0, 0, 658, 420, [
    N("banner", pos(0, 0, 658, 54)),
    N("tile", dict(pos(0, 62, 158, 92), label="UPS · ${$ups.status|or:?}", value="${$ups.batt|u:%}",
                   sub="${$ups.load|d:0|u:% load} · ${$ups.runtime|d:0|u: min}", stripe=sevmap("$ups.sev"), goto="power")),
    N("tile", dict(pos(166, 62, 158, 92), label="Rack power (PDU)", value="${$pdu.watts|d:0|u: W}",
                   sub="of ${$pdu.budget|d:0|u: W} budget", stripe=sevmap("$pdu.sev"), goto="power")),
    N("tile", dict(pos(332, 62, 158, 92), label="WAN · ${$fw.wan_gw|or:?}", value="${$inet.ms|d:0|u: ms|or:DOWN}",
                   sub="↓${$fw.wan_in|d:1} ↑${$fw.wan_out|d:1} ${$fw.rate_unit}", stripe=sevmap("$page_sev.network"), goto="network")),
    N("tile", dict(pos(498, 62, 160, 92), label="Rack fans · avg rpm", value="${$summary.fan_avg|d:0}",
                   sub="switch max ${$summary.switch_tmax|d:0|u:°F}", stripe=sevmap("$page_sev.cooling"), goto="cooling")),
    REP("$servers", 0, 162, 658, 96, [
        free(0, 0, None, 96, [
            DOT("@color", 18, 22, 7),
            L("${@name}", 32, 10, 19, bold=True, r=8),
            L("${@state_line}", 14, 42, 15, color=[{"ref": "@sev", "op": ">", "value": 0, "color": "@color"}, {"color": "muted"}], r=6),
            L("${@watts|d:0|u: W}${@hot_text|or:}", 14, 68, 13, color="muted", r=6),
        ], bg="card", goto="servers"),
    ], mode="all"),
    N("alerts", dict(pos(0, 268, 658, 152), max=10)),
]), middle="silence")

# ============================================================ Power
start("pw")
kv_ups = [("Input", "${$ups.vin|d:1|u: V} ${$ups.freq|d:0|u: Hz}"), ("Output", "${$ups.vout|d:1|u: V}"),
          ("Battery", "${$ups.bvolt|d:1|u: V}"), ("Battery date", "${$ups.bdate|or:—}"),
          ("UPS temp", "${$ups.temp|d:1|u:°F}"), ("Self-test", "${$ups.selftest|or:—}")]
power = screen("power", "Power", "#b078ff", free(0, 0, 658, 420, [
    card(0, 0, 250, 420, [
        L("UPS", 14, 10, 15, "muted"),
        L("${$ups.status|or:no data}", 14, 10, 15, "$ups.color", bold=True, align="right", r=14),
        L("${$ups.batt|d:0|u:%}", 14, 32, 48, bold=True, r=14),
        BAR("$ups.batt", 14, 92, 222, 10, color="$ups.color"),
        L("${$ups.runtime|d:0|u: min runtime}", 14, 110, 16, r=14),
        L("Load", 14, 140, 14, "muted"),
        L("${$ups.load|d:0|u:%}", 14, 140, 14, align="right", r=14),
        BAR("$ups.load", 14, 160, 222, 8, color="$ups.load_color"),
        *[KV(lab, txt, 14, 182 + 25 * i, r=14) for i, (lab, txt) in enumerate(kv_ups)],
        L("Last transfer: ${$ups.reason|or:—}", 14, 334, 12, "muted", r=14),
        N("ups_test", pos(14, 368, 222, 40)),
    ]),
    card(258, 0, 400, 420, [
        L("PDU outlets", 14, 10, 15, "muted"),
        L("${$pdu.watts|d:0|u: W} / ${$pdu.budget|d:0|u: W}", 14, 10, 15, "$pdu.color", bold=True, align="right", r=14),
        L("No PDU data", 14, 50, 16, "muted", show_if=show("$pdu.outlets", "is_empty")),
        REP("$pdu.outlets", 14, 40, 372, 376, [
            free(0, 0, None, 26, [
                L("${@n|pad:2}", 0, 2, 13, "@num_color"),
                BAR("@watts", 26, 7, 86, 7, max="$pdu.top", color="@bar_color"),
                L("${@text}", 0, 2, 13, "@color", align="right"),
            ]),
        ], mode="all", columns=2, flow="col", row_h=26, fit_rows=True, gap_x=16, gap_y=0),
    ]),
]))

# ============================================================ Servers
start("sv")
btn = lambda label, color, suffix, confirm, title, text, cond, **p: N("button", dict(
    label=label, color=color, service="button.press", target="button.idrac_{id}_" + suffix, confirm=confirm,
    confirm_title=title, confirm_text=text, exact=True, show_if=show(cond, "==", True), **p))
soft_text = "Asks {item}'s OS to shut down cleanly\n(ACPI power button). Unsaved work is lost."
servers = screen("servers", "Servers", "#3cd2e6", free(0, 0, 658, 420, [
    L("No iDRAC servers found", 0, 0, 20, "muted", align="center", valign="middle", w=658, h=420,
      show_if=show("$servers", "is_empty")),
    REP("$servers", 0, 0, 658, 420, [
        free(0, 0, 658, 420, [
            card(0, 0, 236, 420, [
                DOT("@color", 22, 28, 10),
                L("${@name}", 42, 12, 26, bold=True, r=10),
                L("${@power|map:on=POWERED ON;off=POWERED OFF;*=NO DATA}", 14, 54, 15, bold=True,
                  color={"ref": "@on", "map": {"true": "ok"}, "default": "muted"}),
                KV("Health", "${@health|or:—}", 14, 84, r=14, color="@health_color"),
                KV("Power", "${@watts|d:0|u: W}", 14, 109, r=14),
                KV("PSU in", "${@psu_v|d:0|u: V} ${@psu_a|d:1|u: A}", 14, 134, r=14),
                KV("OS", "${@os|map:on=running;off=not running;*=—}", 14, 159, r=14),
                KV("iDRAC", "${@avail|map:on=reachable;off=unreachable;*=—}", 14, 184, r=14,
                   color={"ref": "@avail", "map": {"off": "crit"}, "default": "text"}),
                KV("Fan redund.", "${@redund|or:—}", 14, 209, r=14),
                COL(14, 236, 208, 122, [
                    L("Why ${@health}", size=13, color="muted", h=20, mt=2, show_if=show("@why", "==", True)),
                    L("${@reason_text}", size=13, color="@reason_color", wrap=4, show_if=show("@why", "==", True)),
                    L("Last error", size=13, color="muted", h=22, mt=6, show_if=show("@has_error", "==", True)),
                    L("${@last_error}", size=13, color="warn", wrap=6, show_if=show("@has_error", "==", True)),
                ]),
                N("row", dict(pos(14, 366, 208, 40), gap=8), [
                    btn("Power On", "ok", "power_on", "@confirm", "Power On: {item}?", "Sends IPMI power-on to {item}.", "@can.on"),
                    btn("Shut down", "warn", "soft_shutdown", "hold", "Soft Shutdown: {item}?", soft_text, "@soft_norm", danger=True),
                    btn("Shut down", "warn", "soft_shutdown", "hold", "Soft Shutdown: {item}?",
                        soft_text + "\n\n{item} is ESSENTIAL: the network may go down.", "@soft_ess", danger=True),
                    btn("Clear SEL", "info", "clear_sel", "@confirm", "Clear SEL: {item}?",
                        "Clears {item}'s iDRAC System Event Log.\nHealth may return to OK if old events caused it.", "@can.sel"),
                ]),
            ]),
            card(244, 0, 414, 420, [
                L("Temperatures", 14, 10, 15, "muted"),
                COL(14, 36, 386, 374, [
                    L("${@no_temps}", size=15, color="dim", h=30, show_if=show("@temps", "is_empty")),
                    N("repeater", dict(source="model", path="@temps", mode="all", columns=1, row_h=23, gap_y=0,
                                       max="@temps_max", empty=""), [
                        free(0, 0, None, 23, [
                            L("${@name}", 0, 0, 14, r=250),
                            BAR("@f", 136, 6, 164, 7, min=60, max=200, color="@color"),
                            L("${@value|d:0}${@unit}", 0, 0, 14, align="right"),
                        ]),
                    ]),
                    L("Fans", size=15, color="muted", h=34, mt=8, show_if=show("@fans")),
                    N("repeater", dict(source="model", path="@fans", mode="all", columns=1, row_h=23, gap_y=0, empty=""), [
                        free(0, 0, None, 23, [
                            L("Fan ${@n}", 0, 0, 14, color={"ref": "@rpm", "map": {"": "dim", "0": "dim"}, "default": "text"}),
                            BAR("@rpm", 136, 6, 164, 7, max=15000, color="info"),
                            L("${@rpm|d:0|u: rpm}", 0, 0, 14, align="right",
                              color={"ref": "@rpm", "map": {"": "dim", "0": "dim"}, "default": "text"}),
                        ]),
                    ]),
                ]),
            ]),
        ]),
    ], header=True),
]), middle="next", middle_label="Next server")

# ============================================================ Network
start("nw")
network = screen("network", "Network", "#5a8cff", free(0, 0, 658, 420, [
    card(0, 0, 330, 248, [
        L("Firewall", 14, 10, 15, "muted"),
        REP("$fw.links", 0, 36, 330, 96, [free(0, 0, None, 24, [
            DOT("@color", 20, 9, 5), L("${@name}", 32, 0, 14), L("${@value}", 0, 0, 14, "@color", align="right", r=14)])],
            mode="all", columns=1, row_h=24, gap_y=0),
        KV("WAN ↓ / ↑", "${$fw.wan_in|d:2} / ${$fw.wan_out|d:2} ${$fw.rate_unit}", 14, 138, r=14, size=15),
        KV("LAN ↓ / ↑", "${$fw.lan_in|d:1} / ${$fw.lan_out|d:1} ${$fw.rate_unit}", 14, 162, r=14, size=15),
        KV("CPU · mem · temp", "${$fw.cpu|d:0|u:%} · ${$fw.mem|d:0|u:%} · ${$fw.temp|d:0|u:$fw.temp_u}", 14, 186, r=14, size=15),
    ]),
    card(338, 0, 320, 248, [
        L("Ping from rack", 14, 10, 15, "muted"),
        REP("$pings", 0, 36, 320, 212, [free(0, 0, None, 36, [
            DOT("@color", 20, 9, 5), L("${@label}", 32, 0, 14),
            L("${@ms|d:1|u: ms|or:@none_text}", 0, 0, 14, "@vcolor", align="right", r=14),
            L("${@host}", 32, 17, 11, "dim")])], mode="all", columns=1, row_h=36, gap_y=0),
    ]),
    REP("$switches", 0, 256, 658, 154, [free(0, 0, None, 154, [
        DOT("@color", 18, 20, 6), L("${@name}", 32, 10, 16, bold=True, r=80),
        L("${@state|or:—}", 0, 12, 13, "@color", align="right", r=14),
        KV("Temp", "${@temp|d:1|u:@temp_u}", 14, 40, r=14, size=15),
        KV("CPU / mem", "${@cpu|d:0|u:%} / ${@mem|d:0|u:%}", 14, 64, r=14, size=15),
        KV("Etherlight", "${@etherlight}", 14, 88, r=14, size=15, show_if=show("@etherlight"))], bg="card")], mode="all"),
    L("No switch data", 14, 266, 15, "muted", show_if=show("$switches", "is_empty")),
]), middle="reping")

# ============================================================ Time
start("tm")
gps = lambda lab, txt, color=None, cond=None: KV(lab, txt, size=15, color=color, show_if=cond)
time_ = screen("time", "Time", "#ff6ebe", free(0, 0, 658, 420, [
    card(0, 0, 288, 420, [
        L("GPS time server", 14, 10, 15, "muted"),
        L("${$time.source_text}", 0, 12, 13, "dim", align="right", r=14),
        N("pill", dict(pos(14, 36, 260, 46), text="${$time.state_text}", color="$time.state_color", size=22)),
        COL(14, 92, 260, 262, [
            gps("GPS receiver", "${$time.gps.receiver}", "$time.gps.receiver_color", show("$time.gps")),
            gps("Receiver mode", "${$time.gps.mode}", None, show("$time.gps")),
            gps("Timing", "${$time.gps.timing}", "$time.gps.timing_color", show("$time.gps")),
            gps("Holdover", "${$time.gps.holdover}", "$time.gps.holdover_color", show("$time.gps")),
            gps("Satellites", "${$time.gps.sats}", "$time.gps.sats_color", show("$time.gps")),
            gps("GPS − UTC", "${$time.gps.utc}", None, show("$time.gps")),
            gps("Oscillator", "${$time.gps.osc}", None, show("$time.gps")),
            gps("Stratum", "${$time.srv.stratum}", None, show("$time.srv")),
            gps("Leap second", "${$time.srv.leap}", "$time.srv.leap_color", show("$time.srv")),
            gps("Precision · disp.", "${$time.srv.precision}", None, show("$time.srv")),
            gps("Last GPS update", "${$time.srv.last_update}", "$time.srv.last_update_color", show("$time.direct", "==", True)),
            L("Waiting for first reply…", size=15, color="dim", show_if=show("$time.server", "is_empty")),
        ]),
        L("${$time.gps.fw_line}", 14, 362, 11, "dim", show_if=show("$time.gps")),
        L("LAN ports", 14, 380, 14, "muted", show_if=show("$time.lan_ports")),
        REP("$time.lan_ports", 14, 376, 260, 26, [free(0, 0, 44, 26, [
            BOX(0, 0, 44, 26, "@color", tint=0.35, radius=6),
            L("${@text}", 0, 0, 12, "@tcolor", bold=True, align="center", valign="middle", w=44, h=26)])],
            mode="all", columns=8, row_h=26, cell_w=44, gap_x=8, align="right"),
    ]),
    card(296, 0, 362, 196, [
        L("This Pi vs GPS clock", 14, 10, 15, "muted"),
        L("${$time.pi_text}", 0, 12, 13, "$time.pi_color", bold=True, align="right", r=14),
        L("${$time.offset_text}", 14, 34, 30, bold=True, w=200, show_if=show("$time.measured", "==", True)),
        L("${$time.offset_sub}", 16, 72, 12, "muted", r=14, show_if=show("$time.measured", "==", True)),
        L("${$time.delay_text}", 0, 38, 14, align="right", r=14, w=150, show_if=show("$time.measured", "==", True)),
        L("${$time.jitter_text}", 0, 58, 14, align="right", r=14, w=150, show_if=show("$time.measured", "==", True)),
        N("graph", dict(pos(14, 92, 334, 74), source="$time.history", field=1, scale=1000, symmetric=True, min_span=0.5,
                        caption="ms", color="info")),
        L("${$time.history_text}", 14, 169, 11, "dim", w=200),
    ]),
    card(296, 204, 362, 216, [
        L("Devices following it", 14, 10, 15, "muted"),
        REP("$time.clients", 0, 36, 362, 180, [free(0, 0, None, 34, [
            DOT("@color", 20, 9, 5), L("${@label}", 32, 0, 14, r=150),
            L("${@text}", 0, 1, 13, "@tcolor", align="right", r=14)])], mode="all", columns=1, row_h=34, gap_y=0),
    ]),
]))

# ============================================================ Hotspot
start("hs")
on_air = show("$hotspot.view.on_air", "==", True)
hotspot = screen("hotspot", "Hotspot", "#ff963c", free(0, 0, 658, 420, [
    L("No hotspot data in HA", 0, 166, 20, "muted", align="center", w=658, show_if=show("$hotspot.present", "==", False)),
    L("Install / start the WPSD Hotspot Monitor add-on", 0, 194, 14, "dim", align="center", w=658,
      show_if=show("$hotspot.present", "==", False)),
    free(0, 0, 658, 420, [
        BOX(0, 0, 658, 124, "orange", tint=0.32, radius=12, pulse="$hotspot.view.on_air"),
        BOX(0, 0, 658, 124, "none", border="orange", bw=3, radius=12, show_if=on_air),
        L("${$hotspot.view.call}", 18, 8, 46, bold=True, w=300),
        L("${$hotspot.view.name}", 20, 64, 20, bold=True, w=300),
        L("${$hotspot.view.location}", 20, 92, 14, "sand", w=300),
        L("${$hotspot.view.tag}", 0, 10, 12, "$hotspot.view.tag_color", bold=True, align="right", r=14, w=200, h=22,
          chip="$hotspot.view.tag_bg", show_if=show("$hotspot.view.tag")),
        L("${$hotspot.view.tg}", 340, 34, 30, bold=True, r=14),
        L("${$hotspot.view.tg_name}", 340, 70, 15, "sand", r=14),
        L("${$hotspot.view.meta}", 340, 96, 13, "sand", r=14),
        card(0, 132, 330, 288, [
            L("Talkgroups · BrandMeister", 14, 10, 15, "muted"),
            L("${$hotspot.view.net}", 0, 12, 13, "$hotspot.view.net_color", bold=True, align="right", r=14, w=80),
            COL(10, 36, 310, 222, [
                L("${$hotspot.view.tg_empty}", size=14, color="dim", h=30, ml=4, mt=4, show_if=show("$hotspot.view.tgs", "is_empty")),
                N("repeater", dict(source="model", path="$hotspot.view.tgs", max=4, mode="all", columns=1, row_h=58,
                                   gap_y=0, empty=""), [free(0, 0, None, 58, [
                    BOX(0, 0, 310, 52, "card2", radius=8),
                    BOX(0, 8, 4, 36, "@color", radius=2),
                    L("TG ${@tg}", 14, 5, 17, bold=True, w=180),
                    L("${@name}", 14, 26, 13, "muted", r=96),
                    L("${@label}", 0, 6, 12, "@color", bold=True, align="right", r=10, w=120),
                    L("${@left_text}", 0, 26, 12, "muted", align="right", r=10, w=90, show_if=show("@expires", "==", True)),
                    BAR("@left", 14, 46, 286, 3, max=900, color="@color", show_if=show("@expires", "==", True)),
                ])]),
                KV("Auto-static", "${$hotspot.view.autostatic}", size=14, ml=4, mr=4, mt=4, h=27),
                KV("Master", "${$hotspot.view.master}", size=14, ml=4, mr=4),
                KV("Blocked", "${$hotspot.view.blocked}", size=14, color="warn", ml=4, mr=4,
                   show_if=show("$hotspot.view.blocked")),
            ]),
            L("${$hotspot.view.footer}", 14, 266, 12, "dim", r=14),
        ]),
        card(338, 132, 320, 288, [
            L("Last heard", 14, 10, 15, "muted"),
            L("${$hotspot.view.host}", 0, 12, 12, "dim", align="right", r=14, w=150),
            L("Nothing heard yet", 14, 36, 14, "dim", show_if=show("$hotspot.view.heard", "is_empty")),
            REP("$hotspot.view.heard", 0, 33, 320, 252, [free(0, 0, None, 42, [
                BOX(8, 0, 304, 40, "orange", tint=0.3, radius=7, show_if=show("@tx", "==", True)),
                N("inline", dict(pos(14, 3, None, 20, 80), gap=6), [
                    L("${@call}", size=15, color="@color", bold=True),
                    L("${@src}", size=10, color="@src_color", bold="@rf", mt=4),
                ]),
                L("${@time}", 0, 5, 11, "muted", align="right", r=14, w=80),
                L("${@tg}", 14, 21, 12, "muted", r=76),
                L("${@dur}", 0, 21, 12, "@dur_color", bold="@tx", align="right", r=14, w=60),
            ])], max=6, mode="all", columns=1, row_h=42, gap_y=0),
        ]),
    ], show_if=show("$hotspot.present", "==", True)),
]))

# ============================================================ Home Assistant
start("ha")
meter = lambda lab, txt, val, col, y: [L(lab, 14, y, 14, "muted"), L(txt, 0, y, 14, align="right", r=14, w=250),
                                       BAR(val, 14, y + 22, 322, 8, color=col)]
ha = screen("ha", "Home Assistant", "#41bdf5", free(0, 0, 658, 420, [
    card(0, 0, 300, 420, [
        L("Home Assistant", 14, 10, 15, "muted"),
        N("pill", dict(pos(14, 36, 272, 46), text="${$ha.view.state}", color="$ha.view.color")),
        L("${$ha.view.latency}", 14, 36, 13, "muted", align="right", valign="middle", h=46, r=26),
        *[KV(lab, txt, 14, 94 + 24 * i, r=14, size=15, color=col) for i, (lab, txt, col) in enumerate([
            ("Core", "${$ha.view.core}", "$ha.view.core_color"), ("Supervisor", "${$ha.view.supervisor}", None),
            ("OS", "${$ha.view.os}", None), ("Entities", "${$ha.view.entities}", None),
            ("Unavailable", "${$ha.view.unavailable}", "$ha.view.unavailable_color"),
            ("Notifications", "${$ha.view.notifications}", "$ha.view.notifications_color"),
            ("Zigbee2MQTT", "${$ha.view.z2m}", "$ha.view.z2m_color")])],
        L("Add-ons reporting", 14, 268, 13, "muted"),
        REP("$ha.view.addons", 0, 290, 300, 120, [free(0, 0, None, 24, [
            DOT("@color", 20, 8, 5), L("${@name}", 32, 0, 14, r=80),
            L("${@text}", 0, 1, 13, "@color", align="right", r=14, w=70)])], mode="all", columns=1, row_h=24, gap_y=0),
    ]),
    card(308, 0, 350, 196, [
        L("Host", 14, 10, 15, "muted"),
        *meter("CPU", "${$ha.view.cpu_text}", "$ha.view.cpu", "$ha.view.cpu_color", 38),
        *meter("Memory", "${$ha.view.mem_text}", "$ha.view.mem", "$ha.view.mem_color", 76),
        *meter("Disk", "${$ha.view.disk_text}", "$ha.view.disk", "$ha.view.disk_color", 114),
        L("CPU temp", 14, 152, 14, "muted"),
        L("${$ha.view.cpu_temp}", 0, 152, 14, align="right", r=14, w=150),
    ]),
    card(308, 204, 350, 216, [
        L("Backups & updates", 14, 10, 15, "muted"),
        KV("Last backup", "${$ha.view.backup_last}", 14, 36, r=14, size=15, color="$ha.view.backup_color"),
        KV("Next backup", "${$ha.view.backup_next}", 14, 60, r=14, size=15),
        L("${$ha.view.updates_title}", 14, 88, 14, "warn", bold=True, show_if=show("$ha.view.updates_title")),
        REP("$ha.view.updates", 14, 110, 322, 60, [L("• ${@name}", size=13, h=20)], max=3, mode="all", columns=1,
            row_h=20, gap_y=0),
        L("${$ha.view.updates_more}", 14, 170, 12, "muted", show_if=show("$ha.view.updates_more")),
        L("${$ha.view.all_ok}", 14, 88, 14, "ok", show_if=show("$ha.view.updates_title", "is_empty")),
    ]),
]))

# ============================================================ Cooling (+ detail per controller)
start("cl")
fan_row = free(0, 0, None, 30, [
    L("Fan ${@n}", 0, 0, 14, "@label_color", w=60),
    BAR("@frac", 56, 6, None, 7, r=116, max=1, color="@color", show_if=show("@fit", "==", True)),
    L("${@rpm|d:0} rpm", 0, 0, 14, align="right", r=40, w=110, show_if=show("@live", "==", True)),
    L("${@pwm|d:0|u:%}", 0, 1, 13, "muted", align="right", w=40, r=0, show_if=show("@live", "==", True)),
    L("off", 0, 0, 14, "muted", bold=True, align="right", w=40, r=0, show_if=show("@off_shown", "==", True)),
    L("—", 0, 0, 14, "dim", align="right", w=40, r=0, show_if=show("@fit", "==", False)),
])
all_btn = lambda on: N("button", dict(
    pos(12, 210, None, 40, 12), exact=True, size=15,
    label="ALL FANS " + ("ON" if on else "OFF") + "  ·  ${@running}/${@fitted_n} running",
    color="ok" if on else "muted", filled=on, service="fan.turn_off" if on else "fan.turn_on", target="@ctl",
    confirm="confirm" if on else None, confirm_title="Turn off all {item} fans?" if on else None,
    confirm_text="The fans stop cooling the rack until\nyou turn them back on." if on else None, danger=on or None,
    show_if=show("@ctl_mode", "==", "on" if on else "off")))
cooling_detail = {"id": "fan-detail", "title": "", "root": card(0, 0, 658, 420, [
    N("button", dict(pos(12, 10, 90, 32), label="‹ Back", action="back", color="muted", size=14, exact=True)),
    DOT("@color", 122, 26, 6),
    L("${@name}", 136, 14, 18, bold=True, w=300),
    L("${@esp_text}", 0, 16, 14, "@esp_color", align="right", r=14, w=150),
    REP("@fans", 10, 60, 638, 360, [free(0, 0, None, 72, [
        BOX(0, 0, 638, 64, "@row_color", radius=10),
        L("Fan ${@n}", 16, 10, 18, "@label_color", bold=True, w=100),
        L("${@nc_text}", 16, 36, 14, "dim", w=200, show_if=show("@fit", "==", False)),
        L("${@state_text}", 16, 38, 13, "@state_color", bold=True, w=100, show_if=show("@fit", "==", True)),
        BAR("@frac_on", 120, 18, None, 9, r=210, max=1, color="@color", show_if=show("@fit", "==", True)),
        L("${@rpm_text}", 0, 10, 17, "@rpm_color", bold=True, align="right", r=110, w=110, show_if=show("@fit", "==", True)),
        L("PWM ${@pwm|d:0|u:%}", 0, 36, 13, "muted", align="right", r=110, w=110, show_if=show("@on", "==", True)),
        N("button", dict(pos(None, 12, 84, 40, 12), label="ON", color="ok", filled=True, size=16, exact=True,
                         service="fan.turn_off", target="@entity", confirm="confirm", confirm_title="Turn off {item}?",
                         confirm_text="The fan stops cooling the rack until\nyou turn it back on.", danger=True,
                         show_if=show("@toggle_on", "==", True))),
        N("button", dict(pos(None, 12, 84, 40, 12), label="OFF", color="muted", size=16, exact=True,
                         service="fan.turn_on", target="@entity", show_if=show("@toggle_off", "==", True))),
    ])], mode="all", columns=1, row_h=72, gap_y=0),
])}
cooling = screen("cooling", "Cooling", "#96c8ff", free(0, 0, 658, 420, [
    REP("$fanctl", 0, 0, 658, 262, [free(0, 0, None, 262, [
        DOT("@color", 18, 20, 6),
        L("${@name} ›", 32, 10, 16, bold=True, w=150, link="fan-detail"),
        L("${@esp_text}", 0, 12, 13, "@esp_color", align="right", r=14, w=120),
        REP("@fans", 14, 40, None, 160, [fan_row], r=14, mode="all", columns=1, row_h=30, gap_y=0),
        BOX(12, 210, None, 40, "card2", r=12, show_if=show("@ctl_mode", "==", "offline")),
        BOX(12, 210, None, 40, "card2", r=12, show_if=show("@ctl_mode", "==", "none")),
        L("${@ctl_text}", 12, 210, 14, "dim", align="center", valign="middle", h=40, r=12,
          show_if=show("@ctl", "is_empty")),
        all_btn(True), all_btn(False),
    ], bg="card")], mode="all"),
    card(0, 270, 658, 150, [
        L("Equipment temperatures", 14, 10, 15, "muted"),
        REP("$equip_temps", 14, 36, 630, 104, [free(0, 0, None, 26, [
            DOT("@color", 6, 9, 5), L("${@name}", 18, 0, 14, r=90),
            L("${@value|d:1|u:@unit}", 0, 0, 14, align="right")])], max=8, mode="all", columns=2, row_h=26,
            gap_x=20, gap_y=0),
    ]),
]), middle="details", details=[cooling_detail])

# ============================================================ Port Hunt
start("ph")
present = show("$portmap.present", "==", True)
ports = screen("ports", "Port Hunt", "#00d2b4", free(0, 0, 658, 420, [
    L("No port map in HA yet", 0, 166, 20, "muted", align="center", w=658, show_if=show("$portmap.present", "==", False)),
    L("Install / configure the UniFi Port Map add-on", 0, 194, 14, "dim", align="center", w=658,
      show_if=show("$portmap.present", "==", False)),
    N("hunt_list", dict(pos(0, 0, 262, 420), show_if=present)),
    N("hunt_faceplate", dict(pos(270, 0, 388, 150), show_if=present)),
    N("hunt_client", dict(pos(270, 158, 388, 262), show_if=present)),
]), middle="hunt")

# ============================================================ Lights
start("lt")
lights = screen("lights", "Lights", "#ffffff", N("tabs", dict(bar_h=38, tab_w=160, tab_h=30), [
    N("tab", dict(title="Rack LEDs"), [N("wled_panel", dict(grow=True))]),
    N("tab", dict(title="Switch & AP lights"), [card(0, 0, 658, 382, [
        REP("$lights.list", 0, 20, 658, 350, [free(0, 0, None, 70, [
            L("${@name}", 16, 6, 17, "@name_color", bold=True, w=150),
            N("slider", dict(pos(170, 0, None, 36, 148), entity="@id", attr="brightness", bare=True,
                             show_if=show("@dimmable", "==", True))),
            N("toggle", dict(pos(None, 0, 84, 36, 16), entity="@id", size=14)),
        ])], mode="all", columns=1, row_h=70, gap_y=0),
    ])]),
]), middle="wled_power")


# ============================================================ Power outage (only shown while the UPS is on battery)
start("po")
outage = screen("outage", "Power Outage", "#f85149", free(0, 0, 658, 420, [
    BOX(0, 0, 658, 96, "crit", tint=0.3, radius=12, pulse="$outage.low"),
    BOX(0, 0, 658, 96, "none", border="crit", bw=2, radius=12),
    L("ON BATTERY", 18, 10, 30, "crit", bold=True, w=330),
    L("Mains lost at ${$outage.since_text} · ${$outage.minutes|d:0} min ago", 20, 50, 15, w=360),
    L("UPS ${$ups.status|or:?}", 20, 72, 12, "sand", w=360),
    L("${$outage.runtime|d:0} min", 0, 6, 46, "$outage.runtime_color", bold=True, align="right", r=18, w=260),
    L("runtime left", 0, 66, 13, "sand", align="right", r=20, w=200),
    card(0, 104, 320, 316, [
        L("UPS", 14, 10, 15, "muted"),
        L("${$ups.batt|d:0|u:%}", 14, 32, 40, bold=True, w=160),
        L("battery", 0, 50, 13, "muted", align="right", r=14, w=100),
        BAR("$ups.batt", 14, 84, 292, 10, color={"ref": "$outage.low", "map": {"true": "crit"}, "default": "warn"}),
        L("Load", 14, 104, 14, "muted"), L("${$ups.load|d:0|u:%}", 0, 104, 14, align="right", r=14, w=80),
        BAR("$ups.load", 14, 124, 292, 8, color="$ups.load_color"),
        KV("Rack power", "${$pdu.watts|d:0|u: W}", 14, 146, r=14, size=15),
        KV("Mains voltage", "${$ups.vin|d:0|u: V}", 14, 170, r=14, size=15),
        KV("Battery voltage", "${$ups.bvolt|d:1|u: V}", 14, 194, r=14, size=15),
        KV("Internet", "${$inet.ms|d:0|u: ms|or:DOWN}", 14, 218, r=14, size=15),
        L("Hard power-off is never offered; shutdowns are soft (ACPI) and need a 1.5 s hold.", 14, 252, 12, "dim", r=14, wrap=3),
    ]),
    card(328, 104, 330, 316, [
        L("Shutdown plan", 14, 10, 15, "muted"),
        L("${$outage.advice}", 14, 32, 13, "$outage.advice_color", bold=True, r=14, wrap=2),
        REP("$outage.plan", 10, 70, 310, 240, [free(0, 0, None, 54, [
            BOX(0, 0, 310, 48, "card2", radius=8),
            BOX(0, 8, 4, 32, "@color", radius=2),
            L("${@name}", 14, 6, 16, bold=True, w=170),
            L("${@note} · ${@watts|d:0|u: W|or:—}", 14, 27, 12, "muted", w=180),
            N("button", dict(pos(None, 6, 96, 36, 6), label="Shut down", color="warn", size=13, exact=True,
                             service="button.press", target="button.idrac_{id}_soft_shutdown", confirm="hold",
                             confirm_title="Soft Shutdown: {item}?", danger=True,
                             confirm_text="Power outage: asks {item}'s OS to shut down cleanly\n(ACPI power button) to save UPS runtime.",
                             show_if=show("@can_soft", "==", True))),
        ])], mode="all", columns=1, row_h=54, gap_y=0),
    ]),
]), middle="silence", show_when="outage")

# ============================================================ Timeline + history + new devices
start("tl")
graph_card = lambda y, title, key, unit: card(0, y, 270, 86, [
    L(title, 12, 6, 13, "muted", w=150),
    N("graph", dict(pos(10, 26, 250, 52), history=key, minutes=1440, caption=unit, color="info")),
])
timeline = screen("timeline", "Timeline", "#9b8cff", free(0, 0, 658, 420, [
    card(0, 0, 380, 420, [
        L("What happened", 14, 10, 15, "muted"),
        L("newest first", 0, 12, 12, "dim", align="right", r=14, w=100),
        L("Nothing yet: events appear as they happen.", 14, 40, 14, "dim", show_if=show("$events", "is_empty")),
        REP("$events", 8, 34, 364, 380, [free(0, 0, None, 38, [
            DOT("@color", 10, 10, 5),
            L("${@when}", 22, 2, 12, "muted", w=86),
            L("${@text}", 100, 0, 14, r=4),
            L("${@detail|or:}", 100, 19, 11, "dim", r=4),
        ])], max=10, mode="all", columns=1, row_h=38, gap_y=0),
    ]),
    free(388, 0, 270, 420, [
        card(0, 0, 270, 156, [
            L("New devices", 14, 10, 15, "muted"),
            L("${$devices_count|or:0} on the network", 0, 12, 12, "dim", align="right", r=14, w=130),
            L("Nothing new on the network.", 14, 44, 14, "ok", show_if=show("$devices_new", "is_empty")),
            L("Alerts here when a device joins that wasn't seen before.", 14, 66, 12, "dim", r=14, wrap=2,
              show_if=show("$devices_new", "is_empty")),
            REP("$devices_new", 10, 34, 250, 76, [free(0, 0, None, 38, [
                L("${@name}", 4, 0, 14, "warn", bold=True, r=4),
                L("${@ip} · ${@at}", 4, 19, 11, "muted", r=4),
            ])], max=2, mode="all", columns=1, row_h=38, gap_y=0),
            N("button", dict(pos(10, 112, None, 34, 10), label="Mark all known", action="app", command="dev_ack_all",
                             color="info", size=13, exact=True, show_if=show("$devices_new"))),
        ]),
        graph_card(164, "UPS load · 24 h", "ups.load", "%"),
        graph_card(254, "Rack power · 24 h", "pdu.watts", "W"),
        graph_card(344, "Internet ping · 24 h", "inet.ms", "ms"),
    ]),
]))

# ============================================================ Scratchbook
start("nb")
notes = screen("notes", "Scratchbook", "#2ec4b6", card(0, 0, 658, 420, [
    L("Notes about the rack", 14, 10, 15, "muted"),
    L("write and edit them in the builder · Notes", 0, 12, 12, "dim", align="right", r=14, w=300),
    L("No notes yet. Open the builder and add one, e.g. which drive was replaced.", 14, 44, 15, "dim", r=14, wrap=2,
      show_if=show("$notes", "is_empty")),
    REP("$notes", 10, 36, 638, 380, [free(0, 0, None, 76, [
        BOX(0, 0, 638, 70, "card2", radius=8),
        BOX(0, 8, 4, 54, "@color", radius=2),
        L("${@text}", 14, 8, 15, r=100, wrap=2),
        L("${@when}", 14, 50, 11, "muted", w=200),
        N("button", dict(pos(None, 16, 80, 36, 10), label="${@pin_label}", action="app", command="note_pin:{id}",
                         color="teal", size=13, exact=True)),
    ])], max=5, mode="all", columns=1, row_h=76, gap_y=0),
]))

out = {"version": 2, "alertLabel": "rackview_alert", "alerts": [],
       "screens": [overview, power, servers, network, ports, time_, hotspot, ha, cooling, lights, timeline, notes, outage]}
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "layouts", "default_screens.json")
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(out, f, indent=1, ensure_ascii=False)
    f.write("\n")
print("wrote", os.path.normpath(path), sum(1 for _ in json.dumps(out)), "bytes")
