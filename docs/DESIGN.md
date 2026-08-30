# Design notes

Why each layer exists, why each knob is set, and what it costs. Read this before changing
anything in `profiles/policy.json`.

## 1. The core plan

### Allocation order per NUMA node

Cores are sorted by `(l3_domain, core_id)` and cut in this order:

```
[ housekeeping ][ irqnet ][ shared ]        [ exclusive ......................... ]
 \_______________ non-isolated ________/     \____ isolcpus / nohz_full / rcu_nocbs ___/
```

Low-numbered cores go to the platform for two reasons. cpu0 cannot be isolated — it takes
boot-time work, some legacy timer duties, and on many kernels the fallback for anything
with no valid affinity — so the housekeeping block has to start there anyway. And keeping
the exclusive set contiguous and high means `isolcpus=` is a single clean range per node,
which is much easier to eyeball in `/proc/cmdline` at 3am.

### L3 alignment

`l3_align` rounds the non-isolated block up to a whole L3 domain. On AMD this is the single
highest-value decision in the file: Genoa's CCX is 8 cores sharing 32 MiB of L3, and one
chatty housekeeping core inside a CCX evicts cache lines for the seven exclusive cores next
to it. The cost is a few extra cores in the shared pool. Take the trade.

On Intel SPR the L3 is monolithic per socket, so `l3_align` is a no-op and the flag costs
nothing to leave on.

### Why cores, not vCPUs

Everything is decided at *physical core* granularity, then expanded to logical CPUs. With
SMT off that is a 1:1 map, but doing it in this order means the same planner produces a
correct plan if you ever re-enable SMT, and it means the AMD 7a-family (which ships with
SMT already disabled) needs no special-casing.

## 2. NUMA topology per family — get this right first

Every number in the plan derives from the NUMA node count. Get it wrong and the plan still
*looks* reasonable — the core counts add up, the ranges are contiguous — but housekeeping and
IRQ cores land in the wrong memory domains, and the per-node minimums reserve the wrong
number of cores. This is the single easiest thing to be quietly wrong about, so it is
tabulated rather than assumed.

| Metal SKU | CPU | Sockets × cores | SMT | NUMA mode | **NUMA nodes** | Cores/node | L3 domain |
|---|---|---|---|---|---|---|---|
| `c7i.metal-24xl` | Sapphire Rapids | 1 × 48 | 2 → off | SNC off | **1** | 48 | per socket |
| `c7i.metal-48xl` | Sapphire Rapids | 2 × 48 | 2 → off | SNC off | **2** | 48 | per socket |
| `c8i.metal-48xl` | Xeon 6 (Granite Rapids) | 1 × 96 | 2 → off | **SNC3** | **3** | 32 | 32 cores / die |
| `c8i.metal-96xl` | Xeon 6975P-C | 2 × 96 | 2 → off | **SNC3** | **6** | 32 | 32 cores / die |
| `c7a.metal-48xl` | EPYC 9R14 (Genoa) | 2 × 96 | off | NPS1 | **2** | 96 | 8-core CCX |
| `c8a.metal-24xl` | EPYC Turin | 1 × 96 | off | NPS1 | **1** | 96 | 8-core CCX |
| `c8a.metal-48xl` | EPYC Turin | 2 × 96 | off | NPS1 | **2** | 96 | 8-core CCX |

These are the only compute-optimised metal SKUs that exist. There is **no** `c8i.metal-24xl`,
**no** `c8a.metal-96xl`, and **no** `c7a.metal-24xl` — c7a's only bare-metal size is the 48xl.

### Granite Rapids is not one node per socket

Xeon 6900P is built from **three compute dies per socket**, and SNC3 — Intel's default, and
the mode AWS runs — exposes each die as its own NUMA domain with its own L3 slice and its own
four memory channels. A `c8i.metal-96xl` is therefore **six** NUMA nodes, not two.

```
c8i.metal-96xl  —  2 sockets x 3 compute dies  =  6 NUMA domains

  ┌───────────────── socket 0 ─────────────────┐  ┌───────────────── socket 1 ─────────────────┐
  │  node 0     │  node 1     │  node 2        │  │  node 3     │  node 4     │  node 5        │
  │  die 0      │  die 1      │  die 2         │  │  die 0      │  die 1      │  die 2         │
  │  32 cores   │  32 cores   │  32 cores      │  │  32 cores   │  32 cores   │  32 cores      │
  │  160MiB L3  │  160MiB L3  │  160MiB L3     │  │  160MiB L3  │  160MiB L3  │  160MiB L3     │
  │  4 mem ch   │  4 mem ch   │  4 mem ch      │  │  4 mem ch   │  4 mem ch   │  4 mem ch      │
  │  cpu 0-31   │  cpu 32-63  │  cpu 64-95     │  │  cpu 96-127 │  cpu128-159 │  cpu160-191    │
  └─────────────┴─────────────┴────────────────┘  └─────────────┴─────────────┴────────────────┘
         ▲                                  UPI
         └── NIC attaches here (one die, one socket)
```

The plan reserves housekeeping and IRQ cores **per node**, so the node count directly sets the
platform's overhead:

```
assuming 2 NUMA nodes (WRONG):   4 housekeeping +  4 irqnet  =   8 cores reserved
actual SNC3, 6 NUMA nodes:      12 housekeeping + 12 irqnet  =  24 cores reserved
```

Three times the reservation. Worse, the wrong plan puts *no* housekeeping core in four of the
six domains, so kernel per-node work on those dies has to run cross-domain or lands on an
isolated core — which is precisely the jitter the design exists to remove.

### The consequence nobody likes: NIC locality under SNC3

The ENA adapter attaches to **one die on one socket**. Under SNC3 that means only ~32 of the
192 cores on a `c8i.metal-96xl` are in the NIC's own NUMA domain. Cores on the other five
domains pay a die crossing (nodes 1–2) or a UPI traversal (nodes 3–5) on every packet
descriptor and DMA buffer touch.

No amount of core isolation fixes an interconnect hop. So on `c8i`:

- Put the latency-critical brokers on `EXCLUSIVE_CORES_NODE<nic_numa_node>` — that is the
  real low-latency budget, and it is **23 cores, not 138**.
- Treat the other five domains as capacity for work that is throughput-bound rather than
  tail-latency-bound.
- If you need more than one die's worth of latency-critical cores, shard the application by
  NUMA domain and pin each shard to its own die, rather than letting one broker straddle dies.

`lltune validate` fails if the running host's node count disagrees with the plan, and checks
that every CPU the plan assigned to a node is actually resident on that node.

### Why the AMD side is simpler

Genoa and Turin run **NPS1** on EC2 — one NUMA node per socket — so a `c7a.metal-48xl` is two
domains of 96 cores. The cache story is the opposite of Intel's, though: the L3 is per 8-core
CCX, so a 96-core socket has 12 separate last-level caches inside one NUMA domain. That is
what `l3_align` is for (§1), and it is why the AMD plans place the non-isolated block on whole
CCX boundaries while the Intel plans mostly do not need to.

**Verification status.** The SKU list, vCPU counts, CPU models and the Granite Rapids SNC3
mode are from AWS and Intel documentation. The AMD NPS1 setting and Turin's 8-core CCX are
*inferred* — NPS is a BIOS setting you cannot see from outside, and a 96-core Turin could in
principle be Zen 5c with a 16-core CCX (the 4.5 GHz ceiling argues against it). Each profile
records what is verified and what is assumed in its `confidence` block. On first contact with
any of these shapes, run `lltune topology --live` and correct the profile.

## 3. Boot layer — GRUB

| Argument | Why |
|---|---|
| `nosmt=force` | A sibling thread competes for the same execution ports and L1/L2. Sharing a core is the largest single source of tail latency on a hyperthreaded box; it is not a scheduling problem you can tune around. Omitted automatically when the vendor already ships SMT off. |
| `isolcpus=managed_irq,domain,<excl>` | `domain` removes the cores from every scheduler domain, so the load balancer never migrates a task onto them. `managed_irq` steers *kernel-managed* MSI-X vectors (ENA, NVMe) away from them — see §5, this flag is the only lever you have for those. |
| `nohz_full=<excl>` | Stops the 1 kHz scheduler tick on cores running a single runnable task. Removes ~1 interrupt per millisecond per core. |
| `rcu_nocbs=<excl>` | Moves RCU callback processing off the isolated cores onto `rcuo` kthreads on housekeeping. Without this, an isolated core still gets periodic multi-microsecond RCU work. |
| `irqaffinity=<hk+irq>` | The initial default affinity for every non-managed interrupt, applied at boot before userspace exists. `apply-runtime.sh` re-asserts it, but this closes the window during boot. |
| `numa_balancing=disable` | Automatic NUMA balancing unmaps pages to sample them and takes the fault on the next access. On a pinned, NUMA-aware application it is pure downside: multi-microsecond stalls at unpredictable moments. |
| `skew_tick=1` | De-synchronises the remaining per-CPU ticks so hundreds of cores do not contend on the same locks in the same microsecond. Matters more the wider the box. |
| `processor.max_cstate=1` / `intel_idle.max_cstate=1` | C6 exit latency is tens of microseconds. C1 only. |
| `cpufreq.default_governor=performance` | Sets the governor before any workload starts, rather than racing the runtime script. |
| `tsc=reliable clocksource=tsc` | Forces the TSC and skips the watchdog that can demote the clocksource to HPET mid-flight. A clocksource demotion turns every `clock_gettime` into a ~1 µs syscall. |
| `nmi_watchdog=0 nosoftlockup mce=ignore_ce` | Removes periodic NMI sampling and the corrected-machine-check storm handler. |
| `transparent_hugepage=never` | THP defrag stalls are milliseconds long. Use explicit 1 GiB hugepages for the JVM heap instead (`hugepages_1g_per_node`). |
| `pcie_aspm=off` | ASPM link-state exit costs microseconds on the NIC's first packet after idle. |
| `systemd.unified_cgroup_hierarchy=1 cgroup_no_v1=all` | The slice layer needs cgroup v2's `cpuset` controller. Default on AL2023/Ubuntu 22+; explicit for older images. |

### Deliberately NOT set by default

- **`mitigations=off`** — worth a real 5–30 µs on syscall-heavy paths, and a real reduction
  in your security posture. It is a decision for whoever owns the risk, not a default.
  Enable with `--mitigations-off`.
- **`idle=poll`** — eliminates C-state exit latency entirely by spinning. Costs ~100% power
  on every core, and on a 192-core box that is a thermal and bill problem that can itself
  cause frequency variance. Try `max_cstate=1` first; `--idle-poll` if you still need it.
- **`intel_pstate=disable`** — commonly recommended, frequently a trap on EC2 metal: it drops
  you to `acpi-cpufreq`, and if the platform exposes no ACPI P-states you are stuck at
  whatever frequency the firmware left. Setting the governor to `performance` with HWP
  active gets you the same determinism without the cliff.
- **Turbo disabled** — the all-core turbo bin on EC2 metal is stable enough that giving up
  the MHz costs more than the residual variance. Set `LLTUNE_NO_TURBO=1` if you need a
  frequency-invariant timebase more than you need the clock speed.

## 4. cgroup layer — why slices and not just `taskset`

`isolcpus` removes the exclusive cores from init's affinity mask, and every process inherits
that mask. So by default *nothing* can run on an isolated core, which is what you want — the
isolation is fail-safe rather than fail-open.

Moving a task into a cpuset cgroup rewrites its allowed mask. That makes
`Slice=pulsar.slice` the grant mechanism: a unit gets access to the isolated cores
because of where it lives in the hierarchy, not because someone remembered a `taskset`
prefix in an `ExecStart`. Delete the slice and the access is gone.

The hierarchy:

```
-.slice
├── system.slice        AllowedCPUs = housekeeping + shared
├── user.slice          AllowedCPUs = housekeeping
├── machine.slice       AllowedCPUs = housekeeping
├── init.scope          AllowedCPUs = housekeeping
├── irqnet.slice        AllowedCPUs = irqnet
└── pulsar.slice        AllowedCPUs = exclusive          (isolated only)
```

### The cpuset boundary follows the isolation boundary

A cpuset is a *restriction*, and a restriction earns its place only where it gates a
guarantee the kernel is actually making. `isolcpus` makes exactly one such guarantee: those
CPUs are out of every scheduler domain and out of init's inherited affinity mask, so nothing
reaches them without an explicit grant. `pulsar.slice` is that grant, and it therefore
contains the isolated cores and nothing else.

The shared cores have no such guarantee to gate. They are ordinary load-balanced CPUs that
happen to be reserved by convention for the application's non-latency work. Wrapping them in
their own cgroup would confine the app to cores it is already entitled to, in exchange for
another cpuset that has to be regenerated and kept in step with every plan change — cost
without a guarantee. So they sit in `system.slice`, which is where systemd puts ordinary
services anyway, and are reachable with no further ceremony.

Two consequences worth being explicit about:

- **Housekeeping and app shared work share one cpuset.** They are separated by convention
  (`cores.env`) rather than by the kernel. If a runaway agent on a housekeeping core starts
  competing with GC threads, no cgroup will stop it. Add `CPUWeight=` on the individual
  units if that becomes a real contention problem — that is the right tool, not a cpuset.
- **`user.slice`, `machine.slice` and `init.scope` stay housekeeping-only.** An SSH session
  or a container has no business on the shared pool, and confining them keeps the shared
  cores predictable for the application.

The split between `exclusive_cores` and `shared_cores` is enforced where it belongs — in the
application, which reads both lists from `cores.env` and pins its own threads. No cgroup can
tell a broker's IO thread from its GC thread. The `pulsar.slice` and `system.slice` units
each carry the relevant lists as comments, so both files stay self-describing when read in
`/etc/systemd/system` with no plan to hand.

**Escape hatch:** `user.slice` is confined to housekeeping, so an SSH session cannot profile
an isolated core directly. Use `systemd-run --slice=pulsar.slice -t perf ...`.

**Where a service lands by default:** anything with no `Slice=` goes to `system.slice`, so it
gets housekeeping + shared and can never touch an isolated core by accident. Reaching the
latency path requires naming `Slice=pulsar.slice` — which is the fail-safe direction.

### What is deliberately not used

`cpuset.cpus.partition=isolated` can do sched-domain isolation dynamically, without a
reboot. It is not the default here because it can be dismantled at runtime by anything that
can write the cgroup tree, and because a boot-time guarantee is auditable in `/proc/cmdline`.
Use it if you need to re-partition without rebooting, and accept the weaker guarantee.

## 5. Interrupts — the part that actually goes wrong

Three different mechanisms, and only one of them responds to `/proc/irq/N/smp_affinity`:

1. **Legacy / non-managed IRQs** — writable. `apply-runtime.sh` pins them to the irqnet set.
2. **Kernel-managed MSI-X (ENA, NVMe)** — the kernel owns the affinity spread and *rejects
   writes*. `isolcpus=managed_irq` tells it to prefer housekeeping CPUs.
3. **Per-CPU interrupts** (LOC, RES, CAL) — cannot be moved by definition. `nohz_full` and
   `rcu_nocbs` are what reduce these.

The trap is category 2. `managed_irq` only steers the spread *if there is somewhere for it
to go*: ENA allocates one MSI-X vector per queue, so if the interface has 32 combined queues
and you reserved 10 IRQ cores, the spread reaches onto isolated cores regardless of the
cmdline flag. `apply-runtime.sh` therefore clamps `ethtool -L combined` to the irqnet core
count. Raising `shared_ratio` without re-running the runtime script is how this silently
regresses — which is why `validate` walks every `effective_affinity_list` and fails on any
overlap with the isolated set.

Other NIC settings: adaptive coalescing off (`rx-usecs 0`) trades throughput for latency,
which is the trade we are here to make; `napi_defer_hard_irqs=2` + `gro_flush_timeout=20000`
lets the NAPI poll drain the ring instead of taking a hard IRQ per burst; RPS is off because
it costs an IPI per packet; XPS maps each exclusive core to a fixed TX queue so a transmit
never contends on a queue lock owned by another socket.

## 6. Sizing

Defaults per NUMA node: housekeeping `max(2, 4%)` capped at 6, irqnet `max(2, 5%)` capped at
12, shared 15%, rest exclusive. On `c7a-48xl` that is 8 housekeeping / 10 irqnet / 30 shared
/ 144 exclusive.

The floors matter more than the ratios on small nodes, and the caps matter more on large
ones — housekeeping work does not grow linearly with core count, so a fixed 4% would waste
a dozen cores on a 384-vCPU box. `min_exclusive_ratio` is the backstop: if a policy change
would leave less than 55% of the machine exclusive, the planner refuses rather than emitting
a plan nobody reviewed.

### Node count drives the overhead

Because housekeeping and IRQ cores are reserved **per NUMA node**, the platform's fixed cost
scales with the node count, not the core count:

| Shape | NUMA nodes | Cores/node | housekeeping | irqnet | shared | exclusive |
|---|---|---|---|---|---|---|
| `c7i-24xl` | 1 | 48 | 2 | 2 | 7 | 37 |
| `c7i-48xl` | 2 | 48 | 4 | 4 | 14 | 74 |
| `c8i-48xl` | 3 | 32 | 6 | 6 | 15 | 69 |
| `c8i-96xl` | 6 | 32 | 12 | 12 | 30 | 138 |
| `c7a-48xl` | 2 | 96 | 8 | 10 | 30 | 144 |
| `c8a-24xl` | 1 | 96 | 4 | 5 | 15 | 72 |
| `c8a-48xl` | 2 | 96 | 8 | 10 | 30 | 144 |

`c8i-96xl` is the shape where this bites: 24 cores go to the platform purely because SNC3
splits the machine into six domains, each of which needs its own housekeeping and IRQ cores.
That is the correct answer, not waste — a domain with no local housekeeping core forces its
per-node kernel work cross-domain — but it is worth seeing before someone asks why the
384 vCPU box yields 138 exclusive cores while the 192 vCPU `c7a` yields 144.

See §2 for the NIC-locality consequence, which is the sharper constraint on `c8i`.

## 7. What this cannot fix

Isolation removes *your* jitter. It does not remove the hypervisor's, the firmware's, or the
network's. On EC2 metal there is no hypervisor underneath you, but SMIs still exist — that
is what `hwlatdetect` in `jitter-test.sh` is measuring, and if it reports outliers, no kernel
tuning will help. The acceptance gate is p99.99 < 20 µs and max < 50 µs on exclusive cores;
if you cannot hit that, look at firmware and the instance itself before touching this repo.
