#!/usr/bin/env python3
"""Invariant tests for the planner. Run before committing a policy or profile change.

These assert the properties the whole design rests on: the roles partition the machine,
nothing overlaps the isolated set, and the rendered artifacts agree with the plan.
"""
import importlib.util, re, sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# bin/lltune has no .py extension, so name the loader explicitly.
_loader = SourceFileLoader("lltune", str(REPO / "bin" / "lltune"))
_spec = importlib.util.spec_from_loader("lltune", _loader)
lltune = importlib.util.module_from_spec(_spec)
_loader.exec_module(lltune)

expand, compress = lltune.expand, lltune.compress
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


PROFILES = sorted(p.stem for p in (REPO / "profiles").glob("*.json") if p.stem != "policy")
RATIOS = [0.05, 0.15, 0.30]

for name in PROFILES:
    prof = lltune.load_profile(name)
    topo = lltune.topology_from_profile(prof)
    for ratio in RATIOS:
        policy = lltune.load_policy()
        policy["shared_ratio"] = ratio
        tag = f"{name}@shared_ratio={ratio}"
        try:
            plan = lltune.build_plan(topo, policy, name)
        except SystemExit as e:
            # Refusing is a valid outcome, but only when the floor is genuinely breached.
            check("min_exclusive_ratio" in str(e), f"{tag}: unexpected refusal: {e}")
            continue

        roles = {r: set(expand(plan["roles"][r]["cpus"])) for r in
                 ("housekeeping", "irqnet", "shared", "exclusive")}
        allcpus = set()
        for r, s in roles.items():
            check(not (allcpus & s), f"{tag}: role {r} overlaps another role")
            allcpus |= s

        expect = topo["total_cores"] if policy["smt"] == "off" else topo["total_cpus"]
        check(len(allcpus) == expect,
              f"{tag}: roles cover {len(allcpus)} cpus, expected {expect}")

        check(0 in roles["housekeeping"], f"{tag}: cpu0 not in housekeeping")

        iso = set(expand(plan["isolated_cpus"]))
        non = set(expand(plan["non_isolated_cpus"]))
        check(iso == roles["exclusive"], f"{tag}: isolated_cpus != exclusive role")
        check(non == roles["housekeeping"] | roles["irqnet"] | roles["shared"],
              f"{tag}: non_isolated_cpus != hk+irq+shared")
        check(not (iso & non), f"{tag}: isolated and non-isolated overlap")
        check(iso | non == allcpus, f"{tag}: isolated+non-isolated != all cpus")

        land = set(expand(plan["irq_landing_cpus"]))
        check(not (land & iso), f"{tag}: irqaffinity lands on isolated cpus")

        # app contract
        check(set(expand(plan["app"]["exclusive_cores"])) == roles["exclusive"],
              f"{tag}: exclusive_cores != isolated cores")
        check(set(expand(plan["app"]["shared_cores"])) == roles["shared"],
              f"{tag}: shared_cores != non-isolated app pool")
        check(plan["app"]["shared_core_count"] > 0, f"{tag}: empty shared pool")

        # every NUMA node must be self-sufficient: a node with no housekeeping or no IRQ
        # core forces cross-node interrupt handling.
        for n in plan["nodes"]:
            for r in ("housekeeping", "irqnet", "shared", "exclusive"):
                check(n[r]["count"] > 0, f"{tag}: node{n['node']} has no {r} core")

        ratio_actual = len(roles["exclusive"]) / len(allcpus)
        check(ratio_actual >= policy["min_exclusive_ratio"],
              f"{tag}: exclusive ratio {ratio_actual:.3f} < floor {policy['min_exclusive_ratio']}")

        # cmdline agrees with the plan
        cl = plan["cmdline"]
        m = re.search(r"isolcpus=managed_irq,domain,(\S+)", cl)
        check(m and set(expand(m.group(1))) == iso, f"{tag}: isolcpus arg != isolated set")
        for key in ("nohz_full", "rcu_nocbs"):
            m = re.search(rf"{key}=(\S+)", cl)
            check(m and set(expand(m.group(1))) == iso, f"{tag}: {key} arg != isolated set")
        m = re.search(r"irqaffinity=(\S+)", cl)
        check(m and set(expand(m.group(1))) == land, f"{tag}: irqaffinity arg != landing set")
        check(("nosmt=force" in cl) == (topo["threads_per_core"] > 1),
              f"{tag}: nosmt present/absent does not match threads_per_core")

        # rendered slices agree with the plan
        sl = lltune.render_slices(plan)
        def allowed(body):
            return set(expand(re.search(r"AllowedCPUs=(\S+)", body).group(1)))
        check(allowed(sl["pulsar.slice"]) == roles["shared"] | roles["exclusive"],
              f"{tag}: pulsar.slice must cover exclusive + shared")
        check("pulsar-exclusive.slice" not in sl and "pulsar-shared.slice" not in sl,
              f"{tag}: child pulsar slices should no longer be rendered")
        check(allowed(sl["irqnet.slice"]) == roles["irqnet"], f"{tag}: irqnet slice")
        check(allowed(sl["system.slice.d/10-lowlatency.conf"]) == roles["housekeeping"],
              f"{tag}: system.slice drop-in")

        # cores.env round-trips
        env = dict(re.findall(r"^(\w+)=(.*)$", lltune.render_cores_env(plan), re.M))
        check(set(expand(env["EXCLUSIVE_CORES"])) == roles["exclusive"], f"{tag}: env EXCLUSIVE_CORES")
        check(set(expand(env["SHARED_CORES"])) == roles["shared"], f"{tag}: env SHARED_CORES")
        check(int(env["EXCLUSIVE_CORE_COUNT"]) == len(roles["exclusive"]), f"{tag}: env count")

# range compression round-trip
for spec_s in ["0", "0-3", "0-3,8", "1,3,5,7", "0-2,5,7-9,100-200"]:
    check(compress(expand(spec_s)) == spec_s, f"compress/expand round-trip broke on {spec_s}")

n = len(PROFILES) * len(RATIOS)
if fails:
    print(f"FAILED ({len(fails)} problems across {n} plans)")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print(f"ok - {n} plans across {len(PROFILES)} profiles, all invariants hold")
