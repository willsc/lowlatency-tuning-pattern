#!/usr/bin/env python3
"""Draw the four configuration layers as a stack, one figure per shape.

Five bands: the machine at the bottom, then the four layers that configure it, each
carrying its real components and their real CPU lists. Arrows between bands say what the
layer below guarantees the one above; a plan.json rail down the left side shows all four
rendered from one artifact.

The SVG is generated from the planner, so the drawing cannot drift from the plan.

    ./scripts/build-layers-diagram.py [-o docs/layers.html]
"""
import argparse, datetime, html, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docgen_common import CPU_NAME, ORDER, REPO, load_lltune

lltune = load_lltune()

W = 1180
RAIL_X, RAIL_W = 22, 118          # the plan.json rail
BAND_X = 176
BAND_W = W - BAND_X - 22
PAD = 14                          # inside a band
BOX_H = 62
HDR_H = 30
GAP = 52                          # between bands, where the arrows live
VAL_CH = 5.55                     # approx width of one mono char at 9.5px


def esc(s):
    return html.escape(str(s), quote=True)


def wrap(text, width_px, lines=3):
    """Break a cpu list at commas to fit a box, ellipsing if it still will not."""
    cap = max(8, int(width_px / VAL_CH))
    if len(text) <= cap:
        return [text]
    parts, out, cur = text.split(","), [], ""
    for i, p in enumerate(parts):
        piece = p if not cur else cur + "," + p
        if len(piece) <= cap:
            cur = piece
        else:
            out.append(cur + ",")
            cur = p
            if len(out) == lines - 1:
                rest = ",".join(parts[i:])
                out.append(rest if len(rest) <= cap else rest[:cap - 1] + "…")
                return out
    if cur:
        out.append(cur)
    return out[:lines]


class Fig:
    def __init__(self, name, prof, plan):
        self.name, self.prof, self.plan = name, prof, plan
        self.o = []
        self.uid = name.replace(".", "-")

    def add(self, s):
        self.o.append(s)

    # ---------------------------------------------------------------- pieces
    def band(self, y, h, num, title, path, when, tone="plain"):
        self.add(f'<rect x="{BAND_X}" y="{y}" width="{BAND_W}" height="{h}" rx="7" '
                 f'class="band {tone}"/>')
        tx = BAND_X + PAD
        if num:
            self.add(f'<text x="{tx}" y="{y + 20}" class="lnum">LAYER {num}</text>')
            tx += 62
        self.add(f'<text x="{tx}" y="{y + 20}" class="btitle">{esc(title)}</text>')
        if path:
            self.add(f'<text x="{tx + 8 + len(title) * 7.4:.0f}" y="{y + 20}" '
                     f'class="bpath">{esc(path)}</text>')
        if when:
            self.add(f'<text x="{BAND_X + BAND_W - PAD}" y="{y + 20}" '
                     f'class="bwhen end">{esc(when)}</text>')

    def boxes(self, y, items):
        """items: list of (title, value, role class). Widths share the band evenly."""
        n = len(items)
        gap = 10
        bw = (BAND_W - 2 * PAD - gap * (n - 1)) / n
        for i, (title, value, cls) in enumerate(items):
            x = BAND_X + PAD + i * (bw + gap)
            self.add(f'<rect x="{x:.1f}" y="{y}" width="{bw:.1f}" height="{BOX_H}" rx="5" '
                     f'class="box {cls}"/>')
            self.add(f'<rect x="{x:.1f}" y="{y}" width="3.5" height="{BOX_H}" '
                     f'class="rail {cls}"/>')
            self.add(f'<text x="{x + 11:.1f}" y="{y + 18}" class="bxt">{esc(title)}</text>')
            for j, line in enumerate(wrap(value, bw - 22)):
                self.add(f'<text x="{x + 11:.1f}" y="{y + 33 + j * 12}" '
                         f'class="bxv {cls}">{esc(line)}</text>')
        return y + BOX_H

    def arrow(self, y_from, y_to, label, strong=False):
        x = BAND_X + 92
        cls = "flow strong" if strong else "flow"
        self.add(f'<line x1="{x}" y1="{y_from}" x2="{x}" y2="{y_to + 9}" class="{cls}" '
                 f'marker-end="url(#a-{self.uid}{"-s" if strong else ""})"/>')
        self.add(f'<text x="{x + 13}" y="{(y_from + y_to) / 2 + 4:.0f}" '
                 f'class="flowlbl{" strong" if strong else ""}">{esc(label)}</text>')

    def machine(self, y):
        t = self.plan["topology"]
        nodes = self.plan["nodes"]
        h = 44
        inner_w = BAND_W - 2 * PAD
        per_sock = max(1, len(nodes) // t["sockets"])
        sock_gap, node_gap = 18, 9
        sw = (inner_w - sock_gap * (t["sockets"] - 1)) / t["sockets"]
        nw = (sw - node_gap * (per_sock - 1)) / per_sock
        for s in range(t["sockets"]):
            sx = BAND_X + PAD + s * (sw + sock_gap)
            self.add(f'<rect x="{sx:.1f}" y="{y}" width="{sw:.1f}" height="{h}" rx="5" '
                     f'class="socket"/>')
            self.add(f'<text x="{sx + sw / 2:.1f}" y="{y + h + 13}" '
                     f'class="socklbl mid">socket {s}</text>')
            for k in range(per_sock):
                nd = nodes[s * per_sock + k]
                nx = sx + 6 + k * (nw + node_gap)
                w = nw - 12 / per_sock
                total = nd["cores_total"]
                cx = nx
                for role, cls in (("housekeeping", "hk"), ("irqnet", "irq"),
                                  ("shared", "shr"), ("exclusive", "exc")):
                    frac = len(lltune.expand(nd[role]["cpus"])) / total
                    self.add(f'<rect x="{cx:.1f}" y="{y + 20}" width="{w * frac:.2f}" '
                             f'height="15" class="core {cls}"/>')
                    cx += w * frac
                nic = " · NIC" if nd["node"] == t["nic_numa_node"] else ""
                self.add(f'<text x="{nx:.1f}" y="{y + 14}" class="nodelbl">'
                         f'node {nd["node"]}{nic}</text>')
                self.add(f'<text x="{nx + w:.1f}" y="{y + 14}" class="nodelbl end">'
                         f'{total}c</text>')
        return y + h + 18

    def rail(self, y_top, targets):
        cx = RAIL_X + RAIL_W / 2
        self.add(f'<rect x="{RAIL_X}" y="{y_top}" width="{RAIL_W}" height="34" rx="5" '
                 f'class="planbox"/>')
        self.add(f'<text x="{cx}" y="{y_top + 21}" class="plantxt mid">plan.json</text>')
        self.add(f'<line x1="{cx}" y1="{y_top + 34}" x2="{cx}" y2="{targets[-1]}" '
                 f'class="railline"/>')
        for ty in targets:
            self.add(f'<line x1="{cx}" y1="{ty}" x2="{BAND_X - 4}" y2="{ty}" '
                     f'class="railline" marker-end="url(#r-{self.uid})"/>')
        self.add(f'<text x="{cx}" y="{targets[-1] + 20}" class="railtxt mid">rendered</text>')
        self.add(f'<text x="{cx}" y="{targets[-1] + 31}" class="railtxt mid">into all four</text>')


def figure(name, prof, plan):
    f = Fig(name, prof, plan)
    t = plan["topology"]
    hk = plan["roles"]["housekeeping"]["cpus"]
    irq = plan["roles"]["irqnet"]["cpus"]
    shr = plan["roles"]["shared"]["cpus"]
    exc = plan["roles"]["exclusive"]["cpus"]
    hk_irq = plan["irq_landing_cpus"]
    non_iso = plan["non_isolated_cpus"]
    hk_shr = lltune.compress(lltune.expand(hk) + lltune.expand(shr))
    n_irq = len(lltune.expand(irq))

    l1 = [("isolcpus", exc, "exc"), ("nohz_full", exc, "exc"),
          ("rcu_nocbs", exc, "exc"), ("irqaffinity", hk_irq, "irq")]
    if t["threads_per_core"] > 1:
        l1.insert(0, ("nosmt=force", "siblings never onlined", "plain"))
    l2 = [("system.slice", hk_shr, "shr"),
          ("user · machine · init", hk, "hk"),
          ("irqnet.slice", irq, "irq"),
          ("pulsar.slice", exc, "exc")]
    l3 = [("IRQ affinity", hk_irq, "irq"),
          ("ENA combined queues", f"{n_irq} queues = irqnet cores", "irq"),
          ("workqueue cpumask", non_iso, "shr"),
          ("XPS tx queues", exc, "exc")]
    l4 = [("SHARED_CORES", shr, "shr"), ("EXCLUSIVE_CORES", exc, "exc")]

    band_h = HDR_H + BOX_H + PAD
    y = 12
    tops = []

    # layer 4 at the top, machine at the bottom
    for num, title, path, when, items, tone in (
        ("4", "Application contract", "/etc/lowlatency/cores.env", "read at start", l4, "plain"),
        ("3", "Runtime pinning", "scripts/apply-runtime.sh", "every boot", l3, "plain"),
        ("2", "cgroup v2 cpusets", "/etc/systemd/system/", "daemon-reload · immediate", l2, "plain"),
        ("1", "Kernel boot", "/etc/default/grub.d/99-lowlatency.cfg", "at boot · reboot", l1, "plain"),
    ):
        tops.append(y)
        f.band(y, band_h, num, title, path, when, tone)
        f.boxes(y + HDR_H, items)
        y += band_h + GAP

    mach_top = y
    mach_h = HDR_H + 62 + PAD
    f.band(y, mach_h, "", "The machine", prof["metal_sku"],
           f"{t['total_cores']} cores · {t['numa_nodes']} NUMA "
           f"node{'s' if t['numa_nodes'] > 1 else ''}", "hw")
    f.machine(y + HDR_H)
    bottom = y + mach_h

    # arrows, drawn bottom-up: what each layer hands the one above it
    labels = [
        (mach_top, tops[3] + band_h, "the hardware: cores, dies, NUMA distance"),
        (tops[3], tops[2] + band_h,
         "AllowedCPUs must equal isolcpus — the one hard constraint between layers", True),
        (tops[2], tops[1] + band_h, "cgroups fence tasks; they cannot move an interrupt"),
        (tops[1], tops[0] + band_h, "the app is handed the same list, as a string"),
    ]
    for spec in labels:
        f.arrow(spec[0], spec[1], spec[2], len(spec) > 3)

    f.rail(tops[0], [t_ + 15 for t_ in tops])

    H = bottom + 16
    label = (f"{name}: the four configuration layers stacked above the machine, each "
             f"rendered from one plan.json")
    defs = (f'<defs>'
            f'<marker id="a-{f.uid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
            f'markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M0,1 L9,5 L0,9 z" class="head"/></marker>'
            f'<marker id="a-{f.uid}-s" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" '
            f'markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M0,1 L9,5 L0,9 z" class="head strong"/></marker>'
            f'<marker id="r-{f.uid}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" '
            f'markerHeight="5" orient="auto-start-reverse">'
            f'<path d="M0,1 L9,5 L0,9 z" class="rhead"/></marker>'
            f'</defs>')
    return (f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(label)}" class="fig">'
            f'{defs}{"".join(f.o)}</svg>')


def build():
    policy = lltune.load_policy()
    out = []
    for name in ORDER:
        prof = lltune.load_profile(name)
        plan = lltune.build_plan(lltune.topology_from_profile(prof), policy, name)
        t = plan["topology"]
        out.append({
            "id": name, "sku": prof["metal_sku"], "cpu": CPU_NAME.get(name, ""),
            "chips": [f"{t['sockets']} socket{'s' if t['sockets'] > 1 else ''} x "
                      f"{t['total_cores'] // t['sockets']} cores",
                      f"{prof['vcpus']} vCPU -> {t['total_cores']} usable",
                      f"{t['numa_nodes']} NUMA node{'s' if t['numa_nodes'] > 1 else ''}"
                      f" · {prof.get('numa_mode', '').split(' - ')[0]}",
                      f"L3 domain {t['cores_per_l3']} cores"],
            "svg": figure(name, prof, plan),
            "excl": len(lltune.expand(plan["roles"]["exclusive"]["cpus"])),
            "total": t["total_cores"], "nodes": t["numa_nodes"],
        })
    return out


TEMPLATE = Path(__file__).resolve().parent / "layers-diagram.template.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=str(REPO / "docs" / "layers.html"))
    args = ap.parse_args()
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
            f'<b>{p["excl"]} of {p["total"]} cores end up isolated.</b> Read the stack '
            f'upward: the machine offers {p["total"]} cores in {p["nodes"]} NUMA '
            f'domain{"s" if p["nodes"] > 1 else ""}, the kernel drops the exclusive set from '
            f'its scheduler domains, systemd fences that same set in a cpuset, the runtime '
            f'steers interrupts and kernel threads away from it, and the application is '
            f'handed the list as a string. The arrow between layers 1 and 2 is the only hard '
            f'constraint in the design — a <code>pulsar.slice</code> whose '
            f'<code>AllowedCPUs</code> disagreed with <code>isolcpus</code> would keep serving '
            f'traffic while its tail latency doubled. Nothing checks it at runtime, so all '
            f'four layers are rendered from one <code>plan.json</code> instead.'
            f'</figcaption></figure></section>')
    tabs = "".join(
        f'<button class="tab" role="tab" data-id="{p["id"]}" '
        f'aria-selected="{"true" if i == 0 else "false"}">{esc(p["id"])}</button>'
        for i, p in enumerate(figs))
    page = (TEMPLATE.read_text()
            .replace("<!--__TABS__-->", tabs)
            .replace("<!--__PANELS__-->", "".join(panels))
            .replace("<!--__DATE__-->",
                     datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")))
    Path(args.out).write_text(page)
    print(f"wrote {args.out}  ({len(figs)} figures, {len(page) // 1024} KiB)")


if __name__ == "__main__":
    main()
