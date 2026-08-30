#!/usr/bin/env bash
# Runtime half of the tuning pattern. Everything here is lost across a reboot,
# so it runs from lltune-runtime.service on every boot, after the network is up.
#
# The boot half (isolcpus / nohz_full / nosmt) is in GRUB and is NOT re-applied here.
set -euo pipefail

ENV_FILE=${ENV_FILE:-/etc/lowlatency/cores.env}
PLAN=${PLAN:-/etc/lowlatency/plan.json}
DRY=${DRY_RUN:-0}

[[ -r $ENV_FILE ]] || { echo "missing $ENV_FILE - run 'lltune render' + install first" >&2; exit 1; }
# shellcheck disable=SC1090
source "$ENV_FILE"

log() { printf '[lltune-runtime] %s\n' "$*"; }
w()   { # w <value> <file...>
  local val=$1; shift
  for f in "$@"; do
    if (( DRY )); then
      [[ -e $f ]] || continue
      if [[ -w $f ]]; then echo "  would write '$val' -> $f"
      else echo "  would write '$val' -> $f  (needs root)"; fi
      continue
    fi
    [[ -w $f ]] || continue
    echo "$val" > "$f" 2>/dev/null || log "WARN could not write $f"
  done
}

expand() { python3 -c 'import sys
for p in sys.argv[1].split(","):
    if not p: continue
    if "-" in p:
        a,b=p.split("-"); print(" ".join(str(i) for i in range(int(a),int(b)+1)), end=" ")
    else: print(p, end=" ")' "$1"; }

mask() { python3 -c 'import sys
m=0
for p in sys.argv[1].split(","):
    if not p: continue
    if "-" in p:
        a,b=p.split("-"); r=range(int(a),int(b)+1)
    else: r=[int(p)]
    for c in r: m |= 1<<c
w=max(1,(m.bit_length()+31)//32)
print(",".join(f"{(m>>(32*i))&0xFFFFFFFF:08x}" for i in range(w-1,-1,-1)))' "$1"; }

IRQ_LANDING="${HOUSEKEEPING_CORES},${IRQNET_CORES}"
IRQ_MASK=$(mask "$IRQ_LANDING")
ISO_LIST=$(expand "$ISOLATED_CPUS")
NONISO_MASK=$(mask "$NON_ISOLATED_CPUS")

# ---------------------------------------------------------------- 1. interrupts
log "IRQ landing zone: ${IRQ_LANDING} (mask ${IRQ_MASK})"
w "$IRQ_MASK" /proc/irq/default_smp_affinity

managed=0 moved=0
for d in /proc/irq/[0-9]*; do
  irq=${d##*/}
  [[ -f $d/smp_affinity_list ]] || continue
  # Managed IRQs (ENA, NVMe MSI-X) are owned by the kernel's affinity spreading and
  # reject writes. isolcpus=managed_irq is what keeps those off the isolated cores;
  # the queue-count clamp below is what makes that actually fit. A dry run counts
  # what it would move by testing writability, without touching a single IRQ.
  if (( DRY )); then
    if [[ -w $d/smp_affinity_list ]]; then moved=$((moved+1)); else managed=$((managed+1)); fi
  elif echo "$IRQ_LANDING" > "$d/smp_affinity_list" 2>/dev/null; then
    moved=$((moved+1))
  else
    managed=$((managed+1))
  fi
done
if (( DRY )); then
  log "IRQs: would pin ${moved}, ${managed} kernel-managed or not writable"
else
  log "IRQs: ${moved} pinned, ${managed} kernel-managed (steered by managed_irq)"
fi

# irqbalance, if present, must never undo this.
if systemctl list-unit-files irqbalance.service >/dev/null 2>&1; then
  IRQB=/etc/systemd/system/irqbalance.service.d/10-lowlatency.conf
  if (( DRY )); then
    echo "  would write $IRQB  (bans ${ISOLATED_CPUS} from irqbalance)"
  else
    install -d /etc/systemd/system/irqbalance.service.d
    cat > "$IRQB" <<EOC
# Managed by lltune
[Service]
Environment=IRQBALANCE_BANNED_CPULIST=${ISOLATED_CPUS}
Environment=IRQBALANCE_ARGS=--policyscript=/usr/local/sbin/lltune-irq-policy
Slice=irqnet.slice
EOC
  fi
fi

# ---------------------------------------------------------------- 2. NIC
IFACE=${IFACE:-$(ip -o -4 route show default 2>/dev/null | awk '{print $5; exit}')}
if [[ -n ${IFACE:-} ]] && [[ -d /sys/class/net/$IFACE ]]; then
  NIC_NODE=$(cat "/sys/class/net/$IFACE/device/numa_node" 2>/dev/null || echo 0)
  (( NIC_NODE < 0 )) && NIC_NODE=0
  n_irq=$(expand "$IRQNET_CORES" | wc -w)
  if ! command -v ethtool >/dev/null 2>&1; then
    log "WARN ethtool not installed; skipping queue count, coalescing and ring sizing"
  fi
  # The trailing '|| true' matters: with 'set -o pipefail' a missing ethtool makes the
  # whole pipeline exit 127, which under 'set -e' killed this script before it reached
  # the workqueue, power and memory sections.
  max_q=$(ethtool -l "$IFACE" 2>/dev/null | awk '/Pre-set/,/Current/{if($1=="Combined:")print $2}' | head -1 || true)
  max_q=${max_q:-$n_irq}
  # Clamp receive queues to the IRQ core count. ENA spreads its managed IRQs one per
  # queue; more queues than IRQ cores means the spread reaches onto isolated cores.
  q=$(( n_irq < max_q ? n_irq : max_q ))
  log "$IFACE (numa $NIC_NODE): combined queues -> $q (hw max $max_q, irqnet cores $n_irq)"
  (( DRY )) || ethtool -L "$IFACE" combined "$q" 2>/dev/null || log "WARN ethtool -L failed"

  # Interrupt moderation: adaptive coalescing trades latency for throughput. Turn it off.
  (( DRY )) || ethtool -C "$IFACE" adaptive-rx off adaptive-tx off rx-usecs 0 tx-usecs 0 2>/dev/null \
    || log "WARN ethtool -C partially unsupported on $IFACE"
  # Deepest available rings: absorb microbursts instead of dropping.
  rx_max=$(ethtool -g "$IFACE" 2>/dev/null | awk '/Pre-set/,/Current/{if($1=="RX:")print $2}' | head -1 || true)
  tx_max=$(ethtool -g "$IFACE" 2>/dev/null | awk '/Pre-set/,/Current/{if($1=="TX:")print $2}' | head -1 || true)
  [[ -n ${rx_max:-} && -n ${tx_max:-} ]] && { (( DRY )) || ethtool -G "$IFACE" rx "$rx_max" tx "$tx_max" 2>/dev/null || true; }

  # Busy-poll style NAPI: defer the hard IRQ and let the poll loop drain the ring.
  w 2      "/sys/class/net/$IFACE/napi_defer_hard_irqs"
  w 20000  "/sys/class/net/$IFACE/gro_flush_timeout"

  # RPS off - it costs an IPI per packet, which is exactly the jitter we are removing.
  for rq in /sys/class/net/"$IFACE"/queues/rx-*/rps_cpus; do w 0 "$rq"; done

  # XPS: give each exclusive core a deterministic TX queue so transmits never bounce
  # across sockets and never take a lock held by another NUMA node.
  mapfile -t excl < <(expand "$EXCLUSIVE_CORES" | tr ' ' '\n' | grep -v '^$')
  txqs=(/sys/class/net/"$IFACE"/queues/tx-*)
  if (( ${#txqs[@]} > 0 && ${#excl[@]} > 0 )); then
    declare -A qcpus=()
    for i in "${!excl[@]}"; do
      qi=$(( i % ${#txqs[@]} ))
      qcpus[$qi]="${qcpus[$qi]:+${qcpus[$qi]},}${excl[$i]}"
    done
    for qi in "${!qcpus[@]}"; do
      w "$(mask "${qcpus[$qi]}")" "/sys/class/net/$IFACE/queues/tx-$qi/xps_cpus"
    done
    log "XPS: ${#excl[@]} exclusive cores mapped over ${#txqs[@]} tx queues"
  fi
else
  log "WARN no primary interface found; skipping NIC tuning"
fi

# ---------------------------------------------------------------- 3. kernel threads
# Unbound and writeback workqueues default to every CPU. Confine them.
w "$NONISO_MASK" /sys/devices/virtual/workqueue/cpumask
w "$NONISO_MASK" /sys/bus/workqueue/devices/writeback/cpumask
for wq in /sys/devices/virtual/workqueue/*/cpumask; do w "$NONISO_MASK" "$wq"; done

# Best-effort: drag movable kernel threads off the isolated set. Per-CPU kthreads
# (ksoftirqd/N, migration/N, cpuhp/N) legitimately live there and are skipped.
for pid in $(ls /proc | grep -E '^[0-9]+$'); do
  [[ -r /proc/$pid/comm ]] || continue
  [[ -s /proc/$pid/cmdline ]] && continue            # non-empty cmdline => userspace, not a kthread
  comm=$(cat /proc/$pid/comm 2>/dev/null || echo)
  case "$comm" in
    ksoftirqd/*|migration/*|cpuhp/*|idle_inject/*|irq_work/*|rcuc/*|rcuog*|kworker/[0-9]*) continue ;;
  esac
  (( DRY )) || taskset -pc "$NON_ISOLATED_CPUS" "$pid" >/dev/null 2>&1 || true
done

# ---------------------------------------------------------------- 4. power / frequency
for g in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do w performance "$g"; done
for e in /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference; do w performance "$e"; done
w 100 /sys/devices/system/cpu/intel_pstate/min_perf_pct 2>/dev/null || true
# Turbo left ENABLED by default: on EC2 metal the all-core turbo bin is stable, and
# giving that up costs more than the residual frequency variance. Set LLTUNE_NO_TURBO=1
# if your workload needs a frequency-invariant timebase more than it needs the MHz.
if [[ ${LLTUNE_NO_TURBO:-0} == 1 ]]; then
  w 1 /sys/devices/system/cpu/intel_pstate/no_turbo
  w 0 /sys/devices/system/cpu/cpufreq/boost
fi

# ---------------------------------------------------------------- 5. memory
w never /sys/kernel/mm/transparent_hugepage/enabled
w never /sys/kernel/mm/transparent_hugepage/defrag
w 0     /sys/kernel/mm/transparent_hugepage/khugepaged/defrag
w 0     /sys/kernel/mm/ksm/run

# ---------------------------------------------------------------- 6. misc noise
w 0 /proc/sys/kernel/watchdog
w 0 /proc/sys/kernel/nmi_watchdog
w 0 /proc/sys/kernel/timer_migration
w 0 /proc/sys/kernel/numa_balancing
w 300 /proc/sys/vm/stat_interval

log "runtime tuning complete"
