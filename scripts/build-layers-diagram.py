#!/usr/bin/env python3
"""Draw the four configuration layers, one figure per shape.

The figure is one horizontal CPU axis with the machine's cores along the top and each
layer's coverage drawn beneath it in the same coordinate space. That is the whole point:
isolcpus, pulsar.slice's AllowedCPUs and EXCLUSIVE_CORES are three different kernel
mechanisms describing one set of cores, and drawn this way they either line up or they
visibly do not. A shaded column runs the height of the figure behind the isolated cores.

The SVG is generated from the planner, so the picture cannot drift from the plan.

    ./scripts/build-layers-diagram.py [-o docs/layers.html]
"""
import argparse, datetime, html, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docgen_common import CPU_NAME, ORDER, REPO, load_lltune

lltune = load_lltune()

# ---------------------------------------------------------------- geometry
W = 1280
GUT = 214          # left gutter: layer names and mechanism labels
PLOT_X = GUT
PLOT_W = 916       # drawing area for the cores
RIGHT_X = PLOT_X + PLOT_W + 16
NODE_GAP = 9
CELL_MAX = 20
ROW_H = 19         # one mechanism row
BAR_H = 11
LAYER_PAD = 13

ROLES = [("housekeeping", "hk", "housekeeping"),
         ("irqnet", "irq", "irq / net"),
         ("shared", "shr", "shared_cores"),
         ("exclusive", "exc", "exclusive_cores")]


class Axis:
    """Maps a cpu number to an x position, with a gap between NUMA nodes."""

    def __init__(self, plan):
        self.node_cpus, self.cpu_node, self.index = {}, {}, {}
        for n in plan["nodes"]:
            cpus = sorted(c for r in ("housekeeping", "irqnet", "shared", "exclusive")
                          for c in lltune.expand(n[r]["cpus"]))
            self.node_cpus[n["node"]] = cpus
            for i, c in enumerate(cpus):
                self.cpu_node[c] = n["node"]
                self.index[c] = i
        self.nodes = sorted(self.node_cpus)
        total = sum(len(v) for v in self.node_cpus.values())
        gaps = NODE_GAP * (len(self.nodes) - 1)
        self.cell = min(CELL_MAX, (PLOT_W - gaps) / total)
        used = total * self.cell + gaps
        self.x0 = PLOT_X + (PLOT_W - used) / 2
        self.node_x = {}
        x = self.x0
        for nd in self.nodes:
            self.node_x[nd] = x
            x += len(self.node_cpus[nd]) * self.cell + NODE_GAP

    def x(self, cpu):
        return self.node_x[self.cpu_node[cpu]] + self.index[cpu] * self.cell

    def node_span(self, nd):
        return self.node_x[nd], len(self.node_cpus[nd]) * self.cell

    def runs(self, spec):
        """Contiguous (x, width) runs for a cpu list, broken at NUMA node boundaries."""
        cpus = sorted(lltune.expand(spec))
        out, run = [], []
        for c in cpus:
            if run and c == run[-1] + 1 and self.cpu_node[c] == self.cpu_node[run[-1]]:
                run.append(c)
            else:
                if run:
                    out.append(run)
                run = [c]
        if run:
            out.append(run)
        return [(self.x(r[0]), len(r) * self.cell) for r in out]


def esc(s):
    return html.escape(str(s), quote=True)


def bars(ax, spec, y, role_cpus):
    cpus = set(lltune.expand(spec))
    out = []
    for role, cls, _ in ROLES:
        sub = sorted(cpus & role_cpus[role])
        if not sub:
            continue
        for x, w in ax.runs(lltune.compress(sub)):
            out.append(f'<rect x="{x:.1f}" y="{y}" width="{max(w, 1.5):.1f}" '
                       f'height="{BAR_H}" rx="1.5" class="bar {cls}"/>')
    return "".join(out)


def count(spec):
    n = len(lltune.expand(spec))
    return f"{n} cpu" + ("s" if n != 1 else "")


def figure(name, prof, plan):
    ax = Axis(plan)
    t = plan["topology"]
    slices = lltune.render_slices(plan)
    hk = plan["roles"]["housekeeping"]["cpus"]
    irq = plan["roles"]["irqnet"]["cpus"]
    shr = plan["roles"]["shared"]["cpus"]
    exc = plan["roles"]["exclusive"]["cpus"]
    hk_irq = plan["irq_landing_cpus"]
    non_iso = plan["non_isolated_cpus"]
    hk_shr = lltune.compress(lltune.expand(hk) + lltune.expand(shr))
    role_cpus = {r: set(lltune.expand(plan["roles"][r]["cpus"]))
                 for r, _, _ in ROLES}

    # ---- layers: (number, title, path, when, [(mechanism, cpuspec, role class)])
    layers = [
        ("1", "Boot isolation", "/etc/default/grub.d/99-lowlatency.cfg", "at boot · reboot", [
            ("isolcpus=", exc),
            ("nohz_full= / rcu_nocbs=", exc),
            ("irqaffinity=", hk_irq),
        ]),
        ("2", "cgroup v2 cpusets", "/etc/systemd/system/", "daemon-reload · immediate", [
            ("system.slice", hk_shr),
            ("irqnet.slice", irq),
            ("pulsar.slice", exc),
        ]),
        ("3", "Runtime pinning", "scripts/apply-runtime.sh", "every boot", [
            ("IRQ affinity", hk_irq),
            ("workqueue cpumask", non_iso),
            ("XPS tx queues", exc),
        ]),
        ("4", "Application contract", "/etc/lowlatency/cores.env", "read at start", [
            ("SHARED_CORES=", shr),
            ("EXCLUSIVE_CORES=", exc),
        ]),
    ]

    o = []
    y = 34

    # ---- NUMA node headers -------------------------------------------------
    node_hdr = y
    for nd in ax.nodes:
        nx, nw = ax.node_span(nd)
        nic = " · NIC" if nd == t["nic_numa_node"] else ""
        o.append(f'<line x1="{nx:.1f}" y1="{y+7}" x2="{nx+nw:.1f}" y2="{y+7}" class="tick"/>')
        o.append(f'<text x="{nx + nw/2:.1f}" y="{y}" class="lbl mid">node {nd}{nic}</text>')
    y += 16

    # ---- the machine: one cell per core ------------------------------------
    machine_y = y
    for role, cls, _ in ROLES:
        for nd in plan["nodes"]:
            for x, w in ax.runs(nd[role]["cpus"]):
                o.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="22" '
                         f'class="core {cls}"/>')
    # L3 domain divisions, where they fall inside a node
    l3 = t["cores_per_l3"]
    if 0 < l3 < len(ax.node_cpus[ax.nodes[0]]):
        for nd in ax.nodes:
            cpus = ax.node_cpus[nd]
            for i in range(l3, len(cpus), l3):
                lx = ax.x(cpus[i])
                o.append(f'<line x1="{lx:.1f}" y1="{y}" x2="{lx:.1f}" y2="{y+22}" class="l3"/>')
    o.append(f'<text x="{PLOT_X - 12}" y="{y + 15}" class="lbl end">the machine</text>')
    y += 22

    # cpu number ruler at each node's first and last core
    for nd in ax.nodes:
        cpus = ax.node_cpus[nd]
        nx, nw = ax.node_span(nd)
        o.append(f'<text x="{nx:.1f}" y="{y + 11}" class="num">{cpus[0]}</text>')
        o.append(f'<text x="{nx + nw:.1f}" y="{y + 11}" class="num end">{cpus[-1]}</text>')
    y += 22

    o.append(f'<text x="20" y="{y + 10}" class="src">plan.json</text>')
    y += 17

    # ---- shaded column behind the isolated cores ---------------------------
    body_top = y
    header_ys = []
    layers_h = sum(LAYER_PAD + 29 + len(rows) * ROW_H for _, _, _, _, rows in layers)
    shade = "".join(
        f'<rect x="{x:.1f}" y="{machine_y}" width="{w:.1f}" '
        f'height="{layers_h + (body_top - machine_y)}" class="shade"/>'
        for x, w in ax.runs(exc))

    # ---- layer bands -------------------------------------------------------
    for num, title, path, when, rows in layers:
        y += LAYER_PAD
        o.append(f'<line x1="20" y1="{y - 7}" x2="{W - 20}" y2="{y - 7}" class="sep"/>')
        header_ys.append(y + 5)
        o.append(f'<text x="46" y="{y + 9}" class="lnum">LAYER {num}</text>')
        o.append(f'<text x="110" y="{y + 9}" class="ltitle">{esc(title)}</text>')
        o.append(f'<text x="{W - 20}" y="{y + 9}" class="lwhen end">{esc(when)}</text>')
        y += 14
        o.append(f'<text x="110" y="{y + 9}" class="lpath">{esc(path)}</text>')
        y += 15
        for mech, spec in rows:
            o.append(f'<text x="{PLOT_X - 12}" y="{y + 9}" class="mech end">{esc(mech)}</text>')
            o.append(bars(ax, spec, y, role_cpus))
            o.append(f'<text x="{RIGHT_X}" y="{y + 9}" class="num">{count(spec)}</text>')
            y += ROW_H

    # provenance spine: one plan, four layers rendered from it
    if header_ys:
        o.append(f'<line x1="30" y1="{body_top + 2}" x2="30" y2="{header_ys[-1]}" '
                 f'class="spine"/>')
        for hy in header_ys:
            o.append(f'<line x1="30" y1="{hy}" x2="40" y2="{hy}" class="spine"/>')
            o.append(f'<circle cx="30" cy="{hy}" r="2.4" class="spinedot"/>')

    H = y + 16
    label = (f"{name}: the same core partition expressed by all four layers - "
             f"{count(exc)} isolated, aligned across kernel, cgroup, runtime and app contract")
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}" '
            f'class="fig">{shade}{"".join(o)}</svg>')


def build():
    policy = lltune.load_policy()
    out = []
    for name in ORDER:
        prof = lltune.load_profile(name)
        plan = lltune.build_plan(lltune.topology_from_profile(prof), policy, name)
        t = plan["topology"]
        chips = [
            f"{t['sockets']} socket{'s' if t['sockets'] > 1 else ''} x "
            f"{t['total_cores'] // t['sockets']} cores",
            f"{prof['vcpus']} vCPU -> {t['total_cores']} usable",
            f"{t['numa_nodes']} NUMA node{'s' if t['numa_nodes'] > 1 else ''}"
            f" · {prof.get('numa_mode', '').split(' - ')[0]}",
            f"L3 domain {t['cores_per_l3']} cores",
        ]
        out.append({
            "id": name,
            "sku": prof["metal_sku"],
            "cpu": CPU_NAME.get(name, ""),
            "chips": chips,
            "svg": figure(name, prof, plan),
            "excl": len(lltune.expand(plan["roles"]["exclusive"]["cpus"])),
            "total": t["total_cores"],
            "nodes": t["numa_nodes"],
        })
    return out


TEMPLATE = Path(__file__).resolve().parent / "layers-diagram.template.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=str(REPO / "docs" / "layers.html"))
    args = ap.parse_args()
    tpl = TEMPLATE.read_text()
    figs = build()
    panels = []
    for i, p in enumerate(figs):
        chips = "".join(f'<span class="chip">{esc(c)}</span>' for c in p["chips"])
        panels.append(
            f'<section class="panel" id="p-{p["id"]}"{"" if i == 0 else " hidden"}>'
            f'<h2>{esc(p["id"])}</h2>'
            f'<p class="sub">{esc(p["sku"])} · {esc(p["cpu"])}</p>'
            f'<div class="chips">{chips}</div>'
            f'<figure><div class="scroll">{p["svg"]}</div><figcaption>'
            f'<b>{p["excl"]} of {p["total"]} cores are isolated</b>, in '
            f'{p["nodes"]} block{"s" if p["nodes"] > 1 else ""} — one per NUMA node, which '
            f'is why node count and not core count drives the overhead. The shaded columns '
            f'are that set. Four unrelated mechanisms describe it — <code>isolcpus</code>, '
            f'<code>pulsar.slice</code>\u2019s <code>AllowedCPUs</code>, the XPS map and '
            f'<code>EXCLUSIVE_CORES</code> — and each of those rows fills the shaded columns '
            f'exactly. The rows that stop at the shade are its complement: '
            f'<code>irqaffinity</code>, <code>irqnet.slice</code> and the workqueue cpumask '
            f'are where the work that must stay <em>off</em> the isolated cores is sent. '
            f'Nothing reconciles the four at runtime — a layer that disagreed would keep '
            f'serving traffic — so all four are rendered from one <code>plan.json</code>.'
            f'</figcaption></figure></section>')
    tabs = "".join(
        f'<button class="tab" role="tab" data-id="{p["id"]}" '
        f'aria-selected="{"true" if i == 0 else "false"}">{esc(p["id"])}</button>'
        for i, p in enumerate(figs))
    page = (tpl.replace("<!--__TABS__-->", tabs)
               .replace("<!--__PANELS__-->", "".join(panels))
               .replace("<!--__DATE__-->",
                        datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")))
    Path(args.out).write_text(page)
    print(f"wrote {args.out}  ({len(figs)} figures, {len(page) // 1024} KiB)")


if __name__ == "__main__":
    main()
