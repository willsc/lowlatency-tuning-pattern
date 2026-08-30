#!/usr/bin/env python3
"""Build the layer reference page from live planner output.

The page is generated, never hand-written: a document listing GRUB args and cpusets
per instance type would drift from the planner within one policy change, and drift is
the failure mode this whole repo exists to prevent.

    ./scripts/build-layers-page.py [-o out.html]
"""
import argparse, datetime, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docgen_common import ARG_GROUPS, CPU_NAME, ORDER, REPO, load_lltune

lltune = load_lltune()


def build():
    policy = lltune.load_policy()
    profiles = []
    for name in ORDER:
        prof = lltune.load_profile(name)
        topo = lltune.topology_from_profile(prof)
        plan = lltune.build_plan(topo, policy, name)
        slices = lltune.render_slices(plan)
        t = plan["topology"]
        profiles.append({
            "id": name,
            "sku": prof.get("metal_sku", name),
            "cpu": CPU_NAME.get(name, ""),
            "vendor": t["vendor"],
            "sockets": t["sockets"],
            "coresPerSocket": t["total_cores"] // t["sockets"],
            "threadsPerCore": t["threads_per_core"],
            "vcpus": prof["vcpus"],
            "numaNodes": t["numa_nodes"],
            "coresPerL3": t["cores_per_l3"],
            "numaMode": prof.get("numa_mode", ""),
            "totalCores": t["total_cores"],
            "nicNode": t["nic_numa_node"],
            "counts": {r: plan["roles"][r]["cpu_count"] for r in
                       ("housekeeping", "irqnet", "shared", "exclusive")},
            "roles": {r: plan["roles"][r]["cpus"] for r in
                      ("housekeeping", "irqnet", "shared", "exclusive")},
            "nodes": [{"node": n["node"], "total": n["cores_total"],
                       **{r: n[r]["cpus"] for r in
                          ("housekeeping", "irqnet", "shared", "exclusive")}}
                      for n in plan["nodes"]],
            "cmdline": plan["cmdline"].split(),
            "slices": [[k.replace(".d/10-lowlatency.conf", "  (drop-in)"),
                        re.search(r"AllowedCPUs=(\S+)", v).group(1)]
                       for k, v in slices.items()],
            "units": [["/etc/systemd/system/" + k, v.rstrip()] for k, v in slices.items()],
            "grub": lltune.render_grub(plan).rstrip(),
            "runtime": {
                "irq": plan["irq_landing_cpus"],
                "enaQueues": len(lltune.expand(plan["roles"]["irqnet"]["cpus"])),
                "workqueue": plan["non_isolated_cpus"],
                "xps": plan["roles"]["exclusive"]["cpus"],
            },
            "env": [l for l in lltune.render_cores_env(plan).splitlines()
                    if l and not l.startswith("#")],
            "confidence": prof.get("confidence", {}),
        })
    return {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "policy": {k: policy[k] for k in
                   ("shared_ratio", "min_exclusive_ratio", "l3_align", "smt", "cstate_max")},
        "profiles": profiles,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default=str(REPO / "docs" / "layers.html"))
    args = ap.parse_args()
    tpl = (REPO / "scripts" / "layers-page.template.html").read_text()
    data = json.dumps(build(), separators=(",", ":"))
    for ph in ("/*__LAYERS_DATA__*/", "/*__ARG_NOTES__*/"):
        if ph not in tpl:
            sys.exit(f"template is missing the {ph} placeholder")
    Path(args.out).write_text(
        tpl.replace("/*__ARG_NOTES__*/", json.dumps(ARG_GROUPS, separators=(",", ":")))
           .replace("/*__LAYERS_DATA__*/", data))
    print(f"wrote {args.out}  ({len(data)} bytes of plan data, "
          f"{len(json.loads(data)['profiles'])} profiles)")


if __name__ == "__main__":
    main()
