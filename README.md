# lowlatency-tuning-pattern

Deterministic core partitioning and low-latency tuning for the **AWS compute-optimised
bare-metal** families — **`c7i` / `c8i`** (Intel Sapphire Rapids / Xeon 6) and
**`c7a` / `c8a`** (AMD EPYC Genoa / Turin) — at `24xl`, `48xl` and `96xl`.

Every core on the box is assigned exactly one job:

| Role | cgroup | Isolated? | What runs there |
|---|---|---|---|
| **housekeeping** | `system.slice`, `user.slice`, `machine.slice`, `init.scope` | no | systemd, sshd, agents, per-CPU kernel work. Always includes cpu0. |
| **irqnet** | `irqnet.slice` | no | NIC/NVMe interrupts, softirq, irqbalance |
| **shared** | *none* | no | the app's `shared_cores`: GC, JIT, admin endpoints, compaction |
| **exclusive** | `pulsar.slice` | **yes** | the app's `exclusive_cores`: the latency-critical path |

A cpuset is worth having where it gates something the kernel guarantees, and that is true of
`isolcpus`'d CPUs and nothing else. So `pulsar.slice` holds **only the isolated cores**, and
the shared pool is named by **no slice at all** — it is ordinary load-balanced CPUs that the
application pins its own threads onto. `system.slice`, `user.slice`, `machine.slice` and
`init.scope` are all confined to housekeeping, so nothing that lands there by default can
drift onto the app's pool.

The three managed cpusets are disjoint and cover every core except the shared pool, and no
unit's `AllowedCPUs` names a shared core — `scripts/selftest.py` asserts both.

The contract the application sees is exactly two lists, plus the order to spend them in:

```sh
EXCLUSIVE_CORES=24-95,120-191     # == the isolcpus set
SHARED_CORES=9-23,105-119         # non-isolated, load-balanced app pool
NUMA_NODE_ORDER=0,1               # nearest the primary NIC first
NIC_LOCAL_EXCLUSIVE_CORES=24-95   # spend these before any other exclusive core
```

## Not every exclusive core is worth the same

The primary ENA adapter hangs off one PCIe root complex, which belongs to one NUMA node.
An exclusive core on another node pays a die crossing or a socket hop on every packet
descriptor and DMA buffer touch, and no amount of isolation buys that back. So the planner
ranks every node by its distance from the NIC and hands the application the exclusive cores
in that order — tier 0 first. On a `c8i.metal-96xl` that ranking is the difference between
138 exclusive cores and the **23** that are actually NIC-local.

```sh
./bin/lltune nic --live      # the adapter, its NUMA node, the SLIT, and the ranking
```

AWS publishes no PCIe root-complex map, so on every multi-node shape the NIC's node is an
inference until a real host has been read. Run `lltune nic --live` on first contact and
correct the profile; `lltune validate` fails if the installed plan assumed the wrong node,
because a wrong NIC node turns the whole ranking upside down.

## The layers

```
+============================================================================+
| LAYER 4  APPLICATION CORE CONTRACT             no reboot; read at start-up |
|   exclusive_cores, shared_cores, and the NUMA node order to spend them in  |
+----------------------------------------------------------------------------+
| LAYER 3  RUNTIME PINNING                lltune-runtime.service, every boot |
|   IRQ affinity, ENA queue count, XPS, unbound workqueues, governor, THP    |
+----------------------------------------------------------------------------+
| LAYER 2  CGROUP v2 SLICES                         daemon-reload; immediate |
|   pulsar.slice = exclusive   irqnet.slice = irqnet   system.slice = hk     |
|   shared_cores are in NO cpuset - the app pins itself onto them            |
+----------------------------------------------------------------------------+
|    ^   pulsar.slice AllowedCPUs MUST equal isolcpus. Nothing checks        |
|    |   it at run time, so both are rendered from one plan field.           |
+----------------------------------------------------------------------------+
| LAYER 1  BOOT ISOLATION                          grub.d; REQUIRES A REBOOT |
|   isolcpus = nohz_full = rcu_nocbs, and irqaffinity for everything else    |
+----------------------------------------------------------------------------+
| LAYER 0  MACHINE                    the cores the metal shape actually has |
+============================================================================+
```

Four layers, one set of cores. The constraint between layers 2 and 1 is the only hard one:
nothing checks it at run time, so a `pulsar.slice` that disagreed with `isolcpus` would
keep serving traffic while its tail latency doubled. That is why all four are rendered from
one `plan.json`.

**[`docs/ARCHITECTURES.md`](docs/ARCHITECTURES.md) has this diagram for each of the seven
metal shapes**, carrying that shape's real core allocation and the actual values every
layer installs — kernel arguments, cgroup cpusets, runtime pinning targets and the
application's core contract, plus the per-NUMA-node breakdown ordered by distance from the
NIC. The diagrams are plain text, and there is a Confluence-markup copy of the whole
document in [`docs/confluence/`](docs/confluence/).

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
scripts/docgen_common.py    shared material for the doc generators
scripts/docwriter.py        the two output dialects: Markdown and Confluence markup
scripts/confluence.py       Confluence wiki-markup emitters
scripts/textdiag.py         the plain-text layer stack and core-map diagrams
scripts/build-architectures.py     regenerate docs/ARCHITECTURES.*
scripts/build-config-reference.py  regenerate docs/CONFIG-REFERENCE.*
systemd/                    lltune-runtime.service, lltune-validate.service, app example
sysctl/99-lowlatency.conf   runtime-only kernel knobs
tuned/lowlatency-pulsar/    optional tuned delivery of the runtime layer (AL2023/RHEL)
docs/ARCHITECTURES.md       generated: the layer diagram per shape, configs annotated
docs/CONFIG-REFERENCE.md    generated: GRUB isolation + cgroup slices, every shape, verbatim
docs/confluence/*.confluence       the same two documents in Confluence wiki markup
docs/DESIGN.md              why each knob is set, and what it costs
docs/RUNBOOK.md             apply, verify, roll back, debug a regression
```

## Generated documentation

Both documents below are **generated from the planner**, never hand-written, so they cannot
drift from what `install.sh` would actually apply — a document with stale CPU lists in it is
the same silent failure as a stale config file. Each is emitted twice from one generator:
GitHub Markdown for review in the repo, and Confluence wiki markup for pasting into a page.

| Document | What it is for |
|---|---|
| [`docs/ARCHITECTURES.md`](docs/ARCHITECTURES.md) | The **four layers per shape**, as plain-text diagrams: the layer stack with that shape's real values, the proportional core map with NUMA nodes ordered by distance from the NIC, the role and per-node tables, and the full kernel command line. |
| [`docs/CONFIG-REFERENCE.md`](docs/CONFIG-REFERENCE.md) | The **boot isolation (GRUB) and cgroup slice** layers, every shape: which arguments are constant and which carry a CPU list, `AllowedCPUs` for every unit, and the verbatim contents of all seven `99-lowlatency.cfg` files and forty-two systemd units. Greppable and diffable in review. |

The Confluence copies are `docs/confluence/ARCHITECTURES.confluence` and
`docs/confluence/CONFIG-REFERENCE.confluence`. Paste one into a page with **Insert → Markup
→ Confluence wiki**. The diagrams are text inside `{noformat}` macros and are drawn to 78
columns, which fits a default Confluence page without a horizontal scrollbar.

```sh
./bin/lltune layers --profile c8i-96xl     # the same material, in the terminal
./bin/lltune render --live -o out/         # the real files, for the host you are on

./scripts/build-architectures.py           # regenerate after any policy or profile change
./scripts/build-config-reference.py
```

`build-config-reference.py` re-checks the cross-layer invariants as it writes — that
`pulsar.slice`'s `AllowedCPUs` is exactly the `isolcpus` list, that `irqaffinity` and
`isolcpus` are disjoint, that the managed cpusets partition every core but the shared pool,
that no unit's `AllowedCPUs` names a shared core — and refuses to emit a document describing
a broken plan.

## Supported shapes

| Profile | Metal SKU | CPU | Sockets × cores | SMT | NUMA mode | **NUMA nodes** | L3 domain | NIC node | NIC-local exclusive |
|---|---|---|---|---|---|---|---|---|---|
| `c7i-24xl` | `c7i.metal-24xl` | Sapphire Rapids | 1 × 48 | 2 → off | SNC off | 1 | per socket | 0 (certain) | 37 of 37 |
| `c7i-48xl` | `c7i.metal-48xl` | Sapphire Rapids | 2 × 48 | 2 → off | SNC off | 2 | per socket | 0 (inferred) | 37 of 74 |
| `c8i-48xl` | `c8i.metal-48xl` | Xeon 6 (Granite Rapids) | 1 × 96 | 2 → off | **SNC3** | **3** | 32 cores/die | 0 (inferred) | 23 of 69 |
| `c8i-96xl` | `c8i.metal-96xl` | Xeon 6975P-C | 2 × 96 | 2 → off | **SNC3** | **6** | 32 cores/die | 0 (inferred) | **23 of 138** |
| `c7a-48xl` | `c7a.metal-48xl` | EPYC 9R14 (Genoa) | 2 × 96 | already off | NPS1 | 2 | 8-core CCX | 0 (inferred) | 72 of 144 |
| `c8a-24xl` | `c8a.metal-24xl` | EPYC Turin | 1 × 96 | already off | NPS1 | 1 | 8-core CCX | 0 (certain) | 72 of 72 |
| `c8a-48xl` | `c8a.metal-48xl` | EPYC Turin | 2 × 96 | already off | NPS1 | 2 | 8-core CCX | 0 (inferred) | 72 of 144 |

These are the only compute-optimised metal SKUs that exist: there is no `c8i.metal-24xl`,
no `c8a.metal-96xl`, and c7a's only bare-metal size is the 48xl.

**Granite Rapids is not one NUMA node per socket.** Xeon 6900P is three compute dies per
socket, and SNC3 — Intel's default, and the mode AWS runs — exposes each die as its own NUMA
domain. A `c8i.metal-96xl` is **six** NUMA nodes of 32 cores, not two of 96. Since
housekeeping and IRQ cores are reserved per node, assuming two would reserve 8 cores where
the correct answer is 24, and would leave four of the six domains with no local housekeeping
core at all. `docs/DESIGN.md` §2 draws the full topology and the NIC-locality consequence.

**The NIC node is certain only where there is one node to be on.** On the two single-node
shapes the primary ENA is local to the only NUMA domain there is. On every other shape it
is an inference: AWS publishes no PCIe root-complex map, and which socket — or, under SNC3,
which die — the adapter hangs off is a platform wiring choice. Under SNC3 the PCIe root
ports sit on the I/O dies flanking the three compute dies, so the NIC lands on an *edge*
domain rather than the middle one, but that narrows it to two candidates and no further.

Each profile carries a `confidence` block recording what is verified against AWS/Intel
documentation and what is inferred: the NIC node, the AMD NPS setting and Turin's CCX size
all are — NPS is a BIOS setting invisible from outside. On first contact with a shape run
`lltune topology --live` and `lltune nic --live`, and correct the profile. `lltune validate`
fails outright if the host's NUMA node count or its NIC node disagrees with the installed
plan.

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
