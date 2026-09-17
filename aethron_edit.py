#!/usr/bin/env python3
"""THE MODEL STOPS DRAWING THE PAGE AND STARTS EDITING IT.

The owner's reading of the last test was right and it is the whole
reason this file exists. Handed a screenshot, the measurement pipeline
rebuilt the page and scored 22 of 22 lines; handed the same screenshot
and the same measurements, a cheap model scored 6 of 21 and put the
testimonial where the button goes. The conclusion is not that the model
is useless. It is that it was being asked for the one thing it cannot
do — judge a pixel — while the thing it is genuinely good at was never
asked for at all.

So the job is split, permanently:

    MEASUREMENT owns geometry, colour and type. It reads the pixels and
    it is right by construction. No model writes a coordinate, a width,
    a corner radius or a position. Ever.

    THE MODEL owns intent. "Make the button green." "This is Jomiez
    now, not Wezzi." "Shorten the headline." "Give me the nav in a
    lighter weight." It reads a LIST OF ELEMENTS — words, not pixels —
    and returns a LIST OF EDITS naming the ones it wants changed.

The seam between them is this module, and it is guarded the same way
every other seam in this project is guarded: an edit is applied only if
it names a real element and a property that element is allowed to have,
with a value that parses. Then the page is re-rendered and everything
the edit did NOT name must be pixel-identical — a model that was asked
to recolour a button and moved the headline gets its edit refused, not
apologised for.

That last check is the point. It is what lets a weak model near a page
that took real work to get right.
"""
import json
import re
import sys

ID_RE = re.compile(r'data-ae-id="([^"]+)"')
STYLE_RE = re.compile(r'style="([^"]*)"')

# What each kind of element is allowed to be told.
ALLOWED = {
    "text": {"text", "color", "font_size", "font_weight", "font_family",
             "letter_spacing", "opacity", "hidden"},
    "fill": {"background", "border_radius", "opacity", "hidden"},
    "rule": {"background", "opacity", "hidden"},
    "picture": {"opacity", "hidden", "border_radius"},
    "ground": {"opacity"},
    # A BUTTON IS A BOX WITH A LABEL, so it takes both sets. It was
    # absent from this table entirely for as long as buttons were
    # absent from the manifest — "change the call to action" had
    # nothing to name.
    "button": {"text", "color", "font_size", "font_weight",
               "font_family", "letter_spacing", "background",
               "border_radius", "opacity", "hidden"},
    # A TEXT BOX a person types into: its words are its placeholder.
    "input": {"text", "color", "font_size", "font_weight", "font_family",
              "letter_spacing", "opacity", "hidden"},
    "stroke": {"opacity", "hidden"},
}

# The background's MOTION is its own operation, not a property: the gradient's shape
# and colours were fitted to the original's pixels and stay those; only how they move
# over time is added, and the first frame must still be the fitted background.
# RELATIVE GEOMETRY IS THE ONLY KIND ANYONE MAY ASK FOR. Absolute positions were measured
# off the original's pixels and stay refused by name; "move it 40px left" or "make it 20%
# bigger" is a change to a measured value, and Aethron checks the result on screen.
MOVABLE = {"text", "button", "fill", "input", "picture", "rule"}
# GROWING THE PAGE COPIES WHAT IS ALREADY THERE. A fifth chip is the fourth chip's twin —
# same size, same fill, same corners — placed at the gap MEASURED between its siblings.
CLONABLE = {"text", "button", "fill", "input", "picture"}
RESIZABLE = {"button", "fill", "input", "picture"}

ALIVE_STYLES = ("drift", "breathe", "drift+breathe", "off")
# HOW VISIBLE, AS A VIEWER SEES IT: the mean change half a cycle in, in 0-255 levels.
# The first living background moved 5 levels at strength 0.5 and the owner could not see
# it move — so visibility is asked for by name and REACHED BY MEASURING, not guessed.
ALIVE_TARGETS = {"subtle": 4.0, "visible": 10.0, "strong": 18.0}
ALIVE_PROVISIONAL = {"subtle": 0.3, "visible": 0.65, "strong": 1.0}
ALIVE_RE = re.compile(r"<style data-ae-alive[^>]*>.*?</style>", re.S)
LABEL = {"text", "color", "font_size", "font_weight", "font_family",
         "letter_spacing"}

# THE PROPERTIES NOBODY MAY SET. Not a blacklist of dangerous strings —
# a statement about who owns what. Position and size were measured off
# the original's own pixels; a model's opinion about them is the exact
# failure this architecture exists to remove.
GEOMETRY = {"left", "top", "width", "height", "x", "y", "w", "h",
            "position", "transform", "z_index", "z-index"}

CSS = {
    "text": "font-size", "font_size": "font-size",
    "font_weight": "font-weight", "font_family": "font-family",
    "letter_spacing": "letter-spacing", "color": "color",
    "background": "background", "border_radius": "border-radius",
    "opacity": "opacity",
}

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
RGB = re.compile(r"^rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*"
                 r"(,\s*(0|1|0?\.\d+)\s*)?\)$")
FAMILY = re.compile(r"^[A-Za-z0-9 _-]{1,40}$")


class Refused(ValueError):
    """An edit that will not be applied, with the reason in words."""


# ──────────────────────────── reading ────────────────────────────────

def manifest(html: str) -> dict:
    """Every element the page is made of, as words and numbers.

    This is what a model is given instead of an image. It carries no
    pixels, so there is nothing in it to misjudge, and it carries the
    id of each element, so an edit can name one exactly rather than
    describing where it thinks the thing is.
    """
    out = []
    # THE SEMANTIC PASS MADE ELEMENTS THIS FUNCTION COULD NOT SEE.
    # A measured button is emitted as a real <button> (with its label in
    # a nested <span>) and a nav item as an <a>, which is the whole
    # point of it — and this regex matched `div|img` and closed on
    # `</div>`, so every one of them vanished. Measured on the Wezzi
    # rebuild: 27 elements in the file, 20 in the manifest, the seven
    # missing ones being the entire navigation and both buttons.
    # It is not a cosmetic loss. manifest() is what aethron_screen's
    # page_ir reads, so all six framework emitters were silently
    # shipping a page with no nav and no buttons; and it is what a model
    # is handed by aethron_edit, so "rebrand the call to action" named
    # an element that, as far as the tool was concerned, did not exist.
    for m in re.finditer(
            r"<(div|img|a|button|p|h[1-6]|span|section|nav|header|footer"
            r"|main|textarea|svg)"
            r"\b([^>]*)>", html):
        tag, attrs = m.group(1), m.group(2)
        idm = ID_RE.search(attrs)
        if not idm:
            continue
        inner = raw_inner = ""
        if tag != "img":
            close = html.find(f"</{tag}>", m.end())
            if close != -1:
                raw_inner = html[m.end():close]
                # a button holds its label in a span; the text of the
                # element is the text a reader sees, tags stripped
                inner = re.sub(r"<[^>]+>", "", raw_inner)
        st = STYLE_RE.search(attrs)
        style = _parse(st.group(1) if st else "")
        kind = _kind(attrs, style, tag)
        e = {"id": idm.group(1), "kind": kind, "tag": tag,
             "box": [_num(style.get("left")), _num(style.get("top")),
                     _num(style.get("width")), _num(style.get("height"))]}
        if kind in ("text", "button"):
            e["text"] = _unescape(inner)
            e["color"] = style.get("color")
            e["font_size"] = _num(style.get("font-size"))
        if kind == "input":
            ph = re.search(r'placeholder="([^"]*)"', attrs)
            e["text"] = _unescape(ph.group(1)) if ph else ""
            e["color"] = style.get("color")
            e["font_size"] = _num(style.get("font-size"))
        if kind == "ground" and tag == "main":
            pg = re.search(r"\.page\{[^}]*?background:(radial-gradient\(.*?)\}", html, re.S)
            e["background"] = (f"{pg.group(1).count('radial-gradient(')} fitted radial gradients"
                               if pg else "the page's measured ground")
            al = re.search(r'data-ae-alive="([^"]*)"', html)
            e["animation"] = al.group(1) if al else "none"
            e["note"] = ('animate it with {"id": "%s", "animate": {"style": "drift"|"breathe"|'
                         '"drift+breathe"|"off", "period": 4-120, "strength": 0.05-1}}' % idm.group(1))
        if kind == "button":
            e["background"] = style.get("background", "")
            e["border_radius"] = style.get("border-radius")
            # THE LABEL'S SIZE IS ON THE LABEL. A measured button sets
            # `font:inherit` on itself and puts the type on the nested
            # span, so reading font-size off the button alone returns
            # nothing and the label gets guessed from the pill's height.
            if not e.get("font_size"):
                fm = re.search(r"font-size:\s*([\d.]+)px", raw_inner)
                if fm:
                    e["font_size"] = float(fm.group(1))
        elif kind in ("fill", "rule"):
            bg = style.get("background", "")
            e["background"] = bg if len(bg) < 60 else "a measured gradient"
            if kind == "fill":
                e["border_radius"] = style.get("border-radius")
        elif kind == "picture":
            e["note"] = ("a crop of the original — its content cannot be "
                         "edited, only hidden or restyled")
        out.append(e)
    w = h = None
    # A PAGE AETHRON DID NOT BUILD HAS NO CANVAS RULE. The two patterns below are markers its
    # own generated pages carry, so every real page — a migration, a user's own site — answered
    # "the canvas could not be read" and the whole measured-change path stopped there. An
    # adopted page carries the size the BROWSER measured instead (aethron_adopt.canvas_of).
    mm = (re.search(r'name=["\']ae-canvas["\']\s+content=["\'](\d+)x(\d+)', html, re.I)
          or re.search(r'content=["\'](\d+)x(\d+)["\']\s+name=["\']ae-canvas', html, re.I)
          or re.search(r"html,body\{width:(\d+)px;height:(\d+)px", html)
          or re.search(r"\.page\{position:relative;width:(\d+)px;height:(\d+)px", html))
    if mm:
        w, h = int(mm.group(1)), int(mm.group(2))
    return {"canvas": {"w": w, "h": h}, "elements": out}


def _kind(attrs, style, tag="div"):
    cls = re.search(r'class="([^"]*)"', attrs)
    cls = cls.group(1).split() if cls else []
    if tag == "button":
        return "button"
    if tag == "textarea":
        return "input"
    if tag == "main" or "page" in cls:
        return "ground"
    if tag == "svg" or "strokes" in cls:
        return "stroke"
    if tag == "img" or "<img" in attrs or "src=" in attrs or "mk" in cls:
        return "picture"
    if "t" in cls:
        return "text"
    if "rl" in cls or _num(style.get("height")) == 1 or _num(style.get("width")) == 1:
        return "rule"
    if _num(style.get("z-index")) == 0:
        return "ground"
    return "fill"


def _parse(style):
    """A style attribute into properties — WITHOUT splitting inside ().

    Splitting on a plain ";" tears a data URI in half, because
    `url(data:image/png;base64,...)` carries one, and it tears a
    `linear-gradient(...)` apart at any rgb() it contains. The first
    version did exactly that and the carried background came back as
    the property `background-image: url(data:image/png` with the image
    itself parsed as a second, nonsense declaration.

    Depth-aware splitting is the whole fix: a separator only separates
    at the top level.
    """
    out, buf, depth = {}, [], 0
    for ch in str(style) + ";":
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == ";" and depth == 0:
            part = "".join(buf).strip()
            buf = []
            if not part:
                continue
            k, v = _cut(part)
            if k:
                out[k] = v
            continue
        buf.append(ch)
    return out


def _cut(part):
    """Split one declaration at its OWN colon, not a URL's scheme colon."""
    depth = 0
    for i, ch in enumerate(part):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            return part[:i].strip(), part[i + 1:].strip()
    return None, None


def _num(v):
    if not v:
        return None
    m = re.match(r"^(-?[\d.]+)", str(v))
    return float(m.group(1)) if m else None


def _unescape(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&amp;", "&"))


def _escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


# ──────────────────────────── writing ────────────────────────────────

def validate(edit, index):
    """Would this edit be allowed? Say why not, in a sentence."""
    if not isinstance(edit, dict):
        raise Refused("an edit must be an object with an id and a set")
    eid = edit.get("id")
    if eid not in index:
        raise Refused(f"there is no element called {eid!r} on this page")
    el = index[eid]
    if "clone" in edit:
        c = edit["clone"]
        if el["kind"] not in CLONABLE:
            raise Refused(f"{eid}: a {el['kind']} cannot be copied")
        if not isinstance(c, dict) or set(c) - {"text", "place", "dx", "dy"}:
            raise Refused(f"{eid}: clone takes text, and place (after, before, below, above) or dx and dy")
        if "place" in c and c["place"] not in ("after", "before", "below", "above"):
            raise Refused(f"{eid}: place must be after, before, below or above")
        for k in ("dx", "dy"):
            if k in c and (isinstance(c[k], bool) or not isinstance(c[k], (int, float)) or abs(c[k]) > 2000):
                raise Refused(f"{eid}: {k} must be a number of pixels")
        if "text" in c:
            _check(eid, "text", c["text"])
        return True
    if "move" in edit or "resize" in edit:
        return _check_geometry(eid, el, edit)
    if "animate" in edit:
        if el["kind"] != "ground":
            raise Refused(f"{eid}: only the page's background can be animated "
                          f"(this is a {el['kind']})")
        _check_animate(eid, edit["animate"])
        return True
    sets = edit.get("set")
    if not isinstance(sets, dict) or not sets:
        raise Refused(f"{eid}: nothing to set")
    allowed = ALLOWED.get(el["kind"], set())
    for k, v in sets.items():
        if k in GEOMETRY:
            raise Refused(
                f"{eid}: {k} is measured from the original's pixels and is "
                f"not yours to set — describe what you want changed about "
                f"the element instead")
        if k not in allowed:
            raise Refused(f"{eid}: a {el['kind']} has no {k!r} "
                          f"(it has {sorted(allowed)})")
        _check(eid, k, v)
    return True


def _check_geometry(eid, el, edit):
    if "move" in edit:
        mv = edit["move"]
        if el["kind"] not in MOVABLE:
            raise Refused(f"{eid}: a {el['kind']} cannot be moved")
        if not isinstance(mv, dict) or not mv or set(mv) - {"dx", "dy"}:
            raise Refused(f"{eid}: move takes dx and dy, in pixels")
        for k in ("dx", "dy"):
            v = mv.get(k, 0)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or abs(v) > 2000:
                raise Refused(f"{eid}: {k} must be a number of pixels")
    if "resize" in edit:
        rs = edit["resize"]
        if el["kind"] not in RESIZABLE:
            raise Refused(f"{eid}: a {el['kind']} cannot be resized — change its font_size instead")
        sc = rs.get("scale") if isinstance(rs, dict) else None
        if (not isinstance(rs, dict) or set(rs) - {"scale"} or isinstance(sc, bool)
                or not isinstance(sc, (int, float)) or not 0.5 <= sc <= 2.0):
            raise Refused(f"{eid}: resize takes scale, from 0.5 to 2")
    return True


def _check_animate(eid, a):
    if not isinstance(a, dict):
        raise Refused(f"{eid}: animate must be an object")
    unknown = set(a) - {"style", "period", "strength"}
    if unknown:
        raise Refused(f"{eid}: animate takes style, period and strength, "
                      f"not {sorted(unknown)}")
    if a.get("style", "drift") not in ALIVE_STYLES:
        raise Refused(f"{eid}: animate style must be one of {list(ALIVE_STYLES)}")
    period = a.get("period", 18)
    if isinstance(period, bool) or not isinstance(period, (int, float)) or not 4 <= period <= 120:
        raise Refused(f"{eid}: period is seconds per cycle, 4 to 120")
    strength = a.get("strength", 0.5)
    if isinstance(strength, str):
        if strength not in ALIVE_TARGETS:
            raise Refused(f"{eid}: strength is a number 0.05-1 or one of {list(ALIVE_TARGETS)}")
    elif isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0.05 <= strength <= 1:
        raise Refused(f"{eid}: strength is 0.05 (barely) to 1 (plainly moving), or "
                      f"{list(ALIVE_TARGETS)}")


def _check(eid, k, v):
    if k in ("color", "background"):
        if not (isinstance(v, str) and (HEX.match(v) or RGB.match(v))):
            raise Refused(f"{eid}: {k} must be #RRGGBB or rgb(...), "
                          f"got {v!r}")
    elif k == "font_size":
        if not isinstance(v, (int, float)) or not 4 <= v <= 400:
            raise Refused(f"{eid}: font_size must be a number from 4 to 400")
    elif k == "font_weight":
        if v not in (100, 200, 300, 400, 500, 600, 700, 800, 900):
            raise Refused(f"{eid}: font_weight must be one of 100..900")
    elif k == "font_family":
        if not (isinstance(v, str) and FAMILY.match(v)):
            raise Refused(f"{eid}: font_family must be a plain family name")
    elif k == "letter_spacing":
        if not isinstance(v, (int, float)) or abs(v) > 1:
            raise Refused(f"{eid}: letter_spacing is in em, -1 to 1")
    elif k == "opacity":
        if not isinstance(v, (int, float)) or not 0 <= v <= 1:
            raise Refused(f"{eid}: opacity must be 0 to 1")
    elif k == "border_radius":
        if not (v == "50%" or (isinstance(v, (int, float))
                               and 0 <= v <= 500)):
            raise Refused(f"{eid}: border_radius must be a number or '50%'")
    elif k == "hidden":
        if not isinstance(v, bool):
            raise Refused(f"{eid}: hidden must be true or false")
    elif k == "text":
        if not isinstance(v, str) or len(v) > 400:
            raise Refused(f"{eid}: text must be a string under 400 chars")
        if "<" in v or "${" in v or "`" in v:
            raise Refused(f"{eid}: text may not contain <, ` or ${{ "
                          f"(the same rule every other layer here obeys)")


def apply(html: str, edits, on_refusal="report"):
    """Apply the edits that are allowed; report the ones that are not.

    Nothing partial: an edit is applied whole or refused whole, and a
    refusal never stops the others. That is deliberate — a model that
    got one of six edits wrong should land the five that were right and
    be told precisely what to fix, which is how the copy pipeline has
    behaved since the first migration.
    """
    index = {e["id"]: e for e in manifest(html)["elements"]}
    applied, refused = [], []
    for edit in edits or []:
        try:
            validate(edit, index)
        except Refused as why:
            refused.append(str(why))
            if on_refusal == "raise":
                raise
            continue
        if "animate" in edit:
            a = edit["animate"]
            new, ok = animate_background(html, a.get("style", "drift"),
                                         a.get("period", 18), a.get("strength", 0.5))
        elif "move" in edit or "resize" in edit:
            new, ok = _regeometry(html, edit)
        elif "clone" in edit:
            new, ok, created = _clone(html, edit)
            if ok:
                edit = {**edit, "created": created}
        else:
            new, ok = _write(html, edit["id"], edit["set"])
        # AN EDIT THAT WAS NOT WRITTEN WAS NOT APPLIED. The writer used to match only
        # <div> and <img>, so an edit to a heading, a link or a button changed nothing
        # and was still counted as applied — a green light over an untouched page.
        if not ok:
            why = (f"{edit['id']}: could not be written into the page's markup, "
                   f"so nothing was changed")
            refused.append(why)
            if on_refusal == "raise":
                raise Refused(why)
            continue
        html = new
        applied.append(edit)
    return html, applied, refused


def _restyle(tag, sets):
    st = STYLE_RE.search(tag)
    style = _parse(st.group(1) if st else "")
    for k, v in sets.items():
        if k == "hidden":
            style["display"] = "none" if v else "block"
        elif k == "font_size":
            style["font-size"] = f"{float(v):.1f}px"
        elif k == "letter_spacing":
            style["letter-spacing"] = f"{float(v)}em"
        elif k == "border_radius":
            style["border-radius"] = v if v == "50%" else f"{float(v)}px"
        elif k == "font_family":
            style["font-family"] = f"'{v}',sans-serif"
        else:
            style[CSS[k]] = str(v)
    flat = ";".join(f"{a}:{b}" for a, b in style.items())
    if st:
        return tag.replace(st.group(0), f'style="{flat}"', 1)
    return tag[:-1] + f' style="{flat}">'


def _write(html, eid, sets):
    """Write one edit into the element's own markup. Returns (html, written).

    A BUTTON IS A BOX WITH A LABEL: its fill and corners go on the button, its words and
    type on the label inside it. A TEXT BOX's words are its placeholder. Anything that
    cannot take the change says so by returning written=False, never by pretending.
    """
    m = re.search(r'<([a-zA-Z0-9]+)\b[^>]*\bdata-ae-id="' + re.escape(eid)
                  + r'"[^>]*>', html)
    if not m:
        return html, False
    tag, head = m.group(1).lower(), m.group(0)
    if tag == "button":
        close = html.find("</button>", m.end())
        if close == -1:
            return html, False
        inner = html[m.end():close]
        lab = {k: v for k, v in sets.items() if k in LABEL}
        box = {k: v for k, v in sets.items() if k not in LABEL}
        if lab:
            sm = re.search(r"(<span\b[^>]*>)([^<]*)(</span>)", inner)
            if not sm:
                return html, False
            styled = {k: v for k, v in lab.items() if k != "text"}
            span = _restyle(sm.group(1), styled) if styled else sm.group(1)
            if "text" in lab:
                # A NEW WORD IS CENTRED IN ITS BUTTON BY THE BROWSER, not left where the old
                # word's measured offset put it: "Create my app" set at the offset measured
                # for "Generate" ran twelve pixels out of the button's right side.
                st = STYLE_RE.search(span)
                style = _parse(st.group(1) if st else "")
                style["left"], style["width"], style["text-align"] = "0px", "100%", "center"
                flat = ";".join(f"{a}:{b}" for a, b in style.items())
                span = (span.replace(st.group(0), f'style="{flat}"', 1) if st
                        else span[:-1] + f' style="{flat}">')
            words = _escape(lab["text"]) if "text" in lab else sm.group(2)
            inner = inner[:sm.start()] + span + words + sm.group(3) + inner[sm.end():]
        head2 = _restyle(head, box) if box else head
        return html[:m.start()] + head2 + inner + html[close:], True
    if tag == "textarea":
        head2 = head
        if "text" in sets:
            words = _escape(sets["text"])
            head2 = re.sub(r'placeholder="[^"]*"', f'placeholder="{words}"', head2)
            head2 = re.sub(r'aria-label="[^"]*"', f'aria-label="{words}"', head2)
        rest = {k: v for k, v in sets.items() if k != "text"}
        if rest:
            head2 = _restyle(head2, rest)
            if "color" in rest and "--ph:" in head2:
                head2 = re.sub(r"--ph:[^;\"]*", f"--ph:{rest['color']}", head2)
        return html[:m.start()] + head2 + html[m.end():], True
    rest = {k: v for k, v in sets.items() if k != "text"}
    head2 = _restyle(head, rest) if rest else head
    if "text" in sets:
        if tag in ("img", "svg", "main"):
            return html, False
        close = html.find(f"</{tag}>", m.end())
        if close == -1 or "<" in html[m.end():close]:
            return html, False
        return html[:m.start()] + head2 + _escape(sets["text"]) + html[close:], True
    return html[:m.start()] + head2 + html[m.end():], True


def riders(html, eid):
    """Everything that sits ON this element: a card carries its text box, its chips and
    its button. Measured from the element list — a box inside the box, or a line that
    starts inside it — and nothing else is dragged along."""
    elements = manifest(html)["elements"]
    host = next((e for e in elements if e["id"] == eid), None)
    if not host or None in host["box"][:2] or not host["box"][2] or not host["box"][3]:
        return []
    x, y, w, h = host["box"]
    out = []
    for e in elements:
        if e["id"] == eid or e["kind"] in ("ground", "stroke"):
            continue
        ex, ey, ew, eh = e["box"]
        if ex is None or ey is None:
            continue
        if x <= ex and y <= ey and ex + (ew or 0) <= x + w and ey + (eh or 0) <= y + h:
            out.append(e["id"])
    return out


def _shift(html, eid, dx, dy, dw=0.0, dh=0.0):
    m = re.search(r'<([a-zA-Z0-9]+)\b[^>]*\bdata-ae-id="' + re.escape(eid) + r'"[^>]*>', html)
    if not m:
        return html, False
    head = m.group(0)
    st = STYLE_RE.search(head)
    style = _parse(st.group(1) if st else "")
    if _num(style.get("left")) is None or _num(style.get("top")) is None:
        return html, False
    style["left"] = f"{_num(style['left']) + dx:.1f}px"
    style["top"] = f"{_num(style['top']) + dy:.1f}px"
    if dw or dh:
        if _num(style.get("width")) is None or _num(style.get("height")) is None:
            return html, False
        style["width"] = f"{_num(style['width']) + dw:.1f}px"
        style["height"] = f"{_num(style['height']) + dh:.1f}px"
    flat = ";".join(f"{a}:{b}" for a, b in style.items())
    head2 = head.replace(st.group(0), f'style="{flat}"', 1) if st else head[:-1] + f' style="{flat}">'
    return html[:m.start()] + head2 + html[m.end():], True


def _regeometry(html, edit):
    """Apply a relative move or resize to an element and to everything riding on it."""
    start, eid = html, edit["id"]
    el = next((e for e in manifest(html)["elements"] if e["id"] == eid), None)
    if not el:
        return start, False
    carried = riders(html, eid)
    if "move" in edit:
        dx, dy = float(edit["move"].get("dx", 0)), float(edit["move"].get("dy", 0))
        for i in [eid] + carried:
            html, ok = _shift(html, i, dx, dy)
            if not ok:
                return start, False
        # ONE EDIT MAY CARRY BOTH. The first real request sent {"move": ..., "resize": ...}
        # in a single object; only the move was written and the edit was still reported
        # as applied. Both halves are written, or neither is.
        if "resize" not in edit:
            return html, True
        html, ok = _regeometry(html, {"id": eid, "resize": edit["resize"]})
        return (html, True) if ok else (start, False)
    s = float(edit["resize"]["scale"])
    x, y, w, h = el["box"]
    if None in (x, y, w, h):
        return start, False
    nw, nh = w * s, h * s
    html, ok = _shift(html, eid, -(nw - w) / 2, -(nh - h) / 2, nw - w, nh - h)
    if not ok:
        return start, False
    m = re.search(r'(<[a-zA-Z0-9]+\b[^>]*\bdata-ae-id="' + re.escape(eid) + r'"[^>]*>)', html)
    head = m.group(1)
    st = STYLE_RE.search(head)
    style = _parse(st.group(1))
    rad = _num(style.get("border-radius"))
    if rad is not None and rad >= 0.45 * min(w, h):          # a pill stays a pill
        style["border-radius"] = f"{min(nw, nh) / 2:.1f}px"
        html = html.replace(head, head.replace(st.group(0), 'style="' + ";".join(f"{a}:{b}" for a, b in style.items()) + '"', 1), 1)
    if el["kind"] == "button":
        close = html.find("</button>", html.find(f'data-ae-id="{eid}"'))
        seg_start = html.find(">", html.find(f'data-ae-id="{eid}"')) + 1
        inner = html[seg_start:close]
        sm = re.search(r"<span\b[^>]*>", inner)
        if sm:
            sst = STYLE_RE.search(sm.group(0))
            sp = _parse(sst.group(1) if sst else "")
            sp["left"], sp["width"], sp["text-align"] = "0px", "100%", "center"
            if _num(sp.get("top")) is not None:
                sp["top"] = f"{_num(sp['top']) + (nh - h) / 2:.1f}px"
            flat = ";".join(f"{a}:{b}" for a, b in sp.items())
            span = sm.group(0).replace(sst.group(0), f'style="{flat}"', 1) if sst else sm.group(0)[:-1] + f' style="{flat}">'
            inner = inner[:sm.start()] + span + inner[sm.end():]
            html = html[:seg_start] + inner + html[close:]
    cx, cy = x + w / 2, y + h / 2
    for r in carried:
        re_el = next((e for e in manifest(html)["elements"] if e["id"] == r), None)
        if re_el and re_el["box"][0] is not None:
            rx, ry = re_el["box"][0], re_el["box"][1]
            html, _ok = _shift(html, r, (rx - cx) * (s - 1), (ry - cy) * (s - 1))
    return html, True


def _outer(html, eid):
    """The element's whole markup, from its opening tag to its own closing tag."""
    m = re.search(r'<([a-zA-Z0-9]+)\b[^>]*\bdata-ae-id="' + re.escape(eid) + r'"[^>]*>', html)
    if not m:
        return None
    tag = m.group(1).lower()
    if tag in ("img", "input"):
        return m.start(), m.end()
    close = html.find(f"</{tag}>", m.end())
    return (m.start(), close + len(tag) + 3) if close != -1 else None


def _clone(html, edit):
    """Copy an element — and whatever rides on it — beside itself. Returns (html, ok, new ids).

    The spacing is MEASURED: among siblings of the same kind and height on the same row
    (or column), the median gap between neighbours; the copy goes one gap past the last
    of them. A caller that knows better may give dx/dy instead.
    """
    start, eid, c = html, edit["id"], edit["clone"]
    elements = manifest(html)["elements"]
    el = next((e for e in elements if e["id"] == eid), None)
    if not el or el["box"][0] is None or el["box"][1] is None:
        return start, False, []
    x, y, w, h = el["box"]
    if "dx" in c or "dy" in c:
        dx, dy = float(c.get("dx", 0)), float(c.get("dy", 0))
    else:
        if not w or not h:
            return start, False, []
        place = c.get("place", "after")
        same = [e for e in elements if e["kind"] == el["kind"] and e["box"][2] and e["box"][3]
                and abs(e["box"][3] - h) <= 4]
        if place in ("after", "before"):
            line = sorted([e for e in same if abs(e["box"][1] - y) <= 4], key=lambda e: e["box"][0])
            gaps = [b["box"][0] - (a["box"][0] + a["box"][2]) for a, b in zip(line, line[1:])]
            gap = sorted(gaps)[len(gaps) // 2] if gaps else 12.0
            if place == "after":
                last = line[-1]["box"]
                dx, dy = last[0] + last[2] + gap - x, 0.0
            else:
                first = line[0]["box"]
                dx, dy = first[0] - gap - w - x, 0.0
        else:
            col = sorted([e for e in same if abs(e["box"][0] - x) <= 4], key=lambda e: e["box"][1])
            gaps = [b["box"][1] - (a["box"][1] + a["box"][3]) for a, b in zip(col, col[1:])]
            gap = sorted(gaps)[len(gaps) // 2] if gaps else 12.0
            if place == "below":
                last = col[-1]["box"]
                dx, dy = 0.0, last[1] + last[3] + gap - y
            else:
                first = col[0]["box"]
                dx, dy = 0.0, first[1] - gap - h - y
    taken = {e["id"] for e in elements}
    created = []
    for src in [eid] + riders(html, eid):
        span = _outer(html, src)
        if not span:
            return start, False, []
        n = 1
        while f"{src}-c{n}" in taken:
            n += 1
        new_id = f"{src}-c{n}"
        taken.add(new_id)
        markup = html[span[0]:span[1]].replace(f'data-ae-id="{src}"', f'data-ae-id="{new_id}"', 1)
        html = html[:span[1]] + markup + html[span[1]:]
        html, ok = _shift(html, new_id, dx, dy)
        if not ok:
            return start, False, []
        created.append(new_id)
    if "text" in c:
        html, ok = _write(html, created[0], {"text": c["text"]})
        if not ok:
            return start, False, []
    return html, True, created


def _split_top(s):
    out, buf, depth = [], [], 0
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return out


def animate_background(html, style="drift", period=18.0, strength=0.5):
    """Make the fitted background move. Returns (html, written).

    THE BACKGROUND IS CODE, SO ITS MOTION IS CODE. Each fitted layer is a glow with a
    size and a centre; each becomes four registered custom properties, and a keyframe
    loop moves them — drifting the centre round a small tilted ellipse, breathing the
    size, or both. Periods differ layer to layer so the sky never visibly repeats.

    THE FIRST FRAME IS THE FITTED BACKGROUND, EXACTLY: every loop starts and ends on the
    measured values, so a page at rest still matches the screenshot, and anyone who has
    asked their system for less motion gets the still page. The motion lives in its own
    <style> block, so animating again replaces it and "off" removes it without trace.
    """
    import math
    html = ALIVE_RE.sub("", html)
    if style == "off":
        return html, True
    strength = ALIVE_PROVISIONAL[strength] if isinstance(strength, str) else float(strength)
    pm = re.search(r"\.page\{[^}]*?background:(radial-gradient\(.*?)\}", html, re.S)
    if not pm or "</head>" not in html:
        return html, False
    parts = _split_top(pm.group(1))
    layers, base = [], []
    for part in parts:
        if not part.startswith("radial-gradient("):
            base.append(part)
            continue
        pieces = _split_top(part[len("radial-gradient("):-1])
        geo = re.match(r"ellipse\s+(-?[\d.]+)%\s+(-?[\d.]+)%\s+at\s+(-?[\d.]+)%"
                       r"\s+(-?[\d.]+)%$", pieces[0])
        if not geo:
            return html, False
        layers.append((tuple(float(g) for g in geo.groups()), ", ".join(pieces[1:])))
    if not layers:
        return html, False
    drift = style in ("drift", "drift+breathe")
    breathe = style in ("breathe", "drift+breathe")
    props, frames, anims, bg, rootvars = [], [], [], [], []
    steps = 8
    for n, ((w, h, x, y), stops) in enumerate(layers):
        # A SMALL LAYER IS DETAIL, NOT ATMOSPHERE. The fit sometimes spends a tiny glow on a
        # local highlight — one sat under the logo — and drifting it walked a bright smudge
        # across the header. Layers under a tenth of the page stay exactly where they were fitted.
        if max(w, h) < 10.0:
            bg.append(f"radial-gradient(ellipse {w:.2f}% {h:.2f}% at {x:.2f}% {y:.2f}%, {stops})")
            continue
        v = f"--ae{n}"
        for suf, val in (("w", w), ("h", h), ("x", x), ("y", y)):
            props.append(f"@property {v}{suf}{{syntax:'<percentage>';inherits:false;"
                         f"initial-value:{val:.2f}%}}")
            rootvars.append(f"{v}{suf}:{val:.2f}%")
        th = math.radians(n * 137.508)
        amp = min(14.0 * strength, 0.4 * max(w, h))   # a small glow does not travel far
        kf = []
        for k in range(steps + 1):
            ph = 2 * math.pi * k / steps
            dx = dy = 0.0
            if drift:
                px, py = amp * math.cos(ph) - amp, 0.6 * amp * math.sin(ph)
                dx = px * math.cos(th) - py * math.sin(th)
                dy = px * math.sin(th) + py * math.cos(th)
            sc = 1 + (0.28 * strength * math.sin(ph) if breathe else 0.0)
            kf.append(f"{100 * k / steps:.1f}%{{{v}w:{w * sc:.2f}%;{v}h:{h * sc:.2f}%;"
                      f"{v}x:{x + dx + 0.0:.2f}%;{v}y:{y + dy + 0.0:.2f}%}}")
        frames.append(f"@keyframes ae-bg{n}{{{''.join(kf)}}}")
        anims.append(f"ae-bg{n} {float(period) * (1 + 0.23 * n):.2f}s linear infinite"
                     + (" reverse" if n % 2 else ""))
        bg.append(f"radial-gradient(ellipse var({v}w) var({v}h) at var({v}x) var({v}y), {stops})")
    block = (f'<style data-ae-alive="{style};period={float(period):g};strength={float(strength):g}">'
             + "".join(props) + "".join(frames)
             + f".page{{{';'.join(rootvars)};background:{', '.join(bg + base)};"
             + f"animation:{', '.join(anims)}}}"
             + "@media (prefers-reduced-motion:reduce){.page{animation:none}}</style>")
    return html.replace("</head>", block + "</head>", 1), True


def tune_alive(html, style, period, target, workdir, w, h):
    """The strength that makes the motion as visible as asked, FOUND BY MEASURING.

    Render the still page once; then render half a cycle in at a strength, measure the
    mean change, and move the strength by the ratio to the target. Motion grows close to
    linearly with strength, so it lands in two or three renders. Returns
    (strength, [(strength tried, change measured), ...]). With no browser it returns the
    provisional strength and says so, rather than pretending to have measured.
    """
    import aethron_figma_grade as GR
    import aethron_gradient as G
    import aethron_vision as V
    from pathlib import Path
    goal = ALIVE_TARGETS[target]
    if not GR.find_browser():
        return ALIVE_PROVISIONAL[target], [("provisional — no browser to measure with", None)]
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    made = []

    def shoot(page, name):
        f = work / f"_tune_{name}.html"
        f.write_text(page)
        png = f.with_suffix(".png")
        GR.shoot(f, w, h, png)
        made.extend([f, png])
        return V.load(png)
    still = shoot(ALIVE_RE.sub("", html), "still")
    tried, s = [], 0.5
    for _ in range(4):
        page, ok = animate_background(html, style, period, s)
        if not ok:
            break
        page = page.replace("</head>", "<style>.page{animation-play-state:paused!important;"
                            f"animation-delay:-{float(period) / 2:.3f}s!important}}</style></head>", 1)
        c = G.compare(still, shoot(page, f"s{len(tried)}"), step=2, tol=12, border=0)["mean_abs"]
        tried.append((round(s, 3), round(c, 2)))
        if abs(c - goal) <= 0.15 * goal or (s >= 1.0 and c < goal):
            break
        nxt = max(0.05, min(1.0, s * goal / max(c, 0.1)))
        if abs(nxt - s) < 0.02:
            break
        s = nxt
    for f in made:
        try:
            f.unlink()
        except OSError:
            pass
    # RETURN WHAT WAS MEASURED, never the next guess: the closest strength actually rendered
    best = min(tried, key=lambda t: abs(t[1] - goal)) if tried else (ALIVE_PROVISIONAL[target], None)
    return best[0], tried


def prove_alive(html, workdir, w, h, tol=12):
    """Render the moving background and MEASURE it: frame 0 against the still page, and
    frames a quarter and half a cycle in. A check that cannot run reports SKIPPED."""
    import aethron_figma_grade as GR
    import aethron_gradient as G
    import aethron_vision as V
    from pathlib import Path
    m = re.search(r'data-ae-alive="[^"]*period=([\d.]+)', html)
    if not m:
        return {"verdict": "SKIPPED", "why": "the page is not animated"}
    if not GR.find_browser():
        return {"verdict": "SKIPPED", "why": "no browser to render with — UNVERIFIED"}
    period = float(m.group(1))
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    made = []

    def frame(page, name, t=None):
        if t is not None:
            page = page.replace("</head>", "<style>.page{animation-play-state:paused!important;"
                                f"animation-delay:-{t:.3f}s!important}}</style></head>", 1)
        f = work / f"_alive_{name}.html"
        f.write_text(page)
        png = f.with_suffix(".png")
        GR.shoot(f, w, h, png)
        made.extend([f, png])
        return V.load(png)
    still = frame(ALIVE_RE.sub("", html), "still")
    f0 = frame(html, "t0", 0.0)
    fq = frame(html, "quarter", period / 4)
    fh = frame(html, "half", period / 2)
    same = G.compare(still, f0, step=2, tol=tol, border=0)
    q = G.compare(still, fq, step=2, tol=tol, border=0)
    hh = G.compare(still, fh, step=2, tol=tol, border=0)
    for f in made:
        try:
            f.unlink()
        except OSError:
            pass
    moved = max(q["mean_abs"], hh["mean_abs"])
    ok = same["within_tol"] >= 0.999 and moved >= 0.5
    visibility = ("barely noticeable" if moved < 3 else "subtle" if moved < 8
                  else "clearly visible" if moved < 16 else "strong")
    return {"verdict": "PASS" if ok else "FAIL",
            "frame0_matches_still": round(same["within_tol"] * 100, 2),
            "quarter_cycle_mean_change": round(q["mean_abs"], 2),
            "half_cycle_mean_change": round(hh["mean_abs"], 2),
            "half_cycle_within_12": round(hh["within_tol"] * 100, 1),
            "visibility": visibility,
            "why": ("" if ok else "the first frame is not the fitted background"
                    if same["within_tol"] < 0.999 else "the background does not move")}


# ───────────────────────── the guardrail ─────────────────────────────

def touched_only(before_png, after_png, boxes, tol=8, slack=6):
    """Did the edit change ONLY what it named?

    THE CHECK THAT MAKES A WEAK MODEL SAFE HERE. Every other guard in
    this file reads the edit; this one reads the RESULT. A page that has
    been measured into place is easy to disturb — a longer string
    reflows, a bigger size pushes a neighbour, an opacity lands on the
    wrong layer — and none of that shows up in the edit itself.

    So render before and after, and require every pixel outside the
    named elements to be unchanged. An edit that moved something it was
    not asked to move is a failed edit however reasonable it sounded.

    Returns (ok, damage) where damage is the rectangle that changed and
    should not have, so the report can name it.
    """
    import aethron_vision as V
    a, b = V.load(before_png), V.load(after_png)
    if (a.w, a.h) != (b.w, b.h):
        return False, {"why": "the page changed size"}

    def inside(x, y):
        for bx, by, bw, bh in boxes:
            if (bx - slack <= x <= bx + bw + slack
                    and by - slack <= y <= by + bh + slack):
                return True
        return False

    x0 = y0 = 1 << 30
    x1 = y1 = -1
    n = 0
    for y in range(0, a.h, 2):
        for x in range(0, a.w, 2):
            if V._dist(a.rgb(x, y), b.rgb(x, y)) <= tol or inside(x, y):
                continue
            n += 1
            x0, y0 = min(x0, x), min(y0, y)
            x1, y1 = max(x1, x), max(y1, y)
    if not n:
        return True, None
    return False, {"pixels": n * 4, "box": [x0, y0, x1 - x0 + 1, y1 - y0 + 1],
                   "why": "the edit changed part of the page it did not name"}


# ────────────────────────── the model's brief ────────────────────────

PROMPT = """You are editing a page that has ALREADY been rebuilt, exactly,
from a screenshot. It is correct. Your job is not to build it and not to
improve it — only to make the change the user asked for.

You are given the page's ELEMENTS as a list. Each has an id.

Return JSON and nothing else:

  {"edits": [{"id": "<id>", "set": {"<property>": <value>}}, ...],
   "note": "<one line: what you changed, or what you could not>"}

WHAT YOU MAY SET
  text elements     text, color, font_size, font_weight, font_family,
                    letter_spacing, opacity, hidden
  button elements   the same as text (its label) plus background,
                    border_radius
  input elements    text (its placeholder), color, font_size, font_weight,
                    font_family, letter_spacing, opacity, hidden
  stroke elements   opacity, hidden
  the ground        {"id": "<its id>", "animate": {"style": "drift" |
                    "breathe" | "drift+breathe" | "off", "period": 4-120
                    seconds, "strength": 0.05-1 or "subtle" | "visible" |
                    "strong"}} makes the background move;
                    its shape and colours stay the fitted ones
  fill elements     background, border_radius, opacity, hidden
  rule elements     background, opacity, hidden
  picture elements  opacity, border_radius, hidden

MOVING AND RESIZING — RELATIVE ONLY
  {"id": "...", "move": {"dx": pixels, "dy": pixels}}   text, buttons, boxes,
                                                       text boxes, pictures, rules
  {"id": "...", "resize": {"scale": 0.5-2}}             buttons, boxes, text boxes,
                                                       pictures (not text: use font_size)
  The element list gives every box; use it for arithmetic — "centre it" means
  moving by the difference between its centre and the canvas centre. Whatever
  sits on an element moves with it. Aethron renders the result and refuses it if
  anything else is covered, buried or pushed off the page.

ADDING WHAT THE PAGE DOES NOT HAVE — BY COPYING WHAT IT DOES
  {"id": "...", "clone": {"text": "new words", "place": "after" | "before" |
                          "below" | "above"}}
  The copy is the element's twin (same size, fill, corners, type) and whatever
  rides on it is copied too; place uses the gap measured between its siblings.
  To add a fifth chip, clone the last chip with place "after".

WHAT YOU MAY NOT SET, EVER
  left, top, width, height, position, transform, z-index.
  Those were measured from the original's own pixels. Models reading
  sizes off an image are right about 8% of the time, which is why this
  page is right and why you are not being asked. If the change the user
  wants genuinely needs an element moved or resized, do not guess it —
  say so in `note` and leave it out.

RULES
  * Colours are #RRGGBB or rgb(...). Nothing else parses.
  * Change the fewest elements that satisfy the request.
  * A rebrand means changing the WORDS, in every element that carries
    the old name — check the whole list, not just the obvious one.
  * If you are unsure an element is the right one, leave it and say so.
    An honest gap is cheap; a wrong edit is not.
"""


def brief(html, request):
    """Everything a model needs for one edit, and nothing else."""
    man = manifest(html)
    return (PROMPT + "\n\nTHE USER ASKED FOR:\n" + request
            + "\n\nTHE ELEMENTS:\n" + json.dumps(man, indent=1))


ASK_RULES = """

Return ONLY the JSON object — no markdown fences, no commentary.
An edit is either {"id": "...", "set": {...}} or, for the page's ground,
{"id": "...", "animate": {...}}. If the user wants the background to move, or
to move more, use animate on the ground; "more visible" means strength
"visible", and "a lot" means "strong".
"""

FREEZE = "<style>*{animation-play-state:paused!important;animation-delay:0s!important}</style>"


def _parse_reply(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    a, b = t.find("{"), t.rfind("}")
    if a == -1 or b <= a:
        return None
    try:
        plan = json.loads(t[a:b + 1])
    except ValueError:
        return None
    return plan if isinstance(plan, dict) and isinstance(plan.get("edits"), list) else None


def verify_collateral(before, after, ids, workdir, w, h, tol=16):
    """Did the edits change only what they named — ON SCREEN?

    Each named element's footprint is MEASURED, before and after, by rendering the page
    with that element hidden and seeing which pixels it owned. A changed pixel is allowed
    inside an element's footprint before the edit, or inside its footprint after the edit
    where the page underneath was only background. Anything else is damage: an edit that
    grew a heading over a button changed the button, however legal the edit was.
    """
    import aethron_figma_grade as GR
    import aethron_vision as V
    from pathlib import Path
    if not GR.find_browser():
        return {"ok": None, "why": "no browser to render with — UNVERIFIED"}
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    made = []

    def shoot(page, name, hide=None):
        extra = FREEZE + (f"<style>{hide}{{visibility:hidden!important}}</style>" if hide else "")
        f = work / f"_edit_{name}.html"
        f.write_text(page.replace("</head>", extra + "</head>", 1))
        png = f.with_suffix(".png")
        GR.shoot(f, w, h, png)
        made.extend([f, png])
        return png
    b_png, a_png = shoot(before, "before"), shoot(after, "after")
    b, a = V.load(b_png), V.load(a_png)
    ground = V.load(shoot(before, "ground", hide='[data-ae-id]:not([data-ae-id="bg"])'))
    # WHAT AN ELEMENT SITS ON IS ITS GROUND. A button moved across the card it sits on
    # covers card pixels, and the first version called that damage to the card; covering
    # a neighbouring chip still is. So the ground for "only covered background" keeps the
    # page and every surface the named elements ride on.
    # A HOST IS WHAT AN ELEMENT SAT ON BEFORE THE EDIT, AND ONLY A SURFACE CAN BE ONE. The
    # first version asked where the element landed, so a chip copied straight on top of a
    # button "rode on" the button and covering it was allowed. A copy's host is its
    # original's host; a button hosts nothing.
    hosts = set()
    sources = {re.sub(r"-c\d+$", "", i) for i in ids}
    for e in manifest(before)["elements"]:
        if e["id"] in ids or e["kind"] != "fill":
            continue
        carried = riders(before, e["id"])
        if any(src in carried for src in sources):
            hosts.add(e["id"])
    keep = "".join(f':not([data-ae-id="{k}"])' for k in ["bg"] + sorted(hosts))
    ground_hosts = V.load(shoot(before, "ground_hosts", hide=f"[data-ae-id]{keep}")) if hosts else ground

    def footprint(page, full, eid, name):
        hid = V.load(shoot(page, name, hide=f'[data-ae-id="{eid}"]'))
        return {(x, y) for y in range(0, h, 2) for x in range(0, w, 2)
                if V._dist(full.rgb(x, y), hid.rgb(x, y)) > tol}
    own_before, own_after = set(), set()
    buried = 0
    for i, eid in enumerate(ids):
        own_before |= footprint(before, b, eid, f"b{i}")
        seen_after = footprint(after, a, eid, f"a{i}")
        own_after |= seen_after
        # AN ELEMENT PUSHED UNDER ANOTHER IS DAMAGED TOO, and hiding it cannot show that —
        # the part underneath never reached the screen. So render it ALONE on the ground:
        # whatever it draws there that the full page does not show is buried.
        alone = V.load(shoot(after, f"i{i}",
                             hide=f'[data-ae-id]:not([data-ae-id="bg"]):not([data-ae-id="{eid}"])'))
        drawn = {(x, y) for y in range(0, h, 2) for x in range(0, w, 2)
                 if V._dist(alone.rgb(x, y), ground.rgb(x, y)) > tol}
        buried += sum(1 for (x, y) in drawn
                      if not any((x + dx, y + dy) in seen_after for dx in (-2, 0, 2) for dy in (-2, 0, 2)))

    def near(s, x, y):
        return any((x + dx, y + dy) in s for dx in (-2, 0, 2) for dy in (-2, 0, 2))
    bad = []
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if V._dist(b.rgb(x, y), a.rgb(x, y)) <= tol:
                continue
            if near(own_before, x, y):
                continue
            if near(own_after, x, y) and V._dist(b.rgb(x, y), ground_hosts.rgb(x, y)) <= tol:
                continue
            bad.append((x, y))
    for f in made:
        try:
            f.unlink()
        except OSError:
            pass
    if buried >= 40:
        return {"ok": False, "pixels": buried * 4,
                "why": "the edit pushed an element underneath another one"}
    # NOTHING MAY BE PUSHED OFF THE PAGE: an element that now touches an edge it did not
    # touch before has been cut off, and a cut-off element changes nothing it can be blamed for.
    def edges(s_):
        return {("l" if x <= 2 else "r" if x >= w - 3 else "") + ("t" if y <= 2 else "b" if y >= h - 3 else "")
                for x, y in s_} - {""}
    if edges(own_after) - edges(own_before):
        return {"ok": False, "why": "the edit pushed an element off the edge of the page"}
    if not bad:
        return {"ok": True}
    xs, ys = [p[0] for p in bad], [p[1] for p in bad]
    box = [min(xs), min(ys), max(xs) - min(xs) + 2, max(ys) - min(ys) + 2]
    # SAY WHAT WAS HIT, so whoever asked can choose another way: a refusal that names the
    # Generate button is advice; "changed part of the page" is only a no.
    covered = []
    for e in manifest(before)["elements"]:
        if (e["id"] in ids or e["id"] in hosts or e["kind"] in ("ground", "stroke")
                or e["box"][0] is None or e["box"][1] is None):
            continue
        ex, ey, ew, eh = e["box"]
        fs = e.get("font_size") or 12
        ew = ew or len(e.get("text") or "x") * fs * 0.55
        eh = eh or fs * 1.3
        if ex < box[0] + box[2] and box[0] < ex + ew and ey < box[1] + box[3] and box[1] < ey + eh:
            covered.append(f"{e['id']} ({e['kind']}" + (f" '{e['text'][:28]}'" if e.get("text") else "") + ")")
    why = "the edit changed part of the page it did not name"
    if covered:
        why += ": it would cover " + ", ".join(covered[:4])
    return {"ok": False, "pixels": len(bad) * 4, "box": box, "covers": covered, "why": why}


def _rgb_of(value):
    v = (value or "").strip()
    if HEX.match(v):
        return tuple(int(v[i:i + 2], 16) for i in (1, 3, 5))
    m = re.match(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", v)
    return tuple(int(g) for g in m.groups()) if m else None


def _contrast(a, b):
    def rel(c):
        ch = [x / 255 for x in c]
        ch = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in ch]
        return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
    la, lb = sorted((rel(a), rel(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _legible_labels(html, applied):
    """A BUTTON REPAINTED UNDER ITS OLD LABEL COLOUR IS A BUTTON NOBODY CAN READ. The first
    real request turned the button green and left its pale grey label on it. When a fill
    changes and the label colour was not asked for, the label takes whichever of white or
    near-black reads better on the new fill — by the WCAG contrast ratio, measured."""
    notes = []
    index = {e["id"]: e for e in manifest(html)["elements"]}
    for e in applied:
        sets = e.get("set") or {}
        el = index.get(e.get("id"))
        if not el or el["kind"] != "button" or "background" not in sets or "color" in sets:
            continue
        fill = _rgb_of(sets["background"])
        m = re.search(r'data-ae-id="' + re.escape(e["id"]) + r'"[^>]*>\s*<span\b[^>]*color:\s*([^;"]+)', html)
        label = _rgb_of(m.group(1)) if m else None
        if not fill or not label:
            continue
        now = _contrast(fill, label)
        if now >= 4.5:
            continue
        choice = max(((255, 255, 255), (17, 17, 17)), key=lambda c: _contrast(fill, c))
        hexv = "#%02X%02X%02X" % choice
        html, _ok = _write(html, e["id"], {"color": hexv})
        notes.append(f"{e['id']}: label set to {hexv} so it reads on the new fill "
                     f"(contrast {now:.1f} -> {_contrast(fill, choice):.1f})")
    return html, notes


def _fit_words(before, after, applied, workdir, canvas, steps=5):
    """A NEW WORD THAT DOES NOT FIT ITS BOX IS SET SMALLER, NOT SPILLED.

    Only the elements whose words changed are resized, a step at a time, and each step is
    checked on screen again; the first size at which nothing outside the named elements
    changes is kept. If none fits, the caller refuses — a label is never allowed to run
    onto the card around it.
    """
    worded = [e["id"] for e in applied if "set" in e and "text" in e["set"]]
    if not worded:
        return None
    index = {e["id"]: e for e in manifest(after)["elements"]}
    sizes = {i: index[i]["font_size"] for i in worded if index.get(i, {}).get("font_size")}
    if not sizes:
        return None
    named = [e["id"] for e in applied if "set" in e]
    for step in range(1, steps + 1):
        k = 0.92 ** step
        trial, _ap, rf = apply(after, [{"id": i, "set": {"font_size": round(max(4.0, fs * k), 1)}}
                                       for i, fs in sizes.items()])
        if rf:
            return None
        res = verify_collateral(before, trial, named, workdir, canvas["w"], canvas["h"])
        if res.get("ok"):
            return trial, res, [f"{i}: words set at {fs * k:.1f}px (was {fs:.1f}px) so they fit their box"
                                for i, fs in sizes.items()]
    return None


def _attempt(html, plan, workdir, verify, base):
    """Everything Aethron decides about one set of edits a model proposed."""
    edits, notes = plan["edits"], []
    canvas = manifest(html)["canvas"]
    for e in edits:
        an = e.get("animate") if isinstance(e, dict) else None
        if isinstance(an, dict) and an.get("strength") in ALIVE_TARGETS and verify and canvas.get("w"):
            word = an["strength"]
            an["strength"], tried = tune_alive(html, an.get("style", "drift"), an.get("period", 18),
                                               word, workdir, canvas["w"], canvas["h"])
            notes.append(f"'{word}' reached by measuring: {tried} -> strength {an['strength']}")
    out, applied, refused = apply(html, edits)
    out, legible = _legible_labels(out, applied)
    notes += legible
    checks = {}
    if verify and canvas.get("w"):
        named = []
        for e in applied:
            if "set" in e:
                named.append(e["id"])
            elif "move" in e or "resize" in e:
                named += [e["id"]] + riders(html, e["id"])
            elif "clone" in e:
                named += e.get("created", [])
        named = list(dict.fromkeys(named))
        if named:
            checks["collateral"] = verify_collateral(html, out, named, workdir, canvas["w"], canvas["h"])
            if checks["collateral"].get("ok") is False:
                fitted = _fit_words(html, out, applied, workdir, canvas)
                if fitted:
                    out, checks["collateral"], fit_notes = fitted
                    notes += fit_notes
            if checks["collateral"].get("ok") is False:
                return {**base, "verdict": "REFUSED", "why": checks["collateral"]["why"],
                        "refused": refused + [f"{len(applied)} edit(s) withdrawn: {checks['collateral']['why']}"],
                        "note": plan.get("note", ""), "checks": checks, "tuning": notes}
        if any("animate" in e for e in applied):
            checks["motion"] = prove_alive(out, workdir, canvas["w"], canvas["h"])
    verdict = "APPLIED" if applied else "NOTHING APPLIED"
    if checks.get("motion", {}).get("verdict") == "FAIL":
        verdict, out = "REFUSED", html
    return {**base, "verdict": verdict, "html": out, "applied": applied, "refused": refused,
            "note": plan.get("note", ""), "checks": checks, "tuning": notes}


def ask(html, request, workdir, budget_usd=0.03, call=None, verify=True, retries=1):
    """A person's words in; guarded, measured edits out.

    A model is given the page as a list of elements — words and numbers, no pixels — and
    the request, and returns edits. Aethron does the rest and decides: visibility words are
    reached by rendering and measuring, every edit passes the allow-list (absolute geometry
    is refused by name), and the result is checked ON SCREEN. If the edits are refused, the
    model is told exactly why — "it would cover s01 (button 'Generate')" — and may try once
    more, within the same spend cap; if that fails too, the page is left as it was.
    """
    ledger = {"calls": 0, "in": 0, "out": 0, "usd": 0.0, "stopped": None}
    if call is None:
        import aethron_build as B

        def call(prompt):
            return B.gemini_text(prompt, ledger, budget_usd)
    base = {"html": html, "applied": [], "refused": [], "note": "", "checks": {}, "ledger": ledger}
    prompt = brief(html, request) + ASK_RULES
    result, history = None, []
    for attempt in range(retries + 1):
        try:
            reply = call(prompt)
        except Exception as why:
            if result is None:
                return {**base, "verdict": "NOT ASKED", "why": str(why), "attempts": history}
            result["retry_not_asked"] = str(why)
            break
        plan = _parse_reply(reply)
        if plan is None:
            result = {**base, "verdict": "UNREADABLE", "why": "the model's reply was not the JSON asked for",
                      "reply": (reply or "")[:400]}
            break
        result = _attempt(html, plan, workdir, verify, base)
        history.append({"edits": plan.get("edits"), "verdict": result["verdict"],
                        "why": result.get("why"), "refused": result.get("refused")})
        if result["verdict"] == "APPLIED" or attempt == retries or not plan.get("edits"):
            break
        reasons = list(dict.fromkeys(result.get("refused", []) + ([result["why"]] if result.get("why") else [])))
        prompt = (brief(html, request) + ASK_RULES
                  + "\n\nYOUR LAST EDITS WERE NOT APPLIED:\n" + json.dumps(plan.get("edits"))
                  + "\n\nBECAUSE:\n- " + "\n- ".join(reasons)
                  + "\n\nPropose different edits that still do what the user asked but avoid that problem "
                  "(another placement, another element), or return {\"edits\": [], \"note\": \"why it cannot be "
                  "done on this page\"}.\n")
    result["attempts"] = history
    return result


def _selftest():
    ok = fail = 0

    def check(name, cond, detail=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  ok   {name}")
        else:
            fail += 1
            print(f"  FAIL {name}   {detail}")

    page = ('<html><head><style>html,body{width:800px;height:600px}'
            '</style></head><body>'
            '<div class="r" data-ae-id="g0" style="left:0;top:0;width:800px;'
            'height:600px;z-index:0;background:#0B0B0B"></div>'
            '<div class="r" data-ae-id="r1" style="left:0;top:300px;'
            'width:800px;height:1px;z-index:1;background:#333333"></div>'
            '<div class="r" data-ae-id="f1" style="left:100px;top:200px;'
            'width:120px;height:40px;z-index:2;background:#FFFFFF;'
            'border-radius:4px"></div>'
            '<div class="t" data-ae-id="t1" style="left:110px;top:210px;'
            'font-size:14.0px;color:#000000;z-index:4">Get started</div>'
            '<div class="t" data-ae-id="t2" style="left:40px;top:20px;'
            'font-size:18.0px;color:#FFFFFF;z-index:4">Wezzi</div>'
            '<img data-ae-id="p1" class="r" style="left:300px;top:400px;'
            'width:60px;height:60px;z-index:5" src="data:image/png;base64,x">'
            '</body></html>')

    print("── the page reads back as a list of things, not pixels")
    man = manifest(page)
    kinds = {e["id"]: e["kind"] for e in man["elements"]}
    check("every element is found", len(man["elements"]) == 6,
          str(list(kinds)))
    check("the canvas is read", man["canvas"] == {"w": 800, "h": 600},
          str(man["canvas"]))
    check("a hairline is a rule, not a fill", kinds.get("r1") == "rule")
    check("a filled box is a fill", kinds.get("f1") == "fill")
    check("type is text", kinds.get("t1") == "text")
    check("a crop is a picture", kinds.get("p1") == "picture")
    check("the ground is the ground", kinds.get("g0") == "ground")
    t1 = [e for e in man["elements"] if e["id"] == "t1"][0]
    check("text comes with its words", t1["text"] == "Get started")
    check("  ...and its measured size", t1["font_size"] == 14.0)

    print("\n── an edit that is allowed lands exactly")
    out, applied, refused = apply(page, [
        {"id": "f1", "set": {"background": "#B9FF66"}},
        {"id": "t2", "set": {"text": "Jomiez"}}])
    check("both applied", len(applied) == 2 and not refused, str(refused))
    check("the fill really changed", "#B9FF66" in out)
    check("the word really changed", ">Jomiez<" in out)
    check("nothing else moved", "left:100px" in out and "top:200px" in out)

    print("\n── AND NOW THE ATTACKS: what does a wrong edit have to do?")
    bad = [
        ({"id": "f1", "set": {"left": 0}}, "geometry is not the model's"),
        ({"id": "f1", "set": {"width": 500}}, "nor is width"),
        ({"id": "f1", "set": {"transform": "scale(2)"}}, "nor transform"),
        ({"id": "nope", "set": {"opacity": 1}}, "an id that does not exist"),
        ({"id": "t1", "set": {"background": "#fff"}},
         "a property that kind does not have"),
        ({"id": "f1", "set": {"background": "chartreuse"}},
         "a colour that does not parse"),
        ({"id": "t1", "set": {"font_size": 9000}}, "an impossible size"),
        ({"id": "t1", "set": {"font_size": "big"}}, "a size that is a word"),
        ({"id": "t1", "set": {"text": "<script>x</script>"}},
         "markup smuggled into a string"),
        ({"id": "t1", "set": {"text": "${x}`"}}, "a template literal"),
        ({"id": "t1", "set": {"opacity": 4}}, "an opacity out of range"),
        ({"id": "p1", "set": {"text": "hi"}}, "retyping a photograph"),
        ({"id": "f1", "set": {}}, "an edit that sets nothing"),
        ("not an object", "an edit that is not an edit"),
    ]
    caught = 0
    for edit, why in bad:
        out2, applied2, refused2 = apply(page, [edit])
        if refused2 and not applied2 and out2 == page:
            caught += 1
        else:
            print(f"  FAIL through: {why}  -> {applied2}")
    check(f"all {len(bad)} hostile edits refused, page untouched",
          caught == len(bad), f"caught {caught}")

    print("\n── a refusal explains itself")
    _, _, r = apply(page, [{"id": "f1", "set": {"top": 9}}])
    check("the reason names the property and why", r
          and "top" in r[0] and "measured" in r[0], str(r))
    _, ap, r2 = apply(page, [{"id": "f1", "set": {"background": "#000000"}},
                             {"id": "zz", "set": {"opacity": 1}}])
    check("one bad edit does not lose the good ones",
          len(ap) == 1 and len(r2) == 1)

    print("\n── a page Aethron's replicate command wrote: every kind is found, and edits land where they belong")
    rp = ('<html><head><style>.page{position:relative;width:900px;height:600px;overflow:hidden;'
          'background:radial-gradient(ellipse 40.00% 30.00% at 20.00% 80.00%, rgba(40,60,200,1.000) 0.00%, '
          'rgba(40,60,200,0.000) 100.00%),\n  rgb(2,2,4)}</style></head><body>'
          '<main class="page" data-ae-id="bg">'
          '<h1 class="t" data-ae-id="t00" style="left:100px;top:50px;font-size:40.00px;color:#FFFFFF">Build Apps</h1>'
          '<a class="t link" data-ae-id="t01" href="#x" style="left:500px;top:20px;font-size:14.00px;color:#A0A0A0">Features</a>'
          '<p class="t" data-ae-id="t03" style="left:10px;top:500px;font-size:12.00px;color:#FFFFFF">A <b>bold</b> word</p>'
          '<form class="composer"><div class="sf" data-ae-id="s00" style="left:80px;top:200px;width:500px;height:200px;'
          'border-radius:40px;background:rgba(238,238,241,0.810)"></div>'
          '<textarea class="t prompt" data-ae-id="t02" name="prompt" aria-label="Type something" placeholder="Type something" '
          'style="left:110px;top:230px;font-size:16.00px;color:#3B3F4F;width:400px;height:80px;--ph:#858795"></textarea>'
          '<button type="submit" class="sf" data-ae-id="s01" style="left:420px;top:340px;width:140px;height:44px;'
          'border-radius:22px;background:linear-gradient(90deg,#000000 0%,#443D5F 100%)"><span class="t" '
          'style="left:30px;top:10px;font-size:16.00px;color:#D8D4DE">Generate</span></button></form>'
          '<svg class="strokes" data-ae-id="k00" width="900" height="600"></svg></main></body></html>')
    man2 = manifest(rp)
    k2 = {e["id"]: e["kind"] for e in man2["elements"]}
    check("heading, link, box, text box, button, strokes and the page are all found",
          k2 == {"bg": "ground", "t00": "text", "t01": "text", "t03": "text", "s00": "fill",
                 "t02": "input", "s01": "button", "k00": "stroke"}, str(k2))
    check("a replicate page's canvas is read", man2["canvas"] == {"w": 900, "h": 600}, str(man2["canvas"]))
    out3, ap3, rf3 = apply(rp, [{"id": "s01", "set": {"background": "#16A34A", "text": "Create"}},
                               {"id": "t00", "set": {"text": "Build Apps Users Love"}},
                               {"id": "t02", "set": {"text": "Describe your app", "color": "#222222"}}])
    check("all three edits land", len(ap3) == 3 and not rf3, str(rf3))
    check("a button's fill goes on the button", 'data-ae-id="s01" style="left:420px;top:340px;width:140px;'
          'height:44px;border-radius:22px;background:#16A34A"' in out3)
    check("  ...and its words go on its label", ">Create</span></button>" in out3)
    check("a heading's words change inside the heading", ">Build Apps Users Love</h1>" in out3)
    check("a text box's words are its placeholder", 'placeholder="Describe your app"' in out3
          and "--ph:#222222" in out3)
    _, ap4, rf4 = apply(rp, [{"id": "t03", "set": {"text": "plain"}}])
    check("an edit that cannot be written is REFUSED, not counted as applied",
          not ap4 and rf4 and "could not be written" in rf4[0], str(rf4))

    print("\n── the background can live, and rest on the design")
    out5, ap5, rf5 = apply(rp, [{"id": "bg", "animate": {"style": "drift+breathe", "period": 16, "strength": 0.6}}])
    check("the ground can be told to move", len(ap5) == 1 and "@keyframes ae-bg0" in out5
          and "@property --ae0x" in out5, str(rf5))
    check("  ...its loop starts on the fitted values",
          "0.0%{--ae0w:40.00%;--ae0h:30.00%;--ae0x:20.00%;--ae0y:80.00%}" in out5)
    check("  ...and it holds still for anyone who asked for less motion", "prefers-reduced-motion" in out5)
    out6, _, _ = apply(out5, [{"id": "bg", "animate": {"style": "drift", "period": 30, "strength": 0.3}}])
    check("animating again replaces the motion, never stacks it", out6.count("<style data-ae-alive") == 1)
    out7, _, _ = apply(out6, [{"id": "bg", "animate": {"style": "off"}}])
    check("\"off\" removes it without a trace", out7 == rp)
    hostile = [{"id": "t00", "animate": {"style": "drift"}}, {"id": "bg", "animate": {"style": "explode"}},
               {"id": "bg", "animate": {"period": 1}}, {"id": "bg", "animate": {"strength": 5}},
               {"id": "bg", "animate": {"speed": "fast"}}, {"id": "bg", "animate": "yes"}]
    hostile.append({"id": "bg", "animate": {"strength": "loud"}})
    caught = sum(1 for e in hostile if (lambda r: not r[1] and r[2] and r[0] == rp)(apply(rp, [e])))
    _, ap8, rf8 = apply(rp, [{"id": "bg", "animate": {"style": "drift", "period": 10, "strength": "visible"}}])
    check("visibility can be asked for by name", len(ap8) == 1 and not rf8, str(rf8))
    check(f"all {len(hostile)} hostile animations refused, page untouched", caught == len(hostile), f"caught {caught}")

    print("\n── words in, guarded edits out (a stand-in model, no network, no render)")
    seen = []
    fenced = ('```json\n{"edits": [{"id": "s01", "set": {"background": "#16A34A"}}, '
              '{"id": "t00", "set": {"left": 0}}], "note": "made it green"}\n```')
    r = ask(rp, "make the button green", ".", call=lambda pr: (seen.append(pr), fenced)[1], verify=False)
    check("a fenced reply is read and its good edit applied", r["verdict"] == "APPLIED"
          and len(r["applied"]) == 1 and "#16A34A" in r["html"], str(r.get("verdict")))
    check("  ...the geometry it slipped in is refused by name", len(r["refused"]) == 1
          and "measured" in r["refused"][0], str(r["refused"]))
    check("  ...and the model was given the words and the element list, no pixels",
          seen and "make the button green" in seen[0] and '"id": "s01"' in seen[0] and "base64" not in seen[0])
    r = ask(rp, "make it pop", ".", call=lambda pr: "Sure! I made it pop.", verify=False)
    check("a reply that is not the JSON asked for changes nothing", r["verdict"] == "UNREADABLE" and r["html"] == rp)

    def refused_by_cap(pr):
        raise RuntimeError("cap: next call could cost up to $0.02, budget $0.01")
    r = ask(rp, "anything", ".", call=refused_by_cap, verify=False)
    check("a call the spend cap refused changes nothing, and says why", r["verdict"] == "NOT ASKED"
          and r["html"] == rp and "cap" in r["why"])

    print("\n── moving and resizing: relative, carried, and still refused as absolutes")
    g0 = {"s00": "left:80px;top:200px", "t02": "left:110px;top:230px", "s01": "left:420px;top:340px"}
    out9, ap9, rf9 = apply(rp, [{"id": "s00", "move": {"dx": 40, "dy": -20}}])
    check("a card moves", len(ap9) == 1 and not rf9 and 'data-ae-id="s00" style="left:120.0px;top:180.0px' in out9, str(rf9))
    check("  ...and carries its text box and its button with it",
          'data-ae-id="t02" name="prompt" aria-label="Type something" placeholder="Type something" style="left:150.0px;top:210.0px' in out9
          and 'data-ae-id="s01" style="left:460.0px;top:320.0px' in out9)
    check("  ...but not what is not on it", 'data-ae-id="t00" style="left:100px;top:50px' in out9)
    out10, ap10, _ = apply(rp, [{"id": "s01", "resize": {"scale": 1.5}}])
    check("a button grows about its own centre and stays a pill",
          'data-ae-id="s01" style="left:385.0px;top:329.0px;width:210.0px;height:66.0px;border-radius:33.0px' in out10, out10[out10.find('data-ae-id="s01"'):][:120])
    check("  ...with its label centred in the new size", "text-align:center" in out10[out10.find('data-ae-id="s01"'):])
    out11, ap11, _ = apply(rp, [{"id": "s01", "move": {"dx": -40, "dy": 0}, "resize": {"scale": 1.5}}])
    check("one edit that both moves and resizes does BOTH",
          'data-ae-id="s01" style="left:345.0px;top:329.0px;width:210.0px;height:66.0px' in out11,
          out11[out11.find('data-ae-id="s01"'):][:110])
    geo_bad = [{"id": "bg", "move": {"dx": 5}}, {"id": "t00", "resize": {"scale": 1.2}},
               {"id": "s01", "resize": {"scale": 9}}, {"id": "s01", "move": {"dx": "left"}},
               {"id": "s01", "move": {"x": 10}}, {"id": "s01", "set": {"left": 10}}]
    caught = sum(1 for e in geo_bad if (lambda r: not r[1] and r[2] and r[0] == rp)(apply(rp, [e])))
    check(f"all {len(geo_bad)} impossible or absolute geometry edits refused", caught == len(geo_bad), f"caught {caught}")

    print("\n── growing the page by copying what is there")
    rp2 = ('<html><head><style>.page{position:relative;width:900px;height:600px}</style></head><body>'
           '<main class="page" data-ae-id="bg"><form class="composer">'
           '<div class="sf" data-ae-id="s00" style="left:50px;top:50px;width:700px;height:300px;border-radius:40px;background:#EEEEEE"></div>'
           '<button type="button" class="sf" data-ae-id="s01" style="left:100px;top:250px;width:100px;height:40px;border-radius:20px;background:#DDDDDD"><span class="t" style="left:20px;top:10px;font-size:14.00px;color:#333333">Android</span></button>'
           '<button type="button" class="sf" data-ae-id="s02" style="left:210px;top:250px;width:100px;height:40px;border-radius:20px;background:#DDDDDD"><span class="t" style="left:30px;top:10px;font-size:14.00px;color:#333333">IOS</span></button>'
           '<button type="button" class="sf" data-ae-id="s03" style="left:320px;top:250px;width:100px;height:40px;border-radius:20px;background:#DDDDDD"><span class="t" style="left:15px;top:10px;font-size:14.00px;color:#333333">Windows</span></button>'
           '</form></main></body></html>')
    out12, ap12, rf12 = apply(rp2, [{"id": "s03", "clone": {"text": "Linux", "place": "after"}}])
    check("a fifth chip is copied from the fourth", len(ap12) == 1 and not rf12 and ap12[0].get("created") == ["s03-c1"], str((ap12, rf12)))
    check("  ...placed one MEASURED gap after it (10px)", 'data-ae-id="s03-c1" style="left:430.0px;top:250.0px;width:100px;height:40px;border-radius:20px;background:#DDDDDD"' in out12,
          out12[out12.find('data-ae-id="s03-c1"'):][:120])
    check("  ...with its own words, centred", ">Linux</span></button>" in out12 and out12.count(">Windows<") == 1)
    check("  ...and it is a real element the next edit can name", any(e["id"] == "s03-c1" for e in manifest(out12)["elements"]))
    out13, ap13, _ = apply(rp2, [{"id": "s00", "clone": {"place": "below"}}])
    check("copying a card copies what rides on it", len(ap13) == 1 and len(ap13[0].get("created", [])) == 4, str(ap13))
    bad_clone = [{"id": "bg", "clone": {}}, {"id": "s03", "clone": {"place": "inside"}},
                 {"id": "s03", "clone": {"text": "<b>x</b>"}}, {"id": "s03", "clone": {"width": 50}}]
    caught = sum(1 for e in bad_clone if (lambda r: not r[1] and r[2] and r[0] == rp2)(apply(rp2, [e])))
    check(f"all {len(bad_clone)} impossible copies refused", caught == len(bad_clone), f"caught {caught}")

    print("\n── a refused plan goes back to the model once, with the reason")
    prompts = []
    replies = iter(['{"edits": [{"id": "s01", "set": {"left": 900}}], "note": "moved"}',
                    '{"edits": [{"id": "s01", "move": {"dx": 40, "dy": 0}}], "note": "moved relatively"}'])
    r = ask(rp, "move the button right", ".", call=lambda pr: (prompts.append(pr), next(replies))[1], verify=False)
    check("the second, corrected plan is applied", r["verdict"] == "APPLIED" and len(r["attempts"]) == 2, str(r.get("attempts")))
    check("  ...and the retry told the model exactly what was refused and why",
          len(prompts) == 2 and "WERE NOT APPLIED" in prompts[1] and "measured" in prompts[1])
    replies2 = iter(['{"edits": [{"id": "s01", "set": {"left": 900}}], "note": "x"}',
                     '{"edits": [], "note": "there is no room for that on this page"}'])
    r = ask(rp, "move the button", ".", call=lambda pr: next(replies2), verify=False)
    check("a model that says it cannot be done leaves the page untouched, with its reason",
          r["verdict"] == "NOTHING APPLIED" and r["html"] == rp and "no room" in r["note"], str(r.get("verdict")))

    print("\n── the brief a model gets carries no pixels")
    b = brief(page, "make the button green")
    check("it contains the element list", '"id": "f1"' in b)
    check("it contains no image data", "base64" not in b)
    check("it says what may not be set", "left, top, width" in b)

    print(f"\nedit selftest: {ok} ok, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
