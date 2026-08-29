# lowlatency-tuning-pattern

Deterministic core partitioning and low-latency tuning for the **AWS compute-optimised
bare-metal** families — **`c7i` / `c8i`** (Intel Sapphire Rapids / Xeon 6) and
**`c7a` / `c8a`** (AMD EPYC Genoa / Turin) — at `24xl`, `48xl` and `96xl`.

Every core on the box is assigned exactly one job:

| Role | cgroup | Isolated? | What runs there |
|---|---|---|---|
| **housekeeping** | `system.slice`, `user.slice`, `init.scope` | no | systemd, sshd, agents, per-CPU kernel work. Always includes cpu0. |
| **irqnet** | `irqnet.slice` | no | NIC/NVMe interrupts, softirq, irqbalance |
| **shared** | `system.slice` | no | the app's `shared_cores`: GC, JIT, admin endpoints, compaction |
| **exclusive** | `pulsar.slice` | **yes** | the app's `exclusive_cores`: the latency-critical path |

`pulsar.slice` holds **only the isolated cores**. A cpuset is worth having where it gates
something the kernel guarantees, and that is true of `isolcpus`'d CPUs and nothing else. The
shared cores are ordinary load-balanced CPUs, so they live in `system.slice` alongside
housekeeping and need no cgroup of their own. `user.slice`, `machine.slice` and `init.scope`
stay housekeeping-only, so logins and containers cannot drift onto the app's shared pool.

Every core is owned by exactly one of the three cpusets — `scripts/selftest.py` asserts it.

The contract the application sees is exactly two lists:

```sh
EXCLUSIVE_CORES=24-95,120-191     # == the isolcpus set
SHARED_CORES=9-23,105-119         # non-isolated, load-balanced app pool
```

## Why it is built this way

One artifact — `plan.json` — is computed once from the machine's real topology, and the
GRUB cmdline, the systemd slices, the IRQ pinning and the application's core lists are all
*rendered from it*. That is the whole point of the design: on a 384-vCPU 4-socket box,
hand-maintained CPU lists in four different files drift, and the failure mode is silent.
A broker keeps serving traffic while its p99.9 quietly doubles.

## Quick start

```sh
# See the plan for a shape you do not have in front of you
./bin/lltune show --profile c7a-48xl
./bin/lltune show --profile c8i-96xl

# Plan from the real machine and install everything
sudo ./scripts/install.sh --live
sudo reboot

# After reboot, prove it took
./bin/lltune validate
sudo ./scripts/jitter-test.sh
```

## Layout

```
bin/lltune                  plan / render / show / validate  (the whole brain)
profiles/*.json             per-instance-shape topology descriptors
profiles/policy.json        the tuning policy: ratios, C-states, mitigations
scripts/install.sh          render + install boot, cgroup and runtime layers
scripts/apply-runtime.sh    IRQ, ENA queues, XPS, workqueues, power  (runs every boot)
scripts/uninstall.sh        full rollback
scripts/jitter-test.sh      cyclictest / hwlatdetect acceptance gate
scripts/selftest.py         planner invariants — run after any policy/profile change
systemd/                    lltune-runtime.service, lltune-validate.service, app example
sysctl/99-lowlatency.conf   runtime-only kernel knobs
tuned/lowlatency-pulsar/    optional tuned delivery of the runtime layer (AL2023/RHEL)
docs/DESIGN.md              why each knob is set, and what it costs
docs/RUNBOOK.md             apply, verify, roll back, debug a regression
```

## Supported shapes

| Profile | Metal SKU | CPU | Sockets × cores | SMT | NUMA mode | **NUMA nodes** | L3 domain |
|---|---|---|---|---|---|---|---|
| `c7i-24xl` | `c7i.metal-24xl` | Sapphire Rapids | 1 × 48 | 2 → off | SNC off | 1 | per socket |
| `c7i-48xl` | `c7i.metal-48xl` | Sapphire Rapids | 2 × 48 | 2 → off | SNC off | 2 | per socket |
| `c8i-48xl` | `c8i.metal-48xl` | Xeon 6 (Granite Rapids) | 1 × 96 | 2 → off | **SNC3** | **3** | 32 cores/die |
| `c8i-96xl` | `c8i.metal-96xl` | Xeon 6975P-C | 2 × 96 | 2 → off | **SNC3** | **6** | 32 cores/die |
| `c7a-48xl` | `c7a.metal-48xl` | EPYC 9R14 (Genoa) | 2 × 96 | already off | NPS1 | 2 | 8-core CCX |
| `c8a-24xl` | `c8a.metal-24xl` | EPYC Turin | 1 × 96 | already off | NPS1 | 1 | 8-core CCX |
| `c8a-48xl` | `c8a.metal-48xl` | EPYC Turin | 2 × 96 | already off | NPS1 | 2 | 8-core CCX |

These are the only compute-optimised metal SKUs that exist: there is no `c8i.metal-24xl`,
no `c8a.metal-96xl`, and c7a's only bare-metal size is the 48xl.

**Granite Rapids is not one NUMA node per socket.** Xeon 6900P is three compute dies per
socket, and SNC3 — Intel's default, and the mode AWS runs — exposes each die as its own NUMA
domain. A `c8i.metal-96xl` is **six** NUMA nodes of 32 cores, not two of 96. Since
housekeeping and IRQ cores are reserved per node, assuming two would reserve 8 cores where
the correct answer is 24, and would leave four of the six domains with no local housekeeping
core at all. `docs/DESIGN.md` §2 draws the full topology and the NIC-locality consequence.

Each profile carries a `confidence` block recording what is verified against AWS/Intel
documentation and what is inferred. The AMD NPS setting and Turin's CCX size are inferred —
NPS is a BIOS setting invisible from outside. On first contact with a shape run
`lltune topology --live` and correct the profile; `lltune validate` fails outright if the
host's NUMA node count disagrees with the installed plan.

Profiles exist so you can plan and review a shape you are not currently logged into (and
bake a plan into an AMI). **On the host, `--live` is authoritative** — it reads sysfs and
never guesses. If a profile and the live topology disagree, the profile is wrong.

## The two knobs you will actually tune

```jsonc
// profiles/policy.json
"shared_ratio": 0.15,          // how much of each NUMA node becomes shared_cores
"min_exclusive_ratio": 0.55,   // refuse to emit a plan below this much exclusive
```

Raise `shared_ratio` when GC and admin work is starving; lower it to buy back exclusive
cores. `lltune plan --shared-ratio 0.20` overrides without editing the file.
