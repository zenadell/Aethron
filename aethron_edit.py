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
}

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
    for m in re.finditer(r"<(div|img)\b([^>]*)>(?:([^<]*)</div>)?", html):
        attrs, inner = m.group(2), m.group(3) or ""
        idm = ID_RE.search(attrs)
        if not idm:
            continue
        st = STYLE_RE.search(attrs)
        style = _parse(st.group(1) if st else "")
        kind = _kind(attrs, style)
        e = {"id": idm.group(1), "kind": kind,
             "box": [_num(style.get("left")), _num(style.get("top")),
                     _num(style.get("width")), _num(style.get("height"))]}
        if kind == "text":
            e["text"] = _unescape(inner)
            e["color"] = style.get("color")
            e["font_size"] = _num(style.get("font-size"))
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
    mm = re.search(r"html,body\{width:(\d+)px;height:(\d+)px", html)
    if mm:
        w, h = int(mm.group(1)), int(mm.group(2))
    return {"canvas": {"w": w, "h": h}, "elements": out}


def _kind(attrs, style):
    if "<img" in attrs or "src=" in attrs:
        return "picture"
    cls = re.search(r'class="([^"]*)"', attrs)
    cls = cls.group(1) if cls else ""
    if "t" in cls.split():
        return "text"
    if _num(style.get("height")) == 1 or _num(style.get("width")) == 1:
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
        html = _write(html, edit["id"], edit["set"])
        applied.append(edit)
    return html, applied, refused


def _write(html, eid, sets):
    pat = re.compile(r'(<(?:div|img)\b[^>]*data-ae-id="'
                     + re.escape(eid) + r'"[^>]*>)([^<]*)', re.S)
    m = pat.search(html)
    if not m:
        return html
    tag, inner = m.group(1), m.group(2)
    new_tag, new_inner = tag, inner
    st = STYLE_RE.search(tag)
    style = _parse(st.group(1) if st else "")
    for k, v in sets.items():
        if k == "text":
            new_inner = _escape(v)
        elif k == "hidden":
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
        new_tag = new_tag.replace(st.group(0), f'style="{flat}"', 1)
    else:
        new_tag = new_tag[:-1] + f' style="{flat}">'
    return html[:m.start()] + new_tag + new_inner + html[m.end():]


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
  fill elements     background, border_radius, opacity, hidden
  rule elements     background, opacity, hidden
  picture elements  opacity, border_radius, hidden

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

    print("\n── the brief a model gets carries no pixels")
    b = brief(page, "make the button green")
    check("it contains the element list", '"id": "f1"' in b)
    check("it contains no image data", "base64" not in b)
    check("it says what may not be set", "left, top, width" in b)

    print(f"\nedit selftest: {ok} ok, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
