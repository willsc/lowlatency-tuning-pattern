# lowlatency-tuning-pattern

Deterministic core partitioning and low-latency tuning for **AWS bare-metal** instances
(`.metal-24xl`, `.metal-48xl`, and 384-vCPU `96xl`-class), on both **Intel** Sapphire Rapids
and **AMD** EPYC Genoa/Turin.

Every core on the box is assigned exactly one job:

| Role | cgroup | Isolated? | What runs there |
|---|---|---|---|
| **housekeeping** | `system.slice`, `user.slice`, `init.scope` | no | systemd, sshd, agents, per-CPU kernel work. Always includes cpu0. |
| **irqnet** | `irqnet.slice` | no | NIC/NVMe interrupts, softirq, irqbalance |
| **shared** | `pulsar-shared.slice` | no | the app's `shared_cores`: GC, JIT, admin endpoints, compaction |
| **exclusive** | `pulsar-exclusive.slice` | **yes** | the app's `exclusive_cores`: the latency-critical path |

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
./bin/lltune show --profile amd-48xl
./bin/lltune show --profile intel-24xl

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

| Profile | Instance family | vCPU | Sockets × cores | SMT | NUMA | L3 domain |
|---|---|---|---|---|---|---|
| `intel-24xl` | c7i/m7i/r7i `.metal-24xl` | 96 | 1 × 48 | 2 → off | 1 | per-socket |
| `intel-48xl` | c7i/m7i/r7i `.metal-48xl` | 192 | 2 × 48 | 2 → off | 2 | per-socket |
| `intel-96xl` | u7i-class, 4-socket | 384 | 4 × 48 | 2 → off | 4 | per-socket |
| `amd-24xl` | m7a/c7a/r7a 24xl | 96 | 1 × 96 | already off | 1 | 8-core CCX |
| `amd-48xl` | m7a/c7a/r7a `.metal-48xl` | 192 | 2 × 96 | already off | 2 | 8-core CCX |
| `amd-96xl` | EPYC 9005-class, dual socket | 384 | 2 × 192 | already off | 2 | 8-core CCX |
| `amd-milan-48xl` | c6a/m6a/r6a `.metal` | 192 | 2 × 48 | 2 → off | 2 | 8-core CCX |

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
