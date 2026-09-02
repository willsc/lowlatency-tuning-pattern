"""Plain-text diagrams for the configuration layers.

Everything here renders inside a fixed column budget (WIDTH) so the same string is
legible in a terminal, in a Markdown fence on GitHub, and inside a Confluence {noformat}
macro at default page width. That budget is the whole reason these are text and not SVG:
a diagram that has to be exported, committed and kept in step with the planner drifts
exactly the way a hand-maintained CPU list drifts.

Box-drawing characters only from U+2500..U+257F. Those are unambiguous-width in every
monospace font that matters, so a row is exactly WIDTH columns wide wherever it is read.
Arrows and other symbol blocks are deliberately avoided: several are East-Asian-Ambiguous
and render double-width in a CJK-configured terminal, which shears the whole diagram.
"""

WIDTH = 78            # every emitted row is exactly this wide
INNER = WIDTH - 4     # usable text columns inside a bordered row

# ── box drawing ──────────────────────────────────────────────────────────────
TL, TR, BL, BR = "┌", "┐", "└", "┘"
HL, VL = "─", "│"
T_DOWN, T_UP, T_RIGHT, T_LEFT = "┬", "┴", "├", "┤"
CROSS = "┼"

ROLE_GLYPH = [
    ("housekeeping", "H", "hk"),
    ("irqnet", "I", "irq"),
    ("shared", "S", "shared"),
    ("exclusive", "E", "excl"),
]

LEGEND = ("H housekeeping    I irqnet    S shared (in no cpuset)    "
          "E exclusive (isolated)")


# ---------------------------------------------------------------- primitives

def _row(text=""):
    """A bordered content row, padded to exactly WIDTH."""
    return VL + " " + text[:INNER].ljust(INNER) + " " + VL


def _band(left="", right="", kind="mid"):
    """A titled rule: '├─ left ─────…───── right ─┤'."""
    l, r = {"top": (TL, TR), "bottom": (BL, BR), "mid": (T_RIGHT, T_LEFT)}[kind]
    lt = f"{HL} {left} " if left else ""
    rt = f" {right} {HL}" if right else ""
    return l + HL + lt + HL * max(1, WIDTH - 4 - len(lt) - len(rt)) + rt + HL + r


def _field(label, value, note="", width=20, indent=3):
    """'  label    value                      note' rows.

    A long CPU list wraps under a hanging indent; the note stays flushed right on the
    last line it fits on, so the count never floats away from the list it counts.
    """
    lead = " " * indent + label.ljust(width)
    room = INNER - len(lead) - (len(note) + 3 if note else 0)
    chunks, cur = [], ""
    for part in str(value).split(","):
        nxt = f"{cur},{part}" if cur else part
        if len(nxt) > room and cur:
            chunks.append(cur + ",")
            cur = part
        else:
            cur = nxt
    chunks.append(cur)

    out = []
    for i, chunk in enumerate(chunks):
        text = (lead if i == 0 else " " * len(lead)) + chunk
        if note and i == len(chunks) - 1:
            gap = INNER - len(text) - len(note)
            text = text + " " * max(2, gap) + note
        out.append(_row(text))
    return out


def _split_row(left, right, indent=3):
    """Left text, right text flushed to the right margin."""
    left = " " * indent + left
    gap = INNER - len(left) - len(right)
    return _row(left + " " * max(1, gap) + right)


def _place(items, width):
    """Lay labels out at their anchor columns, shifting right on collision.

    Returns None if they cannot be fitted, so the caller can fall back to a compact
    single line rather than emitting a row that silently runs past the margin.
    """
    row = ""
    for col, text in items:
        start = col if not row else max(col, len(row) + 2)
        if start + len(text) > width:
            return None
        row += " " * (start - len(row)) + text
    return row


# ---------------------------------------------------------------- core map

def _segments(counts, span):
    """Distribute `span` columns across the roles, proportionally, min 1 each."""
    total = sum(counts.values())
    raw = {r: counts[r] * span / total for r, _, _ in ROLE_GLYPH}
    cols = {r: max(1, int(raw[r])) if counts[r] else 0 for r, _, _ in ROLE_GLYPH}
    live = [r for r, _, _ in ROLE_GLYPH if counts[r]]
    order = sorted(live, key=lambda r: raw[r] - int(raw[r]), reverse=True)
    i = 0
    while sum(cols.values()) < span:
        cols[order[i % len(order)]] += 1
        i += 1
    while sum(cols.values()) > span:
        biggest = max((r for r in cols if cols[r] > 1), key=lambda r: cols[r])
        cols[biggest] -= 1
    return cols


def _node_bar(node, indent=2):
    """A segmented proportional bar: one box cell per role, divided where roles meet.

    The dividers matter more than the glyphs. A run of letters shows the ratio; a ruled
    boundary shows *where* the cut falls, which is the thing anyone reading this actually
    wants to check.
    """
    counts = {r: node[r]["count"] for r, _, _ in ROLE_GLYPH}
    live = [(r, g, short) for r, g, short in ROLE_GLYPH if counts[r]]
    span = WIDTH - 2 * indent - 2 - (len(live) - 1)   # borders and dividers
    cols = _segments(counts, span)

    pad = " " * indent
    top = pad + TL + T_DOWN.join(HL * cols[r] for r, _, _ in live) + TR
    mid = pad + VL + VL.join(g * cols[r] for r, g, _ in live) + VL
    bot = pad + BL + T_UP.join(HL * cols[r] for r, _, _ in live) + BR

    # Anchor each label under the first column of its segment. The glyph prefix is not
    # decoration: a narrow segment pushes its label right, and without the prefix a
    # reader would tie the range to the wrong block.
    anchors, col = [], indent + 1
    for r, g, _ in live:
        anchors.append((col, f"{g} {node[r]['cpus']}"))
        col += cols[r] + 1
    labels = _place(anchors, WIDTH)
    if labels is None:
        labels = (pad + "  " + "   ".join(f"{g} {node[r]['cpus']}"
                                          for r, g, _ in live))[:WIDTH]
    return [top, mid, bot, labels]


def core_map(plan):
    """One segmented bar per NUMA node, ordered nearest-the-NIC first."""
    by_node = {n["node"]: n for n in plan["nodes"]}
    tier_label = {t["tier"]: t["label"] for t in plan["nic_locality"]["tiers"]}
    out = []
    for node in plan["nic_locality"]["node_order"]:
        n = by_node[node]
        cpus = sorted(int(c) for r, _, _ in ROLE_GLYPH
                      for c in _expand(n[r]["cpus"]))
        head = f"node{node}  ·  tier {n['nic_tier']}  ·  {tier_label[n['nic_tier']]}"
        tail = f"{n['cores_total']} cores  ·  cpu {cpus[0]}-{cpus[-1]}"
        out.append("  " + head.ljust(WIDTH - 4 - len(tail)) + tail)
        out += _node_bar(n)
        out.append("")
    out.append("  " + LEGEND)
    return "\n".join(out)


def _expand(spec):
    cpus = []
    for part in str(spec).split(","):
        if not part.strip():
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            cpus.extend(range(int(a), int(b) + 1))
        else:
            cpus.append(int(part))
    return cpus


# ---------------------------------------------------------------- nic distance axis

def nic_axis(plan):
    """The NUMA nodes as a distance axis from the primary NIC, nearest tier first.

    This is the diagram that says the thing the core map cannot: the exclusive pool is
    ordered, and the leftmost column is the only part of it that is actually NIC-local.
    """
    loc = plan["nic_locality"]
    tiers = loc["tiers"]
    if len(loc["node_order"]) == 1:
        return None

    cells = []
    for t in tiers:
        excl = plan["app"]["exclusive_cores_by_tier"][str(t["tier"])]
        cells.append([
            f"tier {t['tier']}",
            t["label"],
            ", ".join(f"node{n}" for n in t["nodes"]),
            f"{len(_expand(excl))} exclusive cpu",
        ])

    # Each column is at least as wide as its own text, and the slack left over is shared
    # out in proportion to how many exclusive cores the tier holds. The column widths
    # then carry the point of the diagram: on a 6-node shape the NIC-local column is
    # visibly the narrow one.
    inner = WIDTH - 2 - (len(cells) - 1)
    widths = [max(len(l) for l in c) + 2 for c in cells]
    weights = [len(_expand(plan["app"]["exclusive_cores_by_tier"][str(t["tier"])]))
               for t in tiers]
    slack = inner - sum(widths)
    if slack > 0:
        total = sum(weights) or 1
        share = [slack * w // total for w in weights]
        widths = [a + b for a, b in zip(widths, share)]
    while sum(widths) < inner:
        widths[widths.index(min(widths))] += 1
    while sum(widths) > inner:
        widths[widths.index(max(widths))] -= 1

    # Wrap rather than truncate: a column narrowed to fit the page must not silently
    # eat the end of a node list.
    wrapped = []
    for cell, w in zip(cells, widths):
        lines = []
        for text in cell:
            cur = ""
            for word in text.split(" "):
                nxt = f"{cur} {word}" if cur else word
                if len(nxt) > w - 2 and cur:
                    lines.append(cur)
                    cur = "  " + word
                else:
                    cur = nxt
            lines.append(cur)
        wrapped.append(lines)

    rows = max(len(c) for c in wrapped)
    body = []
    for i in range(rows):
        line = VL
        for cell, w in zip(wrapped, widths):
            text = cell[i] if i < len(cell) else ""
            line += (" " + text).ljust(w)[:w] + VL
        body.append(line)

    # The drop-line from the NIC lands on the tier-0 cell, so the diagram says which
    # column is local rather than relying on the reader to trust the ordering.
    drop = 2
    first = HL * (drop - 1) + T_UP + HL * (widths[0] - drop)
    top = TL + T_DOWN.join([first] + [HL * w for w in widths[1:]]) + TR

    out = [
        "  " + f"primary NIC on node {loc['nic_numa_node']}",
        "  " + VL,
        top,
    ] + body + [
        BL + T_UP.join(HL * w for w in widths) + BR,
        "  nearest".ljust(WIDTH - 8) + "furthest",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------- layer stack

def layer_stack(plan, sku, cpu_name, numa_mode):
    """The four configuration layers above the machine, with this shape's real values."""
    t = plan["topology"]
    roles = plan["roles"]
    app = plan["app"]
    loc = plan["nic_locality"]
    order = " > ".join(f"node{n}" for n in loc["node_order"])

    o = [_band("LAYER 4   application core contract", "start-up · no reboot", "top")]
    o += _field("exclusive_cores", app["exclusive_cores"],
                f"{app['exclusive_core_count']} cpu")
    o += _field("shared_cores", app["shared_cores"], f"{app['shared_core_count']} cpu")
    o.append(_row())
    o += _field("spend in this order", order)
    o += _field("  tier 0, NIC-local", app["nic_local_exclusive_cores"],
                f"{app['nic_local_exclusive_core_count']} cpu")

    o.append(_band("LAYER 3   runtime pinning", "every boot · lltune-runtime"))
    o += _field("irqaffinity", plan["irq_landing_cpus"])
    o += _field("workqueue mask", plan["non_isolated_cpus"])
    o += _field("ENA queues", str(roles["irqnet"]["cpu_count"]),
                "clamped to the irqnet core count")

    o.append(_band("LAYER 2   cgroup v2 slices", "daemon-reload · immediate"))
    o += _field("pulsar.slice", roles["exclusive"]["cpus"],
                f"{roles['exclusive']['cpu_count']} cpu")
    o += _field("irqnet.slice", roles["irqnet"]["cpus"],
                f"{roles['irqnet']['cpu_count']} cpu")
    o += _field("system.slice", roles["housekeeping"]["cpus"],
                f"{roles['housekeeping']['cpu_count']} cpu")
    o += _field("shared_cores", roles["shared"]["cpus"],
                f"{roles['shared']['cpu_count']} cpu")
    o.append(_row(" " * 23 + "named by no cpuset; reached by inherited affinity"))

    # The one constraint in the design that nothing enforces at run time, drawn as the
    # link it actually is rather than left as a sentence somewhere else.
    o.append(_band())
    o.append(_row("   pulsar.slice AllowedCPUs  " + "═" * 12 + "  isolcpus"))
    o.append(_row("   identical by construction · nothing checks it at run time"))

    o.append(_band("LAYER 1   boot isolation", "grub.d · REQUIRES A REBOOT"))
    o += _field("isolcpus", plan["isolated_cpus"])
    o += _field("nohz_full", plan["isolated_cpus"])
    o += _field("rcu_nocbs", plan["isolated_cpus"])

    o.append(_band("LAYER 0   the machine", sku))
    o.append(_row(f"   {cpu_name}"))
    o.append(_row(f"   {t['sockets']} socket(s) × {t['total_cores'] // t['sockets']} cores"
                  f"   ·   {t['numa_nodes']} NUMA node(s) ({numa_mode})"
                  f"   ·   L3 domain {t['cores_per_l3']}"))
    nic_note = ("the only node there is" if t["numa_nodes"] == 1
                else "read from the adapter" if loc["verified"]
                else "inferred · confirm with lltune nic --live")
    o.append(_row(f"   primary NIC on node {loc['nic_numa_node']}   ({nic_note})"))
    o.append(_band(kind="bottom"))
    return "\n".join(o)
