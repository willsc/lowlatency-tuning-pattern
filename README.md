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
# Dry run: plan from this machine's real topology and show every file that would
# change, diffed against what is already on disk. Writes nothing, needs no root.
./scripts/apply.sh

# Same plan, actually installed. Asks before it writes.
sudo ./scripts/apply.sh --apply
sudo reboot

# After reboot, prove it took
./bin/lltune validate
sudo ./scripts/jitter-test.sh
```

`apply.sh` is the front door for everything: it plans, shows you the diff for all four
layers, tells you whether a reboot is actually needed, and only then applies. The plan it
shows is the plan it installs — `--apply` hands that exact `plan.json` to `install.sh`
rather than recomputing one.

```sh
./scripts/apply.sh --profile c8i-96xl   # dry-run a shape you are not logged into
./bin/lltune show --profile c7a-48xl    # just the core map
```

## Layout

```
bin/lltune                  plan / render / show / validate  (the whole brain)
profiles/*.json             per-instance-shape topology descriptors
profiles/policy.json        the tuning policy: ratios, C-states, mitigations
scripts/apply.sh            THE FRONT DOOR: dry-run the whole config, then --apply it
scripts/install.sh          render + install boot, cgroup and runtime layers
scripts/apply-runtime.sh    IRQ, ENA queues, XPS, workqueues, power  (runs every boot)
scripts/uninstall.sh        full rollback
scripts/jitter-test.sh      cyclictest / hwlatdetect acceptance gate
scripts/selftest.py         planner invariants — run after any policy/profile change
scripts/docgen_common.py    shared material for the two doc generators
scripts/build-layers-diagram.py    redraw docs/layers.html
scripts/build-config-reference.py  regenerate docs/CONFIG-REFERENCE.md
systemd/                    lltune-runtime.service, lltune-validate.service, app example
sysctl/99-lowlatency.conf   runtime-only kernel knobs
tuned/lowlatency-pulsar/    optional tuned delivery of the runtime layer (AL2023/RHEL)
docs/DESIGN.md              why each knob is set, and what it costs
docs/RUNBOOK.md             apply, verify, roll back, debug a regression
docs/layers.html            generated: a drawing of all four layers, per shape
docs/CONFIG-REFERENCE.md    generated: GRUB isolation + cgroup slices, every shape, verbatim
```

## Generated documentation

Two documents describe the installed configuration for all seven shapes. Both are
**generated from the planner**, never hand-written, so neither can drift from what
`install.sh` would actually apply — a document with stale CPU lists in it is the same
silent failure as a stale config file.

| Document | What it is for |
|---|---|
| [`docs/CONFIG-REFERENCE.md`](docs/CONFIG-REFERENCE.md) | The **boot isolation (GRUB) and cgroup slice** layers, every shape: which arguments are constant and which carry a CPU list, `AllowedCPUs` for every unit, and the verbatim contents of all seven `99-lowlatency.cfg` files and forty-two systemd units. Greppable and diffable in review. |
| [`docs/layers.html`](docs/layers.html) | A **drawing**: the machine's cores along one axis, and every layer's coverage drawn beneath it in the same coordinate space. The isolated set is a shaded column running the height of the figure, so the fact that `isolcpus`, `pulsar.slice` and `EXCLUSIVE_CORES` describe one set of cores is visible rather than asserted. |

```sh
./bin/lltune layers --profile c8i-96xl     # the same material, in the terminal
./bin/lltune render --live -o out/         # the real files, for the host you are on

./scripts/build-config-reference.py        # regenerate after any policy or profile change
./scripts/build-layers-diagram.py
```

`build-config-reference.py` re-checks the cross-layer invariants as it writes — that
`pulsar.slice`'s `AllowedCPUs` is exactly the `isolcpus` list, that `irqaffinity` and
`isolcpus` are disjoint, that the three cpusets partition every core — and refuses to emit a
document describing a broken plan.

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
