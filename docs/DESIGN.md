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

## 2. Boot layer — GRUB

| Argument | Why |
|---|---|
| `nosmt=force` | A sibling thread competes for the same execution ports and L1/L2. Sharing a core is the largest single source of tail latency on a hyperthreaded box; it is not a scheduling problem you can tune around. Omitted automatically when the vendor already ships SMT off. |
| `isolcpus=managed_irq,domain,<excl>` | `domain` removes the cores from every scheduler domain, so the load balancer never migrates a task onto them. `managed_irq` steers *kernel-managed* MSI-X vectors (ENA, NVMe) away from them — see §4, this flag is the only lever you have for those. |
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

## 3. cgroup layer — why slices and not just `taskset`

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
├── system.slice        AllowedCPUs = housekeeping
├── user.slice          AllowedCPUs = housekeeping
├── init.scope          AllowedCPUs = housekeeping
├── irqnet.slice        AllowedCPUs = irqnet
└── pulsar.slice        AllowedCPUs = exclusive + shared
```

### Why one application slice and not two

An earlier revision split the application into `pulsar-exclusive.slice` and
`pulsar-shared.slice`. That was structure for its own sake. A cpuset is a *restriction*, and
a restriction is only worth having where it enforces a guarantee the kernel is not already
making. On the exclusive cores the guarantee is real and comes from `isolcpus`: nothing runs
there unless something explicitly grants access. On the shared cores there is no guarantee to
enforce — they are ordinary load-balanced CPUs, and a cgroup around them would confine the
application to a subset of cores it is already entitled to, at the cost of a second cpuset
that has to be regenerated and kept in step with every plan change.

So there is one `pulsar.slice` with `AllowedCPUs = exclusive + shared`. The split between the
two pools is enforced where it actually belongs — in the application, which reads
`EXCLUSIVE_CORES` and `SHARED_CORES` from `cores.env` and pins its own threads. That is a
decision only the app can make correctly anyway: no cgroup can tell a broker's IO thread from
its GC thread.

The `pulsar.slice` unit carries both lists as comments, so the file is still self-describing
when someone reads it in `/etc/systemd/system` with no plan to hand.

**Escape hatch:** `user.slice` is confined to housekeeping, so an SSH session cannot profile
an isolated core directly. Use `systemd-run --slice=pulsar.slice -t perf ...`.

### What is deliberately not used

`cpuset.cpus.partition=isolated` can do sched-domain isolation dynamically, without a
reboot. It is not the default here because it can be dismantled at runtime by anything that
can write the cgroup tree, and because a boot-time guarantee is auditable in `/proc/cmdline`.
Use it if you need to re-partition without rebooting, and accept the weaker guarantee.

## 4. Interrupts — the part that actually goes wrong

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

## 5. Sizing

Defaults per NUMA node: housekeeping `max(2, 4%)` capped at 6, irqnet `max(2, 5%)` capped at
12, shared 15%, rest exclusive. On `c7a-48xl` that is 8 housekeeping / 10 irqnet / 30 shared
/ 144 exclusive.

The floors matter more than the ratios on small nodes, and the caps matter more on large
ones — housekeeping work does not grow linearly with core count, so a fixed 4% would waste
a dozen cores on a 384-vCPU box. `min_exclusive_ratio` is the backstop: if a policy change
would leave less than 55% of the machine exclusive, the planner refuses rather than emitting
a plan nobody reviewed.

### The dual-socket caveat

On the `48xl` and `96xl` dual-socket shapes the NIC sits on one socket. Cores on the far
socket are an interconnect hop away, and no amount of core isolation fixes a traversal.
Treat the NIC-local socket as the low-latency real estate and the far socket as capacity —
pin the latency-critical brokers to `EXCLUSIVE_CORES_NODE<nic_numa_node>` and put bulk work
everywhere else. `c8a-96xl` is the sharpest case: 288 exclusive cores, but only half of them
are one hop from the NIC.

## 6. What this cannot fix

Isolation removes *your* jitter. It does not remove the hypervisor's, the firmware's, or the
network's. On EC2 metal there is no hypervisor underneath you, but SMIs still exist — that
is what `hwlatdetect` in `jitter-test.sh` is measuring, and if it reports outliers, no kernel
tuning will help. The acceptance gate is p99.99 < 20 µs and max < 50 µs on exclusive cores;
if you cannot hit that, look at firmware and the instance itself before touching this repo.
