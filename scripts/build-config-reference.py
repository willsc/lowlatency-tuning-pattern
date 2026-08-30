#!/usr/bin/env python3
"""Generate docs/CONFIG-REFERENCE.md: the boot isolation and cgroup layers, every shape.

The document is rendered from the planner, exactly like the files install.sh writes to
disk. Hand-maintaining CPU lists in a document is the same failure mode the planner
exists to prevent, one level up: the doc drifts, someone trusts it, and the box they
build from it is subtly wrong.

    ./scripts/build-config-reference.py [-o docs/CONFIG-REFERENCE.md]
"""
import argparse, datetime, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docgen_common import ARG_GROUPS, CPU_NAME, ORDER, REPO, WHY, load_lltune

lltune = load_lltune()

GRUB_PATH = "/etc/default/grub.d/99-lowlatency.cfg"
SYSTEMD_DIR = "/etc/systemd/system"

# The three units that are handed the housekeeping set and nothing else. They are
# rendered identically, so the tables collapse them into one column rather than
# repeating the same CPU list three times.
HK_ONLY = ("user.slice", "machine.slice", "init.scope")

SLICE_ROLE = {
    "system.slice": ("housekeeping + shared_cores", "no",
                     "Ordinary services, plus the application's shared pool: GC, JIT, "
                     "admin endpoints, compaction."),
    "user.slice": ("housekeeping", "no", "Login sessions. Kept off the shared pool so an "
                                         "ssh session cannot land on an application core."),
    "machine.slice": ("housekeeping", "no", "Containers and VMs, for the same reason."),
    "init.scope": ("housekeeping", "no", "PID 1 itself."),
    "irqnet.slice": ("irqnet", "no", "NIC and NVMe interrupt handling, softirq, irqbalance."),
    "pulsar.slice": ("exclusive_cores", "**yes**",
                     "The latency path. This cpuset exists because `isolcpus` is a real "
                     "kernel guarantee worth gating access to."),
}


def unit_path(name):
    """Render key -> the path install.sh writes."""
    if name.endswith("/10-lowlatency.conf"):
        return f"{SYSTEMD_DIR}/{name}"
    return f"{SYSTEMD_DIR}/{name}"


def unit_label(name):
    """Render key -> the unit it modifies."""
    return name.split(".d/")[0]


def plans():
    policy = lltune.load_policy()
    for name in ORDER:
        prof = lltune.load_profile(name)
        plan = lltune.build_plan(lltune.topology_from_profile(prof), policy, name)
        yield name, prof, plan


def cpus_in(spec):
    return len(lltune.expand(spec))


def arg_key(a):
    return a.split("=")[0]


def split_invariant(all_cmdlines):
    """Args identical on all shapes vs args that differ (or are absent on some)."""
    common = set(all_cmdlines[0])
    for c in all_cmdlines[1:]:
        common &= set(c)
    seen, varying = set(), []
    for c in all_cmdlines:
        for a in c:
            if a not in common and arg_key(a) not in seen:
                seen.add(arg_key(a))
                varying.append(arg_key(a))
    invariant = [a for a in all_cmdlines[0] if a in common]
    return invariant, varying


def topo_line(prof, plan):
    t = plan["topology"]
    return (f"{prof['metal_sku']} · {CPU_NAME.get(prof['name'], '')} · "
            f"{t['sockets']} socket{'s' if t['sockets'] > 1 else ''} × "
            f"{t['total_cores'] // t['sockets']} cores · "
            f"{t['numa_nodes']} NUMA node{'s' if t['numa_nodes'] > 1 else ''} "
            f"({prof.get('numa_mode', '').split(' - ')[0]}) · "
            f"L3 domain {t['cores_per_l3']} cores · SMT "
            f"{'2 → disabled at boot' if t['threads_per_core'] > 1 else 'already off (AWS)'}")


def build():
    rows = list(plans())
    cmdlines = [p["cmdline"].split() for _, _, p in rows]
    invariant, varying = split_invariant(cmdlines)
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    o = []
    w = o.append

    w("# Configuration reference — boot isolation and cgroup slices\n")
    w(f"> **Generated** by `scripts/build-config-reference.py` on {today}. Do not hand-edit:\n"
      f"> every list below is rendered from the planner, so it is byte-for-byte what\n"
      f"> `scripts/install.sh` writes to disk for that shape. Regenerate after any change to\n"
      f"> `profiles/policy.json` or a profile.\n")
    w("This document covers the two layers that need a file on disk:\n")
    w(f"| Layer | File | When it takes effect |\n|---|---|---|\n"
      f"| **1 — boot isolation** | `{GRUB_PATH}` | at boot; **requires a reboot** |\n"
      f"| **2 — cgroup v2 slices** | `{SYSTEMD_DIR}/` | `systemctl daemon-reload`; immediate |\n")
    w("Layers 3 (runtime IRQ/queue pinning) and 4 (the application's `cores.env` contract) are\n"
      "not files you edit — see `docs/layers.html` or `lltune layers --profile <name>`.\n")
    w("The two layers are not independent. `pulsar.slice`'s `AllowedCPUs` **is** the `isolcpus`\n"
      "set, and both are rendered from the same `plan.json` field. A cgroup that let a thread\n"
      "onto a core the kernel had not isolated, or isolated a core no cgroup could reach, is\n"
      "the exact class of bug this repo is built to make impossible.\n")

    # ---------------------------------------------------------------- contents
    w("## Contents\n")
    w("- [Summary: the isolation set for every shape](#summary-the-isolation-set-for-every-shape)\n"
      "- [Part 1 — Boot isolation (GRUB)](#part-1--boot-isolation-grub)\n"
      "  - [1.1 Arguments identical on every shape](#11-arguments-identical-on-every-shape)\n"
      "  - [1.2 Arguments that vary by shape](#12-arguments-that-vary-by-shape)\n"
      "  - [1.3 The installed file, per shape](#13-the-installed-file-per-shape)\n"
      "- [Part 2 — cgroup v2 slices](#part-2--cgroup-v2-slices)\n"
      "  - [2.1 The slice map](#21-the-slice-map)\n"
      "  - [2.2 AllowedCPUs for every shape](#22-allowedcpus-for-every-shape)\n"
      "  - [2.3 The installed unit files, per shape](#23-the-installed-unit-files-per-shape)\n"
      "- [Part 3 — Invariants that tie the two layers together](#part-3--invariants-that-tie-the-two-layers-together)\n")

    # ---------------------------------------------------------------- summary
    w("## Summary: the isolation set for every shape\n")
    w("`isolcpus` carries the exclusive cores; `irqaffinity` carries everything that is left\n"
      "after them, minus the shared pool — housekeeping plus irqnet. Housekeeping and IRQ cores\n"
      "are reserved **per NUMA node**, which is why node count, not core count, drives the\n"
      "platform's overhead.\n")
    w("| Profile | Metal SKU | NUMA | Cores | `isolcpus` (exclusive) | # | `irqaffinity` (housekeeping + irqnet) | # |")
    w("|---|---|---:|---:|---|---:|---|---:|")
    for name, prof, plan in rows:
        t = plan["topology"]
        w(f"| `{name}` | `{prof['metal_sku']}` | {t['numa_nodes']} | {t['total_cores']} "
          f"| `{plan['isolated_cpus']}` | {plan['roles']['exclusive']['cpu_count']} "
          f"| `{plan['irq_landing_cpus']}` | {cpus_in(plan['irq_landing_cpus'])} |")
    w("")

    # ---------------------------------------------------------------- part 1
    w("## Part 1 — Boot isolation (GRUB)\n")
    w(f"`scripts/install.sh` writes `{GRUB_PATH}` and re-runs `update-grub` / `grub2-mkconfig`.\n"
      "On AL2023 and RHEL, which have no `grub.d`, it patches `/etc/default/grub` in place after\n"
      "taking a timestamped backup, replacing any previous lltune block rather than appending a\n"
      "second one.\n")

    w("### 1.1 Arguments identical on every shape\n")
    w("These carry no CPU list, so they are the same string on all seven profiles.\n")
    inv = set(invariant)
    for group, args in ARG_GROUPS:
        mine = [a for a in invariant if arg_key(a) in {k for k, _ in args}]
        if not mine:
            continue
        w(f"**{group}**\n")
        w("| Argument | Why |\n|---|---|")
        for a in mine:
            w(f"| `{a}` | {WHY.get(arg_key(a), '')} |")
        w("")

    w("### 1.2 Arguments that vary by shape\n")
    # An arg that appears on every shape but with a different value carries a CPU list;
    # one that is absent from some shapes is conditional on the silicon.
    present = {k: sum(1 for c in cmdlines if any(arg_key(a) == k for a in c)) for k in varying}
    listy = [k for k in varying if present[k] == len(cmdlines)]
    cond = [k for k in varying if present[k] < len(cmdlines)]
    w(f"{len(listy)} arguments carry a CPU list (`{'`, `'.join(listy)}`) and {len(cond)} "
      f"{'are' if len(cond) != 1 else 'is'} conditional on the silicon "
      f"(`{'`, `'.join(cond)}`). Everything in 1.1 is constant. This is the whole of what "
      f"changes between shapes at the boot layer.\n")
    for key in varying:
        w(f"**`{key}`** — {WHY.get(key, '')}\n")
        w("| Profile | Value |\n|---|---|")
        for (name, _, plan), cmd in zip(rows, cmdlines):
            mine = [a for a in cmd if arg_key(a) == key]
            val = f"`{mine[0]}`" if mine else "_not set_"
            if not mine:
                why = ("SMT already off on this AMD shape" if key == "nosmt"
                       else "AMD shape — `intel_idle` is not the idle driver" if key.startswith("intel_idle")
                       else "not applicable")
                val = f"— _{why}_"
            w(f"| `{name}` | {val} |")
        w("")

    w("### 1.3 The installed file, per shape\n")
    w(f"Verbatim contents of `{GRUB_PATH}`. The cmdline is one line; it is shown wrapped by your\n"
      "viewer, not by the file.\n")
    for name, prof, plan in rows:
        w(f"#### `{name}`\n")
        w(f"{topo_line(prof, plan)}\n")
        w("```sh")
        w(lltune.render_grub(plan).rstrip())
        w("```\n")
        w("<details><summary>Same cmdline, one argument per line</summary>\n")
        w("```")
        for a in plan["cmdline"].split():
            w(a)
        w("```\n</details>\n")

    # ---------------------------------------------------------------- part 2
    w("## Part 2 — cgroup v2 slices\n")
    keys = list(lltune.render_slices(rows[0][2]))
    dropins = [k for k in keys if ".d/" in k]
    w(f"{len(keys)} unit files: {len(keys) - len(dropins)} new slices "
      f"(`{'`, `'.join(k for k in keys if '.d/' not in k)}`), and {len(dropins)} drop-ins that "
      f"narrow units systemd already ships "
      f"(`{'`, `'.join(unit_label(k) for k in dropins)}`). Drop-ins are used rather than full "
      "replacements so a distribution update to the underlying unit is not lost.\n")
    w("`cgroup v2` is required — `AllowedCPUs` is a v2-only property, which is why\n"
      "`systemd.unified_cgroup_hierarchy=1` and `cgroup_no_v1=all` are on the cmdline in Part 1.\n")

    w("### 2.1 The slice map\n")
    w("| Unit | Holds | Isolated | What runs there |\n|---|---|---|---|")
    for unit, (holds, iso, what) in SLICE_ROLE.items():
        w(f"| `{unit}` | {holds} | {iso} | {what} |")
    w("")
    w("Every core on the box belongs to exactly one of `system.slice`, `irqnet.slice` and\n"
      "`pulsar.slice`; `scripts/selftest.py` asserts that partition holds for every profile.\n"
      "`user.slice`, `machine.slice` and `init.scope` are subsets of the housekeeping set, not\n"
      "extra partitions.\n")

    w("### 2.2 AllowedCPUs for every shape\n")
    w(f"`{'`, `'.join(HK_ONLY)}` are rendered identically — all three get the housekeeping set —\n"
      "so they share one column.\n")
    w("| Profile | `system.slice` | " + ", ".join(f"`{u}`" for u in HK_ONLY) +
      " | `irqnet.slice` | `pulsar.slice` |")
    w("|---|---|---|---|---|")
    for name, _, plan in rows:
        s = lltune.render_slices(plan)
        def allowed(key):
            return re.search(r"AllowedCPUs=(\S+)", s[key]).group(1)
        w(f"| `{name}` | `{allowed('system.slice.d/10-lowlatency.conf')}` "
          f"| `{allowed('user.slice.d/10-lowlatency.conf')}` "
          f"| `{allowed('irqnet.slice')}` | `{allowed('pulsar.slice')}` |")
    w("")
    w("| Profile | `system.slice` | housekeeping-only | `irqnet.slice` | `pulsar.slice` | total |")
    w("|---|---:|---:|---:|---:|---:|")
    for name, _, plan in rows:
        s = lltune.render_slices(plan)
        n = {k: cpus_in(re.search(r"AllowedCPUs=(\S+)", v).group(1)) for k, v in s.items()}
        w(f"| `{name}` | {n['system.slice.d/10-lowlatency.conf']} "
          f"| {n['user.slice.d/10-lowlatency.conf']} | {n['irqnet.slice']} | {n['pulsar.slice']} "
          f"| {plan['topology']['total_cores']} |")
    w("\n_Counts are CPUs, and the three partition columns sum to the total: housekeeping-only is\n"
      "a subset of `system.slice`, not an addition to it._\n")

    w("### 2.3 The installed unit files, per shape\n")
    w(f"Verbatim contents, at the path `scripts/install.sh` writes them to.\n")
    for name, prof, plan in rows:
        w(f"#### `{name}`\n")
        w(f"{topo_line(prof, plan)}\n")
        for key, body in lltune.render_slices(plan).items():
            w(f"**`{unit_path(key)}`**\n")
            w("```ini")
            w(body.rstrip())
            w("```\n")

    # ---------------------------------------------------------------- part 3
    w("## Part 3 — Invariants that tie the two layers together\n")
    w("Checked at generation time across all seven profiles, and again on the host by\n"
      "`lltune validate` (which reads `/proc/cmdline` and the live cpusets, not this document).\n")
    checks = [
        ("`pulsar.slice` `AllowedCPUs` == the `isolcpus` list on the cmdline",
         lambda p, s: allowed_of(s, "pulsar.slice") == p["isolated_cpus"]
                      == arg_val(p, "isolcpus").split("domain,", 1)[1]),
        ("`isolcpus` == `nohz_full` == `rcu_nocbs`",
         lambda p, s: arg_val(p, "nohz_full") == p["isolated_cpus"]
                      and arg_val(p, "rcu_nocbs") == p["isolated_cpus"]),
        ("`irqaffinity` shares no CPU with `isolcpus`",
         lambda p, s: not (set(lltune.expand(arg_val(p, "irqaffinity")))
                           & set(lltune.expand(p["isolated_cpus"])))),
        ("cpu0 is housekeeping, never isolated",
         lambda p, s: 0 in lltune.expand(p["roles"]["housekeeping"]["cpus"])),
        ("`system.slice` + `irqnet.slice` + `pulsar.slice` partition every core",
         lambda p, s: partition_ok(p, s)),
        ("housekeeping-only units are a subset of `system.slice`",
         lambda p, s: set(lltune.expand(allowed_of(s, "user.slice.d/10-lowlatency.conf")))
                      <= set(lltune.expand(allowed_of(s, "system.slice.d/10-lowlatency.conf")))),
        ("every slice gets every NUMA node in `AllowedMemoryNodes`",
         lambda p, s: all(re.search(r"AllowedMemoryNodes=(\S+)", v).group(1) == p["memory_nodes"]
                          for v in s.values())),
    ]
    w("| Invariant | Holds for |\n|---|---|")
    failures = []
    for label, fn in checks:
        ok = []
        for name, _, plan in rows:
            s = lltune.render_slices(plan)
            if fn(plan, s):
                ok.append(name)
            else:
                failures.append((label, name))
        w(f"| {label} | {'all 7 shapes' if len(ok) == len(rows) else ', '.join(ok) or 'NONE'} |")
    w("")
    if failures:
        for label, name in failures:
            print(f"INVARIANT FAILED: {label} on {name}", file=sys.stderr)
        sys.exit(f"{len(failures)} invariant failure(s); refusing to write a document that "
                 f"documents a broken plan")
    w("---\n")
    w(f"Regenerate: `./scripts/build-config-reference.py` · "
      f"terminal equivalent: `./bin/lltune layers --profile <name>` · "
      f"live host: `./bin/lltune render --live -o out/`\n")
    return "\n".join(o)


def allowed_of(slices, key):
    return re.search(r"AllowedCPUs=(\S+)", slices[key]).group(1)


def arg_val(plan, key):
    for a in plan["cmdline"].split():
        if a.split("=")[0] == key:
            return a.split("=", 1)[1]
    return None


def partition_ok(plan, slices):
    parts = []
    for key in ("system.slice.d/10-lowlatency.conf", "irqnet.slice", "pulsar.slice"):
        parts.append(set(lltune.expand(allowed_of(slices, key))))
    union = set().union(*parts)
    disjoint = all(not (a & b) for i, a in enumerate(parts) for b in parts[i + 1:])
    return disjoint and union == set(lltune.expand(plan["non_isolated_cpus"])) | set(
        lltune.expand(plan["isolated_cpus"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=str(REPO / "docs" / "CONFIG-REFERENCE.md"))
    args = ap.parse_args()
    text = build()
    Path(args.out).write_text(text)
    print(f"wrote {args.out}  ({len(text.splitlines())} lines, 7 profiles)")


if __name__ == "__main__":
    main()
