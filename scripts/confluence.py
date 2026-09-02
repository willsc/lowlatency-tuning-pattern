"""Confluence wiki-markup emitters.

The generated documents exist in two dialects: GitHub Markdown for review in the repo,
and Confluence wiki markup for pasting into a page (editor > Insert > Markup > Confluence
wiki). Both come from the same generator, so the page and the repo cannot disagree.

Wiki markup, not storage-format XHTML: it survives a copy-paste by a human, which is how
these pages actually get updated.
"""

# A '|' inside a cell ends the cell, and CPU lists never contain one - but a ranked list
# is '|'-separated, so anything that might carry one goes through cell() first.
def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def h(level, text):
    return f"h{level}. {text}\n"


def para(text):
    return text.rstrip() + "\n"


def mono(text):
    return "{{" + str(text) + "}}"


def bold(text):
    return f"*{text}*"


def table(headers, rows):
    """||h||h|| header row, then one |a|b| row per record."""
    out = ["||" + "||".join(str(x) for x in headers) + "||"]
    for r in rows:
        out.append("|" + "|".join(cell(x) if str(x).strip() else " " for x in r) + "|")
    return "\n".join(out) + "\n"


def code(body, title=None, language=None):
    opts = []
    if title:
        opts.append(f"title={title}")
    if language:
        opts.append(f"language={language}")
    head = "{code:" + "|".join(opts) + "}" if opts else "{code}"
    return f"{head}\n{body.rstrip()}\n{{code}}\n"


def noformat(body, title=None):
    """Plain text with no syntax colouring - the right macro for an ASCII diagram."""
    head = "{noformat:title=" + title + "}" if title else "{noformat}"
    return f"{head}\n{body.rstrip()}\n{{noformat}}\n"


def panel(kind, body, title=None):
    """kind: info | note | warning | tip"""
    head = "{" + kind + (f":title={title}" if title else "") + "}"
    return f"{head}\n{body.rstrip()}\n{{{kind}}}\n"


def expand(title, body):
    return "{expand:" + title + "}\n" + body.rstrip() + "\n{expand}\n"


def toc(max_level=2):
    return "{toc:maxLevel=" + str(max_level) + "|minLevel=2}\n"


def anchor(name):
    return "{anchor:" + name + "}\n"


def link(text, anchor_name):
    return f"[{text}|#{anchor_name}]"
