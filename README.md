# lowlatency-tuning-pattern

Deterministic core partitioning and low-latency tuning for the **AWS compute-optimised
bare-metal** families — **`c7i` / `c8i`** (Intel Sapphire Rapids / Xeon 6) and
**`c7a` / `c8a`** (AMD EPYC Genoa / Turin) — at `24xl`, `48xl` and `96xl`.

Every core on the box is assigned exactly one job:

| Role | cgroup | Isolated? | What runs there |
|---|---|---|---|
| **housekeeping** | `system.slice`, `user.slice`, `init.scope` | no | systemd, sshd, agents, per-CPU kernel work. Always includes cpu0. |
| **irqnet** | `irqnet.slice` | no | NIC/NVMe interrupts, softirq, irqbalance |
| **shared** | `pulsar.slice` | no | the app's `shared_cores`: GC, JIT, admin endpoints, compaction |
| **exclusive** | `pulsar.slice` | **yes** | the app's `exclusive_cores`: the latency-critical path |

There is **one** application slice. Its cpuset is `exclusive + shared`; which of the two a
thread lands on is the application's decision, made from the core lists below. The shared
cores are not isolated, so a second cgroup around them would enforce nothing — it would just
be another cpuset to keep in sync with the plan.

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

| Profile | CPU | vCPU | Sockets × cores | SMT | NUMA | L3 domain |
|---|---|---|---|---|---|---|
| `c7i-24xl` | Sapphire Rapids | 96 | 1 × 48 | 2 → off | 1 | per-socket |
| `c7i-48xl` | Sapphire Rapids | 192 | 2 × 48 | 2 → off | 2 | per-socket |
| `c8i-24xl` | Xeon 6 (Granite Rapids) | 96 | 1 × 48 | 2 → off | 1 | per-socket |
| `c8i-48xl` | Xeon 6 (Granite Rapids) | 192 | 1 × 96 | 2 → off | 1 | per-socket |
| `c8i-96xl` | Xeon 6 (Granite Rapids) | 384 | 2 × 96 | 2 → off | 2 | per-socket |
| `c7a-24xl` | EPYC 9R14 (Genoa) | 96 | 1 × 96 | already off | 1 | 8-core CCX |
| `c7a-48xl` | EPYC 9R14 (Genoa) | 192 | 2 × 96 | already off | 2 | 8-core CCX |
| `c8a-24xl` | EPYC 9005 (Turin) | 96 | 1 × 96 | already off | 1 | 8-core CCX |
| `c8a-48xl` | EPYC 9005 (Turin) | 192 | 1 × 192 | already off | 1 | 16-core CCX |
| `c8a-96xl` | EPYC 9005 (Turin) | 384 | 2 × 192 | already off | 2 | 16-core CCX |

The `c8i` and `c8a` socket/CCX layouts are best-known planning defaults, not measured — the
generation is new enough that AWS may present it differently. Confirm on first contact with
the hardware (`lltune topology --live`); if SNC is enabled on Xeon 6 you will see 2–3 NUMA
nodes per socket and the profile needs updating. Nothing downstream depends on the profile
being right when you install with `--live`.

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
