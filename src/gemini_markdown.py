"""[claude] 2026-11-14: Gemini answer (AT-SPI tree) -> Markdown.

Before: the bridge/watcher took only flat paragraph text -> headings dropped,
list markers/nesting, code fences and tables lost; Hermes got a wall of text
(DeepSeek 0731 via OpenRouter returns proper Markdown). The Firefox a11y tree
keeps the HTML structure (tag attr: h2/p/ul/ol/li/pre/code/table/...), so we
rebuild Markdown from it. Entry point: response_markdown(heading_node) where
heading_node is the 'Gemini said' answer heading; the answer is its next sibling.
Any failure -> "" (callers fall back to the old flat text).
"""
import os

OBJ = "\ufffc"
SKIP_TAGS = {"gem-icon-button", "button", "sources-list", "message-actions",
             "gem-popover", "thumb-up-button", "thumb-down-button", "copy-button",
             "mat-icon", "gem-icon"}
SKIP_ROLES = {"push button", "toggle button", "image"}


def _attrs(n):
    try:
        return dict(x.split(":", 1) for x in n.getAttributes() if ":" in x)
    except Exception:
        return {}


def _tag(n):
    return _attrs(n).get("tag", "")


def _role(n):
    try:
        return n.getRoleName()
    except Exception:
        return ""


def _children(n):
    try:
        return [n.getChildAtIndex(i) for i in range(n.childCount)]
    except Exception:
        return []


def _raw(n):
    try:
        return n.queryText().getText(0, -1)
    except Exception:
        return ""


def _bold_runs(n, length, text=""):
    """[(start, end)] of bold text via AT-SPI text attribute runs."""
    runs = []
    try:
        t = n.queryText()
        off = 0
        while off < length:
            attrs, s, e = t.getAttributeRun(off, False)
            if e <= off:
                break
            a = dict(x.split(":", 1) for x in attrs if ":" in x)
            w = a.get("weight", "")
            # Gemini's variable font: bold = weight 540 (verified live), normal 400
            if w.isdigit() and int(w) >= 500:
                while s < e and text[s:s + 1].isspace():
                    s += 1
                while e > s and text[e - 1:e].isspace():
                    e -= 1
                if e > s:
                    runs.append((s, e))
            off = e
    except Exception:
        pass
    return runs


def inline(n):
    """Text of a paragraph-like node: inline code chips -> `x`, links -> text,
    bold -> **x**; embedded objects resolved through the Hypertext links."""
    t = _raw(n)
    if not t:
        return ""
    reps = {}
    if OBJ in t:
        try:
            ht = n.queryHypertext()
            for i in range(ht.getNLinks()):
                l = ht.getLink(i)
                o = l.getObject(0)
                r, tg = _role(o), _tag(o)
                sub = inline(o)
                if tg == "code" or (r == "static" and tg != "b"):
                    sub = "`" + sub.strip("`") + "`" if sub.strip() and "\n" not in sub else sub
                elif r in SKIP_ROLES or tg in SKIP_TAGS:
                    sub = ""
                reps[l.startIndex] = sub
        except Exception:
            pass
    bold = _bold_runs(n, len(t), t)
    starts = {s for s, e in bold}
    ends = {e for s, e in bold}
    out = []
    for i, ch in enumerate(t):
        if i in ends:
            out.append("**")
        if i in starts:
            out.append("**")
        if ch == OBJ:
            out.append(reps.get(i, ""))
        else:
            out.append(ch)
    if len(t) in ends:
        out.append("**")
    s = "".join(out).replace("****", "")
    return s.strip()


def _lang_of(container):
    """Code-block header label ('Python') sitting next to the <pre>."""
    stack = [c for c in _children(container) if _tag(c) != "pre"]
    while stack:
        n = stack.pop(0)
        if _tag(n) in SKIP_TAGS or _role(n) in SKIP_ROLES:
            continue
        t = _raw(n).strip()
        if t and OBJ not in t and len(t) < 30:
            return t.lower()
        stack = _children(n) + stack
    return ""


def _table(n):
    rows = []
    stack = [n]
    while stack:
        x = stack.pop(0)
        if _role(x) == "table row" or _tag(x) == "tr":
            cells = [inline(c).replace("|", "\\|").replace("\n", " ") for c in _children(x)
                     if _role(c) in ("table cell", "column header", "row header")]
            rows.append(cells)
            continue
        stack = _children(x) + stack
    if not rows:
        return ""
    w = max(len(r) for r in rows)
    rows = [r + [""] * (w - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * w]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _list(n, depth, out_lines):
    ordered = _tag(n) == "ol"
    k = 0
    for li in _children(n):
        if _role(li) != "list item" and _tag(li) != "li":
            continue
        k += 1
        marker = "%d." % k if ordered else "-"
        head, rest = [], []
        for c in _children(li):
            tg = _tag(c)
            if tg in ("ul", "ol"):
                rest.append(("list", c))
            elif tg == "pre" or any(_tag(g) == "pre" for g in _children(c)):
                rest.append(("block", c))
            elif not rest:
                txt = inline(c) if _role(c) in ("paragraph", "static", "section", "heading") else ""
                if txt:
                    head.append(txt)
            else:
                rest.append(("block", c))
        if not head and not _children(li):
            head = [_raw(li).lstrip("•◦▪-0123456789. ").replace(OBJ, "")]
        ind = "   " * depth
        out_lines.append(ind + marker + " " + " ".join(head).strip())
        for kind, c in rest:
            if kind == "list":
                _list(c, depth + 1, out_lines)
            else:
                sub = []
                _blocks(c, sub)
                for b in sub:
                    out_lines.extend((ind + "   " + ln) if ln else "" for ln in b.split("\n"))


def _blocks(n, out):
    r, tg = _role(n), _tag(n)
    if tg in SKIP_TAGS or r in SKIP_ROLES:
        return
    if r == "heading" or (len(tg) == 2 and tg[0] == "h" and tg[1].isdigit()):
        lvl = _attrs(n).get("level", tg[1:] if tg[1:].isdigit() else "2")
        lvl = int(lvl) if str(lvl).isdigit() else 2
        txt = inline(n)
        if txt:
            out.append("#" * max(1, min(lvl, 6)) + " " + txt)
        return
    if tg in ("ul", "ol") or r == "list":
        lines = []
        _list(n, 0, lines)
        if lines:
            out.append("\n".join(lines))
        return
    if r == "table" or tg == "table":
        t = _table(n)
        if t:
            out.append(t)
        return
    if tg == "hr" or r == "separator":
        out.append("---")
        return
    if tg == "blockquote":
        sub = []
        for c in _children(n):
            _blocks(c, sub)
        if sub:
            out.append("\n".join("> " + ln for ln in "\n\n".join(sub).split("\n")))
        return
    kids = _children(n)
    pre = [c for c in kids if _tag(c) == "pre"]
    if pre:
        lang = _lang_of(n)
        for p in pre:
            code = "".join(_raw(c) for c in _children(p)) or _raw(p)
            code = code.replace(OBJ, "").rstrip("\n")
            out.append("```" + lang + "\n" + code + "\n```")
        return
    if tg == "pre":
        code = ("".join(_raw(c) for c in kids) or _raw(n)).replace(OBJ, "").rstrip("\n")
        out.append("```\n" + code + "\n```")
        return
    if r == "paragraph" or tg == "p":
        txt = inline(n)
        if txt:
            out.append(txt)
        return
    if not kids:
        txt = _raw(n).replace(OBJ, "").strip()
        if txt and r in ("static", "section", "text"):
            out.append(txt)
        return
    for c in kids:
        _blocks(c, out)


def _find_response(heading_node, max_up=6):
    """The heading sits in its own wrapper; the answer body is the nearest
    <model-response-content> (or <message-content>) under an ancestor."""
    want = ("model-response-content", "message-content")
    node = heading_node
    for _ in range(max_up):
        try:
            par = node.parent
        except Exception:
            return None
        if par is None:
            return None
        if _tag(par) in want:
            return par
        stack = [c for c in _children(par) if c != node]
        seen = 0
        while stack and seen < 400:
            x = stack.pop(0)
            seen += 1
            if _tag(x) in want:
                return x
            stack = _children(x) + stack
        node = par
    return None


def response_markdown(heading_node):
    """Markdown of the answer that follows the answer heading ("" on failure)."""
    try:
        resp = _find_response(heading_node)
        if resp is None:
            return ""
        out = []
        _blocks(resp, out)
        md = "\n\n".join(b for b in out if b.strip())
        # Drop the fixed "Gemini can make mistakes" disclaimer line, if present.
        _disclaimer = os.environ.get("GEMINI_UI_DISCLAIMER", "can make mistakes")
        lines = [ln for ln in md.split("\n")
                 if not (ln.startswith("Gemini") and _disclaimer in ln)]
        return "\n".join(lines).strip()
    except Exception:
        return ""
