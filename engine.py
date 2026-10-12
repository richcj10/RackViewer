"""RackView screen engine: lays out and draws screens made in the web builder.

Screen = tree of nodes {id, type, props, children}.
Containers: free (children placed at x/y/w/h), column, row, inline, card, tabs/tab, repeater, listdetail/slot.
Widgets: label, kv, bar, dot, box, graph, tile, big, value, gauge, spark, entities, status, pill, chips, text,
button, toggle, slider, banner, alerts; special: ups_test, hunt_list, hunt_faceplate, hunt_client, wled_panel.

Value references ("refs"), used by every data field:
    sensor.x                 entity state
    sensor.x@attr.path.0     attribute (dotted path, list indexes allowed)
    {{ jinja }}              Home Assistant template, rendered by HA
    @field                   field of the current repeater / list item
    $path.to.value           RackView's data model (servers, ups, pdu, fw, pings, time, hotspot, ha, ...)
Text may contain {item}, {id}, {item.field} placeholders and ${ref|filter|filter} values, e.g.
"${$ups.batt|u:%}", "${@watts|d:1|u: W|or:off}", "${@power|map:on=ON;off=OFF;*=?}". Filters: d:N decimals,
u:unit, or:missing-text, map:k=v;*=other, x:factor, abs, sign, upper, lower, len, join:sep, pre:prefix,
time:strftime (ISO time), age (hours -> "5 h ago"), mmss (seconds -> m:ss).

Colours (color, bg, border, stripe props): a name (ok warn crit info dim muted text accent orange teal purple
bg card card2 line), "#rrggbb", a ref to a value holding a colour name ("@color", "$ups.color"), a map
{"ref": .., "map": {"on": "ok"}, "default": "dim"}, or rules [{ref?, op, value, color}, {color}] (first match;
a rule without op always matches).

Common props on every node: show_if {ref, op, value}; goto (screen id; inside a repeater also selects that
item there); link (detail screen id); ml/mr/mt (margins px). Children of a free container: x, y, w, h,
r (right offset), b (bottom offset).
"""
import fnmatch
import json
import math
import re
import time

GAP = 8
PAD = 12
RULE_OPS = ("<=", ">=", "!=", "==", "<", ">", "contains", "is_empty", "not_empty")
NATURAL = {"value": 24, "gauge": 26, "spark": 70, "tile": 92, "big": 64, "pill": 46, "button": 44,
           "toggle": 36, "slider": 58, "banner": 54, "chips": 30}
FILLERS = {"alerts", "listdetail", "builtin", "wled_panel", "hunt_list"}
COLOR_NAMES = {"ok": "OK", "warn": "WARN", "crit": "CRIT", "info": "INFO", "dim": "DIM", "muted": "MUTED",
               "text": "TEXT", "bg": "BG", "card": "CARD", "card2": "CARD2", "line": "LINE",
               "orange": (255, 140, 40), "teal": (0, 210, 180), "purple": (176, 120, 255), "sand": (240, 220, 200)}
PLACEHOLDER = re.compile(r"\$\{([^{}]*)\}")
MISSING = (None, "", "unknown", "unavailable")


# ---------------------------------------------------------------------------- pure helpers
def compare(state, op, rhs):
    if op == "is_empty":
        return state in (None, "", [], {}, "unknown", "unavailable", "none", "None")
    if op == "not_empty":
        return not compare(state, "is_empty", rhs)
    if op == "contains":
        return str(rhs).lower() in str(state).lower()
    try:
        a, b = float(state), float(rhs)
        num = True
    except (TypeError, ValueError):
        num = False
    if op in ("==", "!="):
        eq = (a == b) if num else str(state).lower() == str(rhs).lower()
        return eq if op == "==" else not eq
    if not num:
        return False
    return {"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}.get(op, False)


RULE = re.compile(r"^\s*(<=|>=|!=|==|<|>)\s*(.+?)\s*$")


def rule_hit(rule, state):
    """Short rule strings used by tiles and alert rows: '< 60', '!= online'."""
    m = RULE.match(str(rule or ""))
    return bool(m) and state is not None and compare(state, m.group(1), m.group(2))


def templ(s, item):
    """Fill {item}, {id} and {item.field} from the current repeater/list item."""
    if s is None or not item or "{" not in str(s):
        return s
    if isinstance(item, dict):
        name, iid = item.get("name", ""), item.get("id", "")
    else:
        name, iid = item, item
    out = str(s).replace("{item}", str(name)).replace("{id}", str(iid))
    if isinstance(item, dict):
        out = re.sub(r"\{item\.([\w.]+)\}", lambda m_: str(dig(item, m_.group(1)) if dig(item, m_.group(1)) is not None else ""), out)
    return out


def dig(obj, path):
    """obj['a']['b'][0] from 'a.b.0'."""
    for part in str(path).split("."):
        if part == "":
            continue
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, (list, tuple)):
            try:
                obj = obj[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if obj is None:
            return None
    return obj


def placeholder_refs(s):
    """Refs used inside ${...} placeholders of a text."""
    return [m.split("|", 1)[0].strip() for m in PLACEHOLDER.findall(str(s or ""))]


def _key(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return "" if v is None else str(v)


def apply_filters(v, filters, resolve=None):
    """Format a value for display: see the module docstring for the filters."""
    dflt, unit, dec, sign, pre, mapped, pad = "—", "", None, False, "", False, 0
    for f in filters:
        name, _, arg = f.partition(":")
        name = name.strip()
        if resolve and arg[:1] in ("@", "$") and name in ("or", "u", "pre"):
            arg = resolve(arg)
            arg = "" if arg is None else str(arg)
        if name == "or":
            dflt = arg
        elif name == "u":
            unit = arg
        elif name == "d":
            dec = int(arg or 0)
        elif name == "pre":
            pre = arg
        elif name == "pad":
            pad = int(arg or 0)
        elif name == "sign":
            sign = True
        elif name == "len":
            v = len(v) if isinstance(v, (list, dict, str)) else 0
        elif name == "join":
            v = (arg or ", ").join(str(x) for x in v) if isinstance(v, (list, tuple)) else v
        elif name in ("x", "abs"):
            try:
                v = float(v) * (float(arg) if name == "x" else 1)
                v = abs(v) if name == "abs" else v
            except (TypeError, ValueError):
                pass
        elif name == "map":
            table = dict(kv.split("=", 1) for kv in arg.split(";") if "=" in kv)
            k = _key(v)
            if k in table:
                v, mapped = table[k], True
            elif k.lower() in {x.lower(): x for x in table}:
                v, mapped = table[{x.lower(): x for x in table}[k.lower()]], True
            elif "*" in table:
                v, mapped = table["*"], True
        elif name in ("upper", "lower") and isinstance(v, str):
            v = v.upper() if name == "upper" else v.lower()
        elif name == "time":
            try:
                import datetime as _dt
                v = _dt.datetime.fromisoformat(str(v)).astimezone().strftime(arg or "%H:%M")
            except (TypeError, ValueError):
                v = None
        elif name == "age":
            try:
                h = float(v)
                v, mapped = (f"{h:.0f} h ago" if h < 48 else f"{h / 24:.0f} days ago"), True
            except (TypeError, ValueError):
                v = None
        elif name == "mmss":
            try:
                t = int(float(v))
                v, mapped = f"{t // 60}:{t % 60:02d}", True
            except (TypeError, ValueError):
                v = None
    if mapped:
        out = "" if v is None else str(v)
    elif v in MISSING:
        return dflt
    elif isinstance(v, bool):
        out = "on" if v else "off"
    elif isinstance(v, (int, float)):
        f = float(v)
        d = dec if dec is not None else (0 if f.is_integer() or abs(f) >= 10 else 1) if isinstance(v, float) else 0
        out = f"{f:+.{d}f}" if sign else f"{f:.{d}f}"
    elif isinstance(v, (list, dict)):
        out = json.dumps(v)[:40]
    else:
        out = str(v)
    if pad:
        out = out.rjust(pad)
    return f"{pre}{out}{unit}"


def walk(node):
    if not isinstance(node, dict):
        return
    yield node
    for c in node.get("children") or []:
        yield from walk(c)


def fmt_value(v, unit="", decimals=None, kind=None):
    if v is None or v in ("unknown", "unavailable", ""):
        return "—"
    if kind == "duration":
        try:
            s = int(float(v))
            return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
        except (TypeError, ValueError):
            return str(v)
    if isinstance(v, (list, dict)):
        return json.dumps(v)[:40]
    try:
        f = float(v)
        if decimals is not None and decimals != "":
            s = f"{f:.{int(decimals)}f}"
        else:
            s = f"{f:.0f}" if abs(f) >= 100 or f == int(f) else f"{f:.1f}"
    except (TypeError, ValueError):
        s = str(v)
    unit = (unit or "").replace("Â", "")
    return f"{s}{unit if unit == '%' else (' ' + unit if unit else '')}"


def shorten(items):
    """Drop the words every item name shares ('iDRAC Router PCH Temp' -> 'PCH Temp')."""
    if len(items) < 2:
        return
    words = [str(i["name"]).split() for i in items]
    pre = 0
    while all(len(w) > pre + 1 for w in words) and len({w[pre].lower() for w in words}) == 1:
        pre += 1
    suf = 0
    while all(len(w) > pre + suf + 1 for w in words) and len({w[-1 - suf].lower() for w in words}) == 1:
        suf += 1
    if pre and all(re.fullmatch(r"[\d.]+", " ".join(w[pre:len(w) - suf]) or "x") for w in words):
        pre -= 1                     # 'Fan 1' reads better than '1'
    for it, w in zip(items, words):
        it["full_name"] = it["name"]
        it["name"] = " ".join(w[pre:len(w) - suf]) or it["name"]


def natural_key(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(s))]


# ---------------------------------------------------------------------------- config analysis
def screen_roots(cfg):
    for s in cfg.get("screens", []):
        yield s, s.get("root") or {}
        for d in s.get("details") or []:
            yield s, d.get("root") or {}


def ref_strings(cfg):
    """Every ref-like string in the config (entity refs, templates, rules)."""
    out = []
    for _, root in screen_roots(cfg):
        for n in walk(root):
            p = n.get("props") or {}
            for k in ("entity", "target", "value", "max", "sub_ref", "attr_list"):
                if isinstance(p.get(k), str) and p.get(k):
                    out.append(p[k])
            for e in p.get("entities") or []:
                out.append(e)
            for k in ("show_if",):
                if isinstance(p.get(k), dict) and p[k].get("ref"):
                    out.append(p[k]["ref"])
            if p.get("source") == "attr" and p.get("ref"):
                out.append(p["ref"])
            for c in p.get("colors") or []:
                if c.get("ref"):
                    out.append(c["ref"])
            for k in ("label", "sub", "title", "text", "value", "confirm_text", "confirm_title"):
                if isinstance(p.get(k), str) and "{{" in p[k]:
                    out.append(p[k])
                if isinstance(p.get(k), str) and "${" in p[k]:
                    out.extend(placeholder_refs(p[k]))
    for a in cfg.get("alerts") or []:
        if a.get("ref") or a.get("entity"):
            out.append(a.get("ref") or a.get("entity"))
    return out


def entity_of(ref):
    """'sensor.x@a.b' -> 'sensor.x'; templates and item fields -> None."""
    if not ref or not isinstance(ref, str) or ref[:1] in ("@", "$") or "{{" in ref:
        return None
    e = ref.split("@", 1)[0].strip()
    return e if re.match(r"^[a-z_]+\.[\w{}.*?-]+$", e) else None


def entity_regex(refs):
    """Regex alternatives so the HA query fetches referenced entities, {id} templates and globs."""
    parts = set()
    for r in refs:
        e = entity_of(r)
        if not e:
            continue
        x = re.sub(r"\\\{[^}]*\\\}", "[a-z0-9_]+", re.escape(e))      # {id} / {item.x} placeholders
        parts.add("^" + x.replace(r"\*", ".*").replace(r"\?", ".") + "$")  # glob wildcards
    return "|".join(sorted(parts))


# ---------------------------------------------------------------------------- engine
class Engine:
    def __init__(self, ui):
        self.ui = ui
        self.tab = {}            # tabs node id -> active index
        self.item = {}           # repeater/listdetail node id -> selected index
        self.scroll = {}         # list node id -> scroll offset px
        self.record = None       # list of (node id, rect) while the builder asks for a layout map

    # ------------------------------------------------------------------ data access
    def ents(self):
        return ((self.ui.model or {}).get("ents")) or {}

    def tvals(self):
        return getattr(self.ui.data, "tvals", {}) or {}

    def resolve(self, ref, item=None):
        """Value of a ref (state / attribute / template / item field)."""
        if ref is None or ref == "":
            return None
        if not isinstance(ref, str):
            return ref
        ref = templ(ref, item)
        if "{{" in ref:
            return self.tvals().get(ref)
        if ref.startswith("@"):
            return dig(item, ref[1:]) if isinstance(item, dict) else (item if ref == "@" else None)
        if ref.startswith("$"):
            return dig(self.ui.model or {}, ref[1:])
        if "@" in ref:
            e, path = ref.split("@", 1)
            a = (self.ents().get(e.strip()) or {}).get("a") or {}
            return dig(a, path.strip())
        e = self.ents().get(ref.strip())
        return e["s"] if e else None

    def eref(self, ref, item):
        """Entity id(s) from a widget's entity/target field: literal, {id} template, @field or $model path
        (a list of entity ids is passed through)."""
        if isinstance(ref, str) and ref[:1] in ("@", "$"):
            v = self.resolve(ref, item)
            return list(v) if isinstance(v, (list, tuple)) else (str(v) if v else None)
        return templ(ref, item)

    def model_ref(self, ref):
        """Inside ${...}: 'ups.batt' means $ups.batt when 'ups' is a data-model key and not an entity."""
        if ref[:1] in ("@", "$") or "{{" in ref:
            return ref
        e = ref.split("@", 1)[0]
        if e not in self.ents() and ref.split(".", 1)[0] in (self.ui.model or {}):
            return "$" + ref
        return ref

    def fmt(self, s, item):
        """Replace ${ref|filters} placeholders in a text."""
        def one(m):
            parts = [x for x in m.group(1).split("|")]
            ref = self.model_ref(parts[0].strip())
            return apply_filters(self.resolve(ref, item), parts[1:], lambda r: self.resolve(r, item))
        return PLACEHOLDER.sub(one, s)

    def number(self, x, item=None, default=0.0):
        """A number given literally ('100', 100) or as a ref to read."""
        if x in (None, ""):
            return float(default)
        try:
            return float(x)
        except (TypeError, ValueError):
            try:
                return float(self.resolve(x, item))
            except (TypeError, ValueError):
                return float(default)

    def unit(self, ref, item=None):
        e = entity_of(templ(ref, item) if ref else ref)
        if not e or "@" in str(ref):
            return ""
        return ((self.ents().get(e) or {}).get("u") or "").replace("Â", "")

    def text(self, s, item):
        """Label/text with {item} placeholders and optional {{ templates }}."""
        if s is None:
            return ""
        s = templ(s, item)
        if "{{" in str(s):
            v = self.tvals().get(s)
            return "…" if v is None else str(v)
        s = str(s)
        return self.fmt(s, item) if "${" in s else s

    def shown(self, n, item):
        cond = (n.get("props") or {}).get("show_if")
        if not cond or not cond.get("ref"):
            return True
        return compare(self.resolve(cond["ref"], item), cond.get("op", "=="), cond.get("value", ""))

    def color(self, rules, value, default, item=None):
        for c in rules or []:
            if compare(value if not c.get("ref") else self.resolve(c["ref"], item), c.get("op", "=="), c.get("value", "")):
                return self.paint(c.get("color"), item, value, default)
        return default

    def paint(self, spec, item=None, own=None, default=None):
        """Colour from a name, #hex, @/$ ref holding a colour name, a {ref, map, default} map or a rule list."""
        if spec in (None, "", [], {}):
            return default
        if isinstance(spec, str):
            if spec[:1] in ("@", "$"):
                v = self.resolve(spec, item)
                return self.named(v, default) if v not in (None, "") else default
            return self.named(spec, default)
        if isinstance(spec, dict):
            v = self.resolve(spec.get("ref"), item) if spec.get("ref") else own
            m = spec.get("map") or {}
            col = m.get(_key(v), m.get(_key(v).lower(), spec.get("default")))
            return self.paint(col, item, own, default)
        if isinstance(spec, (list, tuple)) and spec and isinstance(spec[0], dict):
            for c in spec:
                if not c.get("op"):
                    return self.paint(c.get("color"), item, own, default)
                v = self.resolve(c["ref"], item) if c.get("ref") else own
                if compare(v, c["op"], c.get("value", "")):
                    return self.paint(c.get("color"), item, own, default)
            return default
        return self.named(spec, default)

    def named(self, name, default=None):
        import rackview as rv
        if not name:
            return default
        if isinstance(name, (list, tuple)):
            return tuple(name)
        if str(name).startswith("#") and len(name) == 7:
            return tuple(int(name[i:i + 2], 16) for i in (1, 3, 5))
        if name == "accent":
            page = self.ui.PAGES[self.ui.page] if self.ui.PAGES else None
            return rv.PAGE_COLORS.get(page, (rv.INFO, None))[0]
        if name == "none":
            return None
        v = COLOR_NAMES.get(name)
        if isinstance(v, str):
            return getattr(rv, v)
        return v or default

    def items(self, p, parent_item=None):
        """Items for repeaters, lists and chips: list of dicts with at least id and name."""
        m = self.ui.model or {}
        src = p.get("source")
        if src == "idrac":
            return [dict(id=s["id"], name=s["name"], power=s.get("power"), health=s.get("health"), on=s.get("on"))
                    for s in m.get("servers", [])]
        if src == "fancontrol":
            return [dict(id=fc.get("id"), name=fc["name"], offline=fc.get("offline")) for fc in m.get("fanctl", [])]
        if src == "entities" and p.get("pattern"):
            pat = templ(p["pattern"], parent_item)
            out = []
            for e in sorted(self.ents()):
                if fnmatch.fnmatch(e, pat):
                    rec = self.ents()[e]
                    if p.get("skip_unavailable", True) and rec.get("s") in ("unknown", "unavailable", None):
                        continue
                    out.append(dict(id=e, entity=e, name=rec.get("n") or (rec.get("a") or {}).get("friendly_name")
                                    or e.split(".", 1)[-1].replace("_", " "), state=rec.get("s"), unit=rec.get("u")))
            out.sort(key=lambda o: natural_key(o["name"]))
            if p.get("short_names", True):
                shorten(out)
            return out
        if src in ("attr", "model") and (p.get("ref") or p.get("path")):
            v = self.resolve(p.get("ref") or p.get("path"), parent_item)
            if isinstance(v, dict):
                v = [dict(val, id=k, name=val.get("name", k)) if isinstance(val, dict) else dict(id=k, name=k, value=val)
                     for k, val in v.items()]
            if not isinstance(v, list):
                return []
            out = []
            for i, x in enumerate(v):
                if isinstance(x, dict):
                    d = dict(x)
                    d["_i"] = i
                    d.setdefault("id", str(i))
                    d.setdefault("name", str(x.get(p.get("name_field") or "name") or x.get("callsign") or x.get("id") or i))
                    out.append(d)
                else:
                    out.append(dict(id=str(i), name=str(x), value=x, _i=i))
            return out
        if src == "list":
            return [dict(id=str(x).strip(), name=str(x).strip()) for x in str(p.get("values") or "").split(",") if str(x).strip()]
        return []

    # ------------------------------------------------------------------ measuring
    def natural(self, n, w, item):
        if not self.shown(n, item):
            return None
        t, p = n.get("type"), n.get("props") or {}
        if p.get("h"):
            return int(p["h"])
        kids = n.get("children") or []
        if t == "text":
            return {"s": 16, "m": 20, "l": 30, "xl": 48}.get(p.get("size", "m"), 20)
        if t == "label":
            size = int(p.get("size") or 15)
            if p.get("wrap"):
                return len(self.ui.wrap(self.text(p.get("text"), item), size, max(20, w), int(p["wrap"]))) * (size + 4)
            return size + 6
        if t == "kv":
            return int(p.get("size") or 16) + 9
        if t in ("dot", "bar"):
            return 12
        if t == "free":
            hs = []
            for c in kids:
                if not self.shown(c, item):
                    continue
                cp = c.get("props") or {}
                x = int(cp.get("x") or 0)
                cw = int(cp["w"]) if cp.get("w") else w - x - int(cp.get("r") or 0)
                hs.append(int(cp.get("y") or 0) + (int(cp["h"]) if cp.get("h") else (self.natural(c, cw, item) or 0)))
            return max(hs or [0])
        if t == "inline":
            return max([self.natural(c, w, item) or 0 for c in kids if self.shown(c, item)] or [0])
        if t in NATURAL:
            return NATURAL[t]
        if t == "status":
            per_row = max(1, int(w // 150))
            return 22 * max(1, math.ceil(len(p.get("entities") or []) / per_row))
        if t == "entities":
            return 22 * max(1, len(p.get("entities") or [])) + (20 if p.get("title") else 0)
        if t == "alerts":
            return 28 * min(int(p.get("max") or 4), 2) + 8
        if t in ("column", "tab", "slot"):
            hs = [h for h in (self.natural(c, w, item) for c in kids) if h is not None]
            gap = int(p["gap"]) if p.get("gap") is not None else GAP
            return sum(hs) + gap * max(0, len(hs) - 1)
        if t == "row":
            vis = [c for c in kids if self.shown(c, item)]
            cw = (w - GAP * max(0, len(vis) - 1)) / max(1, len(vis))
            return max([self.natural(c, self.fixed_w(c, cw), item) or 0 for c in vis] or [0])
        if t == "card":
            hs = [h for h in (self.natural(c, w - 2 * PAD, item) for c in kids) if h is not None]
            return sum(hs) + GAP * max(0, len(hs) - 1) + 20 + (24 if p.get("title") or p.get("link") else 0)
        if t == "tabs":
            i = min(self.tab.get(n["id"], 0), max(0, len(kids) - 1))
            return 36 + ((self.natural(kids[i], w, item) or 0) if kids else 0)
        if t == "repeater":
            its = self.limit(p, self.items(p, item), item) or [{}]
            if p.get("mode") == "all" and p.get("row_h"):
                if not self.limit(p, self.items(p, item), item):
                    return 0
                cols = int(p.get("columns") or 1)
                rows = math.ceil(len(its) / cols)
                gy = int(p["gap_y"]) if p.get("gap_y") is not None else GAP
                return rows * int(p["row_h"]) + gy * (rows - 1)
            if p.get("mode") == "all":
                cols = int(p.get("columns") or len(its)) or 1
                rows = math.ceil(len(its) / cols)
                cw = (w - GAP * (cols - 1)) / cols
                rh = max(sum(self.natural(c, cw, it) or 0 for c in kids) + GAP * max(0, len(kids) - 1) for it in its)
                return rows * rh + GAP * (rows - 1)
            return 18 + sum(self.natural(c, w, its[0]) or 0 for c in kids)
        return 60

    def limit(self, p, its, item=None):
        if p.get("max") not in (None, ""):
            return its[:max(0, int(self.number(p["max"], item, len(its))))]
        return its

    def width(self, n, item):
        """Natural width of an inline child (labels measure their text)."""
        p = n.get("props") or {}
        if p.get("w"):
            return int(p["w"])
        if n.get("type") == "label":
            tw = self.ui.F(int(p.get("size") or 15), self.flag(p.get("bold"), item)).size(self.text(p.get("text"), item))[0]
            return tw + (18 if p.get("chip") else 0)
        if n.get("type") == "dot":
            return 2 * int(p.get("radius") or 5) + 2
        return 40

    def flag(self, v, item):
        """A yes/no prop given literally or as a @/$ ref."""
        if isinstance(v, str) and v[:1] in ("@", "$"):
            return self.resolve(v, item) in (True, "on", "true", 1)
        return bool(v)

    def fills(self, n):
        return bool((n.get("props") or {}).get("grow")) or n.get("type") in FILLERS

    def fixed_w(self, n, default):
        w = (n.get("props") or {}).get("w")
        return int(w) if w else default

    # ------------------------------------------------------------------ drawing core
    def draw(self, n, r, item):
        if not isinstance(n, dict) or not self.shown(n, item):
            return
        p = n.get("props") or {}
        r = self.ui.pg.Rect(r)
        if p.get("ml") or p.get("mr") or p.get("mt"):
            ml, mr, mt = int(p.get("ml") or 0), int(p.get("mr") or 0), int(p.get("mt") or 0)
            r = self.ui.pg.Rect(r.x + ml, r.y + mt, r.w - ml - mr, r.h - mt)
        if self.record is not None and n.get("id"):
            self.record.append((n["id"], tuple(r)))
        t = n.get("type")
        if p.get("goto"):
            idx = item.get("_i") if isinstance(item, dict) else None
            self.ui.hit(r, lambda g=p["goto"], i=idx: self.ui.goto(g, i))
        if p.get("link") and t not in ("card", "button"):
            self.ui.hit(r, lambda d=p["link"], it=item: self.ui.open_detail(d, it))
        fn = getattr(self, "w_" + str(t), None)
        if fn:
            fn(n, r, item, p)

    def stack(self, kids, r, item, vertical=True, gap=GAP):
        pg = self.ui.pg
        kids = [c for c in kids if self.shown(c, item)]
        if not kids:
            return
        if not vertical:
            fixed = sum(self.fixed_w(c, 0) for c in kids)
            flex = [c for c in kids if not (c.get("props") or {}).get("w")]
            fw = (r.w - fixed - gap * (len(kids) - 1)) / max(1, len(flex))
            x = r.x
            for c in kids:
                cw = self.fixed_w(c, fw)
                self.draw(c, pg.Rect(int(x), r.y, int(cw), r.h), item)
                x += cw + gap
            return
        nat = [self.natural(c, r.w, item) or 0 for c in kids]
        grow = [i for i, c in enumerate(kids) if self.fills(c) and not (c.get("props") or {}).get("h")]
        free = r.h - sum(nat[i] for i in range(len(kids)) if i not in grow) - gap * (len(kids) - 1)
        if grow and free > 0:
            for i in grow:
                nat[i] = max(nat[i], free / len(grow))
        y = r.y
        canvas = self.ui.canvas
        clip = canvas.get_clip()
        canvas.set_clip(r.clip(clip) if clip else r)
        for c, h in zip(kids, nat):
            self.draw(c, pg.Rect(r.x, int(y), r.w, int(h)), item)
            y += h + gap
        canvas.set_clip(clip)

    # ------------------------------------------------------------------ containers
    def gap(self, p):
        return int(p["gap"]) if p.get("gap") not in (None, "") else GAP

    def w_column(self, n, r, item, p):
        self.stack(n.get("children") or [], r, item, gap=self.gap(p))

    w_tab = w_column
    w_slot = w_column

    def w_row(self, n, r, item, p):
        self.stack(n.get("children") or [], r, item, vertical=False, gap=self.gap(p))

    def place(self, c, r, item):
        """Rect of a free container's child from its x, y, w, h, r (right offset), b (bottom offset)."""
        cp = c.get("props") or {}
        x, y = int(cp.get("x") or 0), int(cp.get("y") or 0)
        if cp.get("w"):
            w = int(cp["w"])
            if cp.get("r") not in (None, ""):          # width + right offset: anchored to the right edge
                x = r.w - int(cp["r"]) - w
        else:
            w = r.w - x - int(cp.get("r") or 0)
        if cp.get("h"):
            h = int(cp["h"])
        elif cp.get("b") not in (None, ""):
            h = r.h - y - int(cp["b"])
        else:
            h = self.natural(c, w, item) or 20
        return self.ui.pg.Rect(r.x + x, r.y + y, w, h)

    def w_free(self, n, r, item, p):
        """Container whose children sit at explicit positions; optional card background."""
        import rackview as rv
        bg = self.paint(p.get("bg"), item, None, None)
        if bg:
            if p.get("tint") not in (None, ""):
                bg = self.ui.tint(bg, float(p["tint"]))
            self.ui.pg.draw.rect(self.ui.canvas, bg, r, border_radius=int(p.get("radius", 10)))
        bc = self.paint(p.get("border"), item, None, None)
        if bc:
            self.ui.pg.draw.rect(self.ui.canvas, bc, r, int(p.get("bw") or 2), border_radius=int(p.get("radius", 10)))
        for c in n.get("children") or []:
            if self.shown(c, item):
                self.draw(c, self.place(c, r, item), item)

    def w_inline(self, n, r, item, p):
        """Children side by side at their natural widths (text followed by a chip, ...)."""
        x = r.x
        for c in n.get("children") or []:
            if not self.shown(c, item):
                continue
            cw = self.width(c, item)
            self.draw(c, self.ui.pg.Rect(x, r.y, cw, r.h), item)
            x += cw + int(p.get("gap") if p.get("gap") is not None else 6)

    def w_card(self, n, r, item, p):
        import rackview as rv
        ui = self.ui
        bg = self.named(p.get("bg"), rv.CARD)
        ui.card(r, bg if bg == rv.CARD else ui.tint(bg, 0.3))
        y = r.y + 10
        title = self.text(p.get("title"), item)
        if title or p.get("link"):
            tc = self.color(p.get("colors"), self.resolve(p.get("entity"), item), rv.MUTED) if p.get("entity") else rv.MUTED
            ui.T(title or "", 14, tc, (r.x + PAD, y), maxw=r.w - 110)
            if p.get("link"):
                tr = ui.T("Details ›", 13, rv.INFO, (r.right - PAD, y), "topright", bold=True)
                ui.hit(tr.inflate(30, 20), lambda d=p["link"], it=item: ui.open_detail(d, it))
            y += 24
        self.stack(n.get("children") or [], ui.pg.Rect(r.x + PAD, y, r.w - 2 * PAD, r.bottom - 10 - y), item)

    def w_tabs(self, n, r, item, p):
        import rackview as rv
        kids = n.get("children") or []
        if not kids:
            return
        i = min(self.tab.get(n["id"], 0), len(kids) - 1)
        x = r.x
        bar_h, tab_h = int(p.get("bar_h") or 36), int(p.get("tab_h") or 28)
        for k, t in enumerate(kids):
            label = self.text((t.get("props") or {}).get("title") or f"Tab {k + 1}", item)
            w = int(p["tab_w"]) if p.get("tab_w") else self.ui.F(13, True).size(label)[0] + 28
            self.ui.button((x, r.y, w, tab_h), label, lambda k=k: self.tab.__setitem__(n["id"], k),
                           col=rv.OK if k == i else rv.MUTED, filled=k == i, size=13)
            x += w + 8
        self.draw(kids[i], self.ui.pg.Rect(r.x, r.y + bar_h, r.w, r.h - bar_h), item)

    def w_repeater(self, n, r, item, p):
        import rackview as rv
        its = self.limit(p, self.items(p, item), item)
        kids = n.get("children") or []
        if not its:
            if p.get("empty") != "":
                self.ui.T(p.get("empty") or "Nothing to show", 13, rv.DIM, (r.x + 4, r.y + 4))
            return
        if p.get("mode") == "all" and p.get("row_h"):
            cols = int(p.get("columns") or 1)
            rows = math.ceil(len(its) / cols)
            gx = int(p["gap_x"]) if p.get("gap_x") not in (None, "") else GAP
            gy = int(p["gap_y"]) if p.get("gap_y") not in (None, "") else GAP
            rh = float(p["row_h"])
            if p.get("fit_rows"):
                rh = min(rh, (r.h - gy * (rows - 1)) // rows)
            cw = float(p["cell_w"]) if p.get("cell_w") else (r.w - gx * (cols - 1)) / cols
            x0 = r.right - (cols * cw + gx * (cols - 1)) if p.get("align") == "right" else r.x
            if p.get("align") == "right":
                cols_used = min(cols, len(its))
                x0 = r.right - (cols_used * cw + gx * (cols_used - 1))
            for k, it in enumerate(its):
                col, row = (k // rows, k % rows) if p.get("flow") == "col" else (k % cols, k // cols)
                self.stack(kids, self.ui.pg.Rect(int(x0 + col * (cw + gx)), int(r.y + row * (rh + gy)), int(cw), int(rh)), it)
            return
        if p.get("mode") == "all":
            cols = int(p.get("columns") or len(its)) or 1
            rows = math.ceil(len(its) / cols)
            cw = (r.w - GAP * (cols - 1)) / cols
            rh = (r.h - GAP * (rows - 1)) / rows if p.get("stretch", True) else \
                max(sum(self.natural(c, cw, it) or 0 for c in kids) + GAP * max(0, len(kids) - 1) for it in its)
            for k, it in enumerate(its):
                cx = r.x + (k % cols) * (cw + GAP)
                cy = r.y + (k // cols) * (rh + GAP)
                self.stack(kids, self.ui.pg.Rect(int(cx), int(cy), int(cw), int(rh)), it)
            return
        i = self.item.get(n["id"], 0) % len(its)
        if p.get("header"):                     # "Router  1/3" next to the screen title
            self.ui.subtitle = f"{its[i].get('name', '')}  {i + 1}/{len(its)}"
            self.stack(kids, r, its[i])
            return
        self.ui.T(f"{its[i].get('name', '')}  {i + 1} / {len(its)}", 11, rv.DIM, (r.right - 4, r.y), "topright")
        self.stack(kids, self.ui.pg.Rect(r.x, r.y + 18, r.w, r.h - 18), its[i])

    def w_listdetail(self, n, r, item, p):
        import rackview as rv
        ui = self.ui
        its = self.items(p, item)
        kids = n.get("children") or []
        row_slot = kids[0] if kids else None
        det_slot = kids[1] if len(kids) > 1 else None
        lw = int(p.get("list_w") or min(260, r.w // 3 + 30))
        lr = ui.pg.Rect(r.x, r.y, lw, r.h)
        ui.card(lr)
        i = min(self.item.get(n["id"], 0), max(0, len(its) - 1))
        rowh = max(30, (self.natural(row_slot, lw - 20, its[0] if its else None) or 20) + 14) if row_slot else 40
        inner = ui.pg.Rect(lr.x + 6, lr.y + 6, lr.w - 12, lr.h - 12)
        maxs = max(0, len(its) * rowh - inner.h)
        sc = self.scroll[n["id"]] = max(0, min(maxs, self.scroll.get(n["id"], 0)))
        clip = ui.canvas.get_clip()
        ui.canvas.set_clip(inner)
        for k in range(sc // rowh, min(len(its), sc // rowh + inner.h // rowh + 2)):
            rr = ui.pg.Rect(inner.x, inner.y + k * rowh - sc, inner.w, rowh - 4)
            ui.pg.draw.rect(ui.canvas, ui.tint(rv.INFO, 0.35) if k == i else rv.CARD2, rr, border_radius=7)
            if row_slot:
                self.stack(row_slot.get("children") or [], rr.inflate(-14, -8), its[k])
            else:
                ui.T(its[k].get("name", ""), 14, rv.TEXT, (rr.x + 8, rr.centery), "midleft")
        ui.canvas.set_clip(clip)
        if not its:
            ui.T(p.get("empty") or "No items", 13, rv.MUTED, (lr.x + 12, lr.y + 12))

        def fn(x, y, phase, its=its):
            if phase == "down":
                self._ld = (y, self.scroll.get(n["id"], 0), False)
            elif phase == "move":
                y0, s0, moved = self._ld
                moved = moved or abs(y - y0) > 8
                self._ld = (y0, s0, moved)
                if moved:
                    self.scroll[n["id"]] = s0 - (y - y0)
            elif phase == "up" and not self._ld[2]:
                k = (y - inner.y + self.scroll.get(n["id"], 0)) // rowh
                if 0 <= k < len(its):
                    self.item[n["id"]] = k
        ui.drag(inner, fn)
        if det_slot is not None and its:
            self.draw(det_slot, ui.pg.Rect(lr.right + GAP, r.y, r.right - lr.right - GAP, r.h), its[i])

    # ------------------------------------------------------------------ primitives (placed text, bars, dots, boxes)
    ANCHOR = {("top", "left"): "topleft", ("top", "center"): "midtop", ("top", "right"): "topright",
              ("middle", "left"): "midleft", ("middle", "center"): "center", ("middle", "right"): "midright",
              ("bottom", "left"): "bottomleft", ("bottom", "center"): "midbottom", ("bottom", "right"): "bottomright"}

    def w_label(self, n, r, item, p):
        import rackview as rv
        ui = self.ui
        text = self.text(p.get("text"), item)
        size, bold = int(p.get("size") or 15), self.flag(p.get("bold"), item)
        col = self.paint(p.get("color"), item, None, rv.TEXT)
        if p.get("upper"):
            text = text.upper()
        align, valign = p.get("align") or "left", p.get("valign") or "top"
        if p.get("chip"):                      # text on a rounded chip sized to it
            cw_ = ui.F(size, bold).size(text)[0] + 18
            ch = min(r.h, size + 10)
            x = {"center": r.centerx - cw_ // 2, "right": r.right - cw_}.get(align, r.x)
            chip = ui.pg.Rect(x, r.y, cw_, ch)
            ui.pg.draw.rect(ui.canvas, self.paint(p["chip"], item, None, rv.CARD2), chip, border_radius=ch // 2)
            ui.T(text, size, col, chip.center, "center", bold=bold)
            return
        if p.get("wrap"):
            lh = size + 4
            for k, line in enumerate(ui.wrap(text, size, max(20, r.w), int(p["wrap"]))):
                x = {"center": r.centerx, "right": r.right}.get(align, r.x)
                ui.T(line, size, col, (x, r.y + k * lh), self.ANCHOR[("top", align)], bold=bold)
            return
        x = {"center": r.centerx, "right": r.right}.get(align, r.x)
        y = {"middle": r.centery, "bottom": r.bottom}.get(valign, r.y)
        ui.T(text, size, col, (x, y), self.ANCHOR[(valign, align)], bold=bold, maxw=r.w if r.w > 4 else None)

    def w_kv(self, n, r, item, p):
        import rackview as rv
        self.ui.kv(r.x, r.y, r.w, self.text(p.get("label"), item), self.text(p.get("text"), item),
                   self.paint(p.get("color"), item, None, rv.TEXT), size=int(p.get("size") or 16))

    def w_bar(self, n, r, item, p):
        import rackview as rv
        v = self.resolve(p.get("value"), item)
        lo, hi = self.number(p.get("min"), item, 0), self.number(p.get("max"), item, 100)
        try:
            frac = (float(v) - lo) / max(1e-9, hi - lo)
        except (TypeError, ValueError):
            frac = 0
        self.ui.bar(r, max(0.0, frac), self.paint(p.get("color"), item, v, rv.INFO),
                    self.paint(p.get("track"), item, v, rv.LINE))

    def w_dot(self, n, r, item, p):
        import rackview as rv
        self.ui.dot(r.center, self.paint(p.get("color"), item, None, rv.DIM), int(p.get("radius") or min(r.w, r.h) // 2))

    def w_box(self, n, r, item, p):
        import rackview as rv
        ui = self.ui
        rad = int(p.get("radius", 8))
        col = self.paint(p.get("color"), item, None, rv.CARD2)
        if col:
            k = p.get("tint")
            if p.get("pulse") and self.resolve(p["pulse"], item) in (True, "on", "true", 1):
                k = 0.55 + 0.3 * (0.5 + 0.5 * math.sin(time.time() * 5))
            ui.pg.draw.rect(ui.canvas, ui.tint(col, float(k)) if k not in (None, "") else col, r, border_radius=rad)
        bc = self.paint(p.get("border"), item, None, None)
        if bc:
            ui.pg.draw.rect(ui.canvas, bc, r, int(p.get("bw") or 2), border_radius=rad)

    def w_graph(self, n, r, item, p):
        """Line graph of a list from the data model ($time.history, field 1 = offset); optional symmetric scale."""
        import rackview as rv
        ui = self.ui
        ui.pg.draw.rect(ui.canvas, rv.BG, r, border_radius=6)
        series = self.resolve(p.get("source"), item) or []
        f, k = p.get("field"), float(p.get("scale") or 1)
        vals = []
        for x in series:
            v = dig(x, str(f)) if f not in (None, "") else x
            try:
                vals.append(float(v) * k)
            except (TypeError, ValueError):
                pass
        if p.get("symmetric"):
            ui.pg.draw.line(ui.canvas, rv.LINE, (r.x, r.centery), (r.right, r.centery))
        if len(vals) < 2:
            return
        if p.get("symmetric"):
            span = max(float(p.get("min_span") or 0) or 1e-9, max(abs(v) for v in vals))
            pts = [(r.x + 4 + i * (r.w - 8) / (len(vals) - 1), r.centery - v / span * (r.h / 2 - 6)) for i, v in enumerate(vals)]
            if p.get("caption"):
                ui.T(f"scale ±{span:.2f} {p['caption']}", 11, rv.DIM, (r.right, r.bottom + 3), "topright")
        else:
            lo, hi = min(vals), max(vals)
            span = (hi - lo) or 1
            pts = [(r.x + 4 + i * (r.w - 8) / (len(vals) - 1), r.bottom - 4 - (v - lo) / span * (r.h - 8)) for i, v in enumerate(vals)]
        ui.pg.draw.lines(ui.canvas, self.paint(p.get("color"), item, None, rv.INFO), False, pts, 2)

    # ------------------------------------------------------------------ special widgets (interactive, drawn by RackView)
    def w_ups_test(self, n, r, item, p):
        self.ui.w_ups_test(r)

    def w_hunt_list(self, n, r, item, p):
        self.ui.hunt_list(r)

    def w_hunt_faceplate(self, n, r, item, p):
        self.ui.hunt_faceplate(r)

    def w_hunt_client(self, n, r, item, p):
        self.ui.hunt_client_card(r)

    def w_wled_panel(self, n, r, item, p):
        self.ui.wled_panel(r)

    # ------------------------------------------------------------------ data widgets
    def w_text(self, n, r, item, p):
        import rackview as rv
        size = {"s": 12, "m": 15, "l": 22, "xl": 40}.get(p.get("size", "m"), 15)
        col = self.color(p.get("colors"), self.resolve(p.get("entity"), item), self.named(p.get("color"), rv.TEXT)) \
            if p.get("entity") else self.named(p.get("color"), rv.TEXT)
        anchor = {"center": "midtop", "right": "topright"}.get(p.get("align"), "topleft")
        x = {"center": r.centerx, "right": r.right}.get(p.get("align"), r.x)
        self.ui.T(self.text(p.get("text"), item), size, col, (x, r.y), anchor, bold=p.get("size") != "s", maxw=r.w)

    def val_text(self, p, item, ref_key="entity"):
        ref = p.get(ref_key)
        v = self.resolve(ref, item)
        return v, fmt_value(v, p.get("unit") if p.get("unit") is not None else self.unit(ref, item), p.get("decimals"),
                            p.get("format"))

    def w_value(self, n, r, item, p):
        import rackview as rv
        v, s = self.val_text(p, item)
        col = self.color(p.get("colors"), v, rv.TEXT)
        self.ui.kv(r.x, r.y, r.w, self.text(p.get("label"), item) or (p.get("entity") or ""), s, col, size=15)

    def w_big(self, n, r, item, p):
        import rackview as rv
        v, s = self.val_text(p, item)
        col = self.color(p.get("colors"), v, rv.TEXT)
        if p.get("label"):
            self.ui.T(self.text(p.get("label"), item), 13, rv.MUTED, (r.x, r.y))
        self.ui.T(s, 40, col, (r.x, r.y + (16 if p.get("label") else 0)), bold=True, maxw=r.w)

    def w_gauge(self, n, r, item, p):
        import rackview as rv
        v, s = self.val_text(p, item)
        try:
            lo, hi = self.number(p.get("min"), item, 0), self.number(p.get("max"), item, 100)
            frac = (float(v) - lo) / max(1e-9, hi - lo)
        except (TypeError, ValueError):
            frac = 0
        col = self.color(p.get("colors"), v, rv.INFO)
        label = self.text(p.get("label"), item)
        lw = 0 if not label else min(120, r.w // 3)
        if label:
            self.ui.T(label, 14, rv.TEXT, (r.x, r.y + 3), maxw=lw - 6)
        vw = 0 if p.get("hide_value") else 96
        self.ui.bar((r.x + lw, r.y + 9, r.w - lw - vw, 8), frac, col)
        if vw:
            self.ui.T(s, 14, rv.TEXT, (r.right, r.y + 3), "topright")

    def w_tile(self, n, r, item, p):
        import rackview as rv
        if p.get("value") not in (None, ""):
            v, s = None, self.text(p["value"], item)
        else:
            v, s = self.val_text(p, item)
        sev = 2 if rule_hit(p.get("crit"), v) else 1 if rule_hit(p.get("warn"), v) else 0
        stripe = self.paint(p.get("stripe"), item, v, None) if p.get("stripe") else None
        self.ui.tile(r, self.text(p.get("label"), item), s, self.text(p.get("sub"), item), sev, stripe=stripe)

    def w_pill(self, n, r, item, p):
        import rackview as rv
        v, s = self.val_text(p, item)
        col = self.paint(p.get("color"), item, v, None) if p.get("color") else self.color(p.get("colors"), v, rv.OK, item)
        text = self.text(p.get("text"), item) or s
        self.ui.pg.draw.rect(self.ui.canvas, self.ui.tint(col, 0.3), r, border_radius=10)
        self.ui.pg.draw.rect(self.ui.canvas, col, r, 2, border_radius=10)
        self.ui.dot((r.x + 22, r.centery), col, 8)
        self.ui.T(str(text).upper() if p.get("upper", True) else text, int(p.get("size") or 21), col,
                  (r.x + 40, r.centery), "midleft", bold=True, maxw=r.w - 52)

    def w_spark(self, n, r, item, p):
        import rackview as rv
        ref = templ(p.get("entity"), item)
        v, s = self.val_text(p, item)
        self.ui.kv(r.x, r.y, r.w, self.text(p.get("label"), item) or (ref or ""), s, size=14)
        hist = self.ui.history(ref, int(p.get("minutes") or 60))
        g = self.ui.pg.Rect(r.x, r.y + 24, r.w, r.h - 26)
        self.ui.pg.draw.rect(self.ui.canvas, rv.BG, g, border_radius=6)
        if len(hist) > 1:
            lo, hi = min(hist), max(hist)
            span = (hi - lo) or 1
            pts = [(g.x + 4 + i * (g.w - 8) / (len(hist) - 1), g.bottom - 4 - (val - lo) / span * (g.h - 8))
                   for i, val in enumerate(hist)]
            self.ui.pg.draw.lines(self.ui.canvas, self.named(p.get("color"), rv.INFO), False, pts, 2)
        else:
            self.ui.T("collecting…", 11, rv.DIM, (g.x + 6, g.y + 4))

    def w_entities(self, n, r, item, p):
        import rackview as rv
        y = r.y
        if p.get("title"):
            self.ui.T(self.text(p["title"], item), 13, rv.MUTED, (r.x, y))
            y += 20
        for e in p.get("entities") or []:
            v = self.resolve(e, item)
            ent = entity_of(templ(e, item)) or e
            name = (self.ents().get(ent) or {}).get("n") or str(ent).split(".", 1)[-1].replace("_", " ")
            self.ui.kv(r.x, y, r.w, name, fmt_value(v, self.unit(e, item)), self.color(p.get("colors"), v, rv.TEXT), size=14)
            y += 22

    def w_status(self, n, r, item, p):
        import rackview as rv
        es = p.get("entities") or []
        per_row = max(1, int(r.w // 150))
        for k, e in enumerate(es):
            x = r.x + (k % per_row) * (r.w / per_row)
            y = r.y + (k // per_row) * 22
            v = self.resolve(e, item)
            s = str(v).lower() if v is not None else ""
            default = rv.DIM if s in ("", "unknown", "unavailable", "none") else rv.CRIT if s in ("off", "problem", "critical", "down") else rv.OK
            col = self.color(p.get("colors"), v, default)
            ent = entity_of(templ(e, item)) or e
            name = (self.ents().get(ent) or {}).get("n") or str(ent).split(".", 1)[-1].replace("_", " ")
            self.ui.dot((int(x) + 6, y + 9), col, 5)
            self.ui.T(name, 13, rv.TEXT, (int(x) + 16, y + 1), maxw=int(r.w / per_row) - 20)

    def w_chips(self, n, r, item, p):
        import rackview as rv
        its = self.items(p, item) if p.get("source") else [dict(id=e, name=e, value=self.resolve(e, item))
                                                             for e in p.get("entities") or []]
        x = r.x
        for it in its:
            label = self.text(p.get("label") or "{item}", it)
            v = self.resolve(p.get("value_ref") or "@value", it) if (p.get("value_ref") or "value" in it) else None
            col = self.color(p.get("colors"), v, rv.OK if v not in (None, "", "off", False, "inactive", "down") else rv.DIM)
            tw = self.ui.F(12, True).size(label)[0] + 18
            if x + tw > r.right:
                break
            chip = self.ui.pg.Rect(x, r.y + 3, tw, 22)
            self.ui.pg.draw.rect(self.ui.canvas, self.ui.tint(col, 0.35), chip, border_radius=11)
            self.ui.T(label, 12, col, chip.center, "center", bold=True)
            x += tw + 6

    def w_banner(self, n, r, item, p):
        self.ui.alert_banner(r)

    def w_alerts(self, n, r, item, p):
        self.ui.alert_list(r, int(p.get("max") or 6))

    def w_builtin(self, n, r, item, p):
        """Old configs: draw the factory layout of that page (screens.json is migrated on load)."""
        d = self.ui.factory_screen(p.get("page"))
        if d:
            self.draw(d["root"], r, item)

    # ------------------------------------------------------------------ controls
    def w_button(self, n, r, item, p):
        import rackview as rv
        ui = self.ui
        col = self.paint(p.get("color") or "info", item, None, rv.INFO)
        label = self.text(p.get("label"), item) or "Button"
        action = p.get("action") or "service"
        confirm = self.resolve(p["confirm"], item) if str(p.get("confirm") or "")[:1] in ("@", "$") else p.get("confirm")

        def press():
            if action == "detail" and p.get("link"):
                return ui.open_detail(p["link"], item)
            if action == "back":
                return ui.close_detail()
            svc = templ(p.get("service") or "", item)
            if "." not in svc:
                return ui.notify("This button has no action set", rv.WARN, 3)
            domain, service = svc.split(".", 1)
            try:
                data = json.loads(templ(p.get("data") or "{}", item) or "{}")
            except ValueError:
                return ui.notify("Button data is not valid JSON", rv.CRIT, 4)
            tgt = self.eref(p.get("target"), item)
            if tgt:
                data.setdefault("entity_id", tgt)
            run = lambda: ui.send(f"btn-{n['id']}-{tgt}", label, domain, service, data)
            if confirm in ("confirm", "hold"):
                body = self.text(p.get("confirm_text"), item) or (f"Runs {svc}" + (f" on {tgt}" if tgt else ""))
                ui.ask(self.text(p.get("confirm_title"), item) or f"{label}?", body.split("\n"), run,
                       hold=confirm == "hold", danger=bool(p.get("danger")) or p.get("color") in ("crit", "warn"))
            else:
                run()
        sent = time.monotonic() - ui.pending.get(f"btn-{n['id']}-{self.eref(p.get('target'), item)}", -999) < 5
        ui.button(r if p.get("h") or p.get("exact") else r.inflate(0, -4), "Sent ✓" if sent else label, press, col=col,
                  size=int(p.get("size") or 14), filled=bool(p.get("filled")))

    def w_toggle(self, n, r, item, p):
        import rackview as rv
        ui = self.ui
        e = self.eref(p.get("entity"), item)
        s = self.resolve(e, item)
        on = s == "on"
        label = self.text(p.get("label"), item)
        if label:
            ui.T(label, 15, rv.TEXT, (r.x, r.centery), "midleft", maxw=r.w - 80)
        domain = (e or "x.").split(".", 1)[0]

        def flip():
            svc = "turn_off" if on else "turn_on"
            run = lambda: ui.send(f"tgl-{e}", f"{label or e} {'off' if on else 'on'}", domain, svc, {"entity_id": e})
            if on and p.get("confirmOff"):
                ui.ask(f"Turn off {label or e}?", ["You can turn it back on from here."], run, danger=True)
            else:
                run()
        br = r if not label else ui.pg.Rect(r.right - 64, r.y + 3, 64, r.h - 6)
        ui.button(br, "ON" if on else "OFF", flip, col=rv.OK if on else rv.MUTED,
                  filled=on, size=int(p.get("size") or 13), enabled=s not in rv.BAD)

    def w_slider(self, n, r, item, p):
        import rackview as rv
        ui = self.ui
        e = self.eref(p.get("entity"), item)
        attr = p.get("attr") or "brightness"
        a = (self.ents().get(e) or {}).get("a") or {}
        if attr == "brightness":
            cur, vmax = (a.get("brightness") or a.get("b") or 0) if self.resolve(e) == "on" else 0, 255
        elif attr == "percentage":
            cur, vmax = a.get("percentage") or a.get("pct") or 0, 100
        else:
            try:
                cur = float(self.resolve(e) or 0)
            except ValueError:
                cur = 0
            vmax = float(a.get("max") or 100)
        domain = (e or "x.").split(".", 1)[0]

        def set_value(v):
            if attr == "brightness":
                ui.data.call("light", "turn_on" if v else "turn_off", {"entity_id": e, **({"brightness": v} if v else {})})
            elif attr == "percentage":
                ui.data.call("fan", "set_percentage", {"entity_id": e, "percentage": v})
            else:
                ui.data.call(domain if domain in ("number", "input_number") else "number", "set_value",
                             {"entity_id": e, "value": v})
        ui.slider(r if p.get("bare") else (r.x - 10, r.y + 24, r.w + 20, r.h - 24), f"sl-{n['id']}-{e}", cur, vmax, set_value, col=rv.INFO,
                  label=self.text(p.get("label"), item) or None,
                  fmt_v=(lambda v: f"{round(v * 100 / 255)}%") if attr == "brightness" else None)

    # ------------------------------------------------------------------ navigation helpers
    def first_repeater(self, root):
        for nd in walk(root):
            if nd.get("type") == "repeater" and (nd.get("props") or {}).get("mode") != "all":
                return nd
        return None

    def next_item(self, root):
        rep = self.first_repeater(root)
        if rep:
            k = len(self.items(rep.get("props") or {})) or 1
            self.item[rep["id"]] = (self.item.get(rep["id"], 0) + 1) % k
            return True
        return False

    def detail_targets(self, root):
        """(detail id, item) pairs reachable from a screen: cards/buttons that link to a detail,
        once per item when they sit inside a repeater."""
        out = []

        def visit(n, item_list):
            p = n.get("props") or {}
            if n.get("type") == "repeater":
                its = self.items(p) or [None]
                for c in n.get("children") or []:
                    visit(c, its)
                return
            link = p.get("link") if n.get("type") != "button" or p.get("action") == "detail" else None
            if link:
                for it in item_list:
                    if (link, it) not in out:
                        out.append((link, it))
            for c in n.get("children") or []:
                visit(c, item_list)
        visit(root, [None])
        return out
