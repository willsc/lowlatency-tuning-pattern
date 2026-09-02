"""Plain-text diagrams for the configuration layers.

Everything here renders inside a fixed column budget (WIDTH) so the same string is
legible in a terminal, in a Markdown fence on GitHub, and inside a Confluence {code}
macro at default page width. That budget is the whole reason these are text and not
SVG: a diagram that has to be exported, committed and kept in step with the planner
drifts exactly the way a hand-maintained CPU list drifts.
"""

WIDTH = 78          # outer width of a box, including both border characters
BAR = WIDTH - 6     # inner width of a core-map bar

ROLE_GLYPH = [
    ("housekeeping", "H"),
    ("irqnet", "I"),
    ("shared", "S"),
    ("exclusive", "E"),
]

LEGEND = ("H housekeeping   I irqnet   S shared (in no cpuset)   "
          "E exclusive (isolated)")


# ---------------------------------------------------------------- box drawing

def _rule(ch="-"):
    return "+" + ch * (WIDTH - 2) + "+"


def _line(text=""):
    return "| " + text[:WIDTH - 4].ljust(WIDTH - 4) + " |"


def _split(label, value, indent=2):
    """A 'label   value' row, wrapping a long CPU list under a hanging indent."""
    pad = " " * indent
    lab = f"{pad}{label}"
    room = WIDTH - 4 - len(lab) - 2
    out, cur = [], ""
    for part in value.split(","):
        candidate = f"{cur},{part}" if cur else part
        if len(candidate) > room and cur:
            out.append(cur + ",")
            cur = part
        else:
            cur = candidate
    out.append(cur)
    rows = [_line(f"{lab}  {out[0]}")]
    for extra in out[1:]:
        rows.append(_line(" " * (len(lab) + 2) + extra))
    return rows


def _headed(left, right):
    """A layer heading: title on the left, when-it-applies flushed right."""
    room = WIDTH - 4
    gap = room - len(left) - len(right)
    return _line(left + " " * max(1, gap) + right)


# ---------------------------------------------------------------- core map

def core_bar(counts):
    """Proportional role bar. Any role with cores gets at least one column."""
    total = sum(counts.values())
    if not total:
        return " " * BAR
    raw = {r: counts[r] * BAR / total for r, _ in ROLE_GLYPH}
    cols = {r: max(1, int(raw[r])) if counts[r] else 0 for r, _ in ROLE_GLYPH}
    # Hand the rounding remainder to the largest roles until the bar is exactly full.
    order = sorted((r for r, _ in ROLE_GLYPH if counts[r]),
                   key=lambda r: raw[r] - int(raw[r]), reverse=True)
    i = 0
    while sum(cols.values()) < BAR and order:
        cols[order[i % len(order)]] += 1
        i += 1
    while sum(cols.values()) > BAR:
        biggest = max((r for r in cols if cols[r] > 1), key=lambda r: cols[r])
        cols[biggest] -= 1
    return "".join(g * cols[r] for r, g in ROLE_GLYPH)


def core_map(plan):
    """One proportional bar per NUMA node, ordered nearest-NIC-first."""
    by_node = {n["node"]: n for n in plan["nodes"]}
    tier_label = {t["tier"]: t["label"] for t in plan["nic_locality"]["tiers"]}
    out = []
    for node in plan["nic_locality"]["node_order"]:
        n = by_node[node]
        counts = {r: n[r]["count"] for r, _ in ROLE_GLYPH}
        head = (f"node{node}   tier {n['nic_tier']}  {tier_label[n['nic_tier']]}")
        tail = f"{n['cores_total']} cores"
        out.append("  " + head.ljust(BAR - len(tail)) + tail)
        out.append("  [" + core_bar(counts) + "]")
    out.append("")
    out.append("  " + LEGEND)
    return "\n".join(out)


# ---------------------------------------------------------------- layer stack

def layer_stack(plan, sku, cpu_name, numa_mode):
    """The four configuration layers above the machine, with this shape's real values."""
    t = plan["topology"]
    roles = plan["roles"]
    app = plan["app"]
    loc = plan["nic_locality"]
    irq_cores = roles["irqnet"]["cpu_count"]
    order = " -> ".join(f"node{n}" for n in loc["node_order"])

    o = [_rule("=")]
    o.append(_headed("LAYER 4  APPLICATION CORE CONTRACT",
                     "no reboot; read at start-up"))
    o += _split("exclusive_cores", app["exclusive_cores"])
    o += _split("shared_cores   ", app["shared_cores"])
    o += _split("NIC-local first", order)
    o.append(_line(f"  spend {app['nic_local_exclusive_cores']} "
                   f"({app['nic_local_exclusive_core_count']} cpu) before any other "
                   f"exclusive core"))

    o.append(_rule())
    o.append(_headed("LAYER 3  RUNTIME PINNING", "lltune-runtime.service, every boot"))
    o += _split("IRQ affinity   ", plan["irq_landing_cpus"])
    o.append(_line(f"  ENA combined queues   {irq_cores}  (clamped to the irqnet core count)"))
    o += _split("workqueue mask ", plan["non_isolated_cpus"])

    o.append(_rule())
    o.append(_headed("LAYER 2  CGROUP v2 SLICES", "daemon-reload; immediate"))
    o += _split("pulsar.slice   ", roles["exclusive"]["cpus"])
    o += _split("irqnet.slice   ", roles["irqnet"]["cpus"])
    o += _split("system.slice   ", roles["housekeeping"]["cpus"])
    o += _split("shared_cores   ", roles["shared"]["cpus"])
    o.append(_line(" " * 19 + "in NO cpuset - reached by inherited affinity"))

    o.append(_rule())
    o.append(_line("   ^   pulsar.slice AllowedCPUs MUST equal isolcpus. Nothing checks"))
    o.append(_line("   |   it at run time, so both are rendered from one plan field."))
    o.append(_rule())

    o.append(_headed("LAYER 1  BOOT ISOLATION", "grub.d; REQUIRES A REBOOT"))
    o += _split("isolcpus       ", plan["isolated_cpus"])
    o.append(_line("  also nohz_full and rcu_nocbs, both the same list"))

    o.append(_rule())
    o.append(_headed(f"LAYER 0  MACHINE   {sku}", cpu_name))
    o.append(_line(f"  {t['sockets']} socket(s) x {t['total_cores'] // t['sockets']} cores"
                   f"   {t['numa_nodes']} NUMA node(s) ({numa_mode})"
                   f"   L3 domain {t['cores_per_l3']}"))
    if len(loc["node_order"]) == 1:
        nic_note = "the only node there is"
    elif loc["verified"]:
        nic_note = "read from the adapter on a live host"
    else:
        nic_note = "inferred - confirm with lltune nic --live"
    o.append(_line(f"  primary NIC on node {loc['nic_numa_node']}  ({nic_note})"))
    o.append(_rule("="))
    return "\n".join(o)
