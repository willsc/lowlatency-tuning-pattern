# Runbook

## Apply to a new host

```sh
git clone <this repo> /opt/lowlatency-tuning-pattern
cd /opt/lowlatency-tuning-pattern

sudo ./scripts/install.sh --live          # prints the plan, then installs all three layers
# review the core map it prints before rebooting
sudo reboot

./bin/lltune validate                     # must be 0 failed
sudo ./scripts/jitter-test.sh 300         # acceptance gate
```

`install.sh` is idempotent. Re-running it re-plans, re-renders and re-installs; only the
boot layer needs the reboot.

### Variants

```sh
sudo ./scripts/install.sh --live --no-boot                 # cgroups + runtime only, no reboot
sudo ./scripts/install.sh --profile c7a-48xl               # bake into an AMI, no live host needed
sudo ./scripts/install.sh --live --shared-ratio 0.20       # bigger shared pool
sudo ./scripts/install.sh --live --hugepages-1g-per-node 32
sudo ./scripts/install.sh --live --mitigations-off         # read docs/DESIGN.md §2 first
```

## Change the core split on a running fleet

Changing `shared_ratio` moves the boundary between shared and exclusive, which changes
`isolcpus` — so it is a **reboot**, not a live change.

```sh
sudo ./scripts/install.sh --live --shared-ratio 0.20
# drain the node, then:
sudo reboot
./bin/lltune validate
```

To preview the effect on every shape before touching anything:

```sh
for p in profiles/*.json; do
  n=$(basename "$p" .json); [ "$n" = policy ] && continue
  ./bin/lltune show --profile "$n" --shared-ratio 0.20 | head -12
done
```

## Verify

```sh
./bin/lltune validate            # all checks
./bin/lltune validate --quiet    # failures and warnings only — use this in monitoring
```

`lltune-validate.service` runs this on every boot and logs to the journal. It exits 0 even
on failure by design: a misconfigured node should alert, not refuse to boot.

```sh
journalctl -u lltune-validate.service -b
```

## Roll back

```sh
sudo ./scripts/uninstall.sh
sudo reboot                      # required to drop isolcpus / nosmt / nohz_full
```

`install.sh` backs up `/etc/default/grub` to `/etc/default/grub.lltune-backup.<epoch>` before
editing it on distros with no `grub.d` (AL2023, RHEL); `uninstall.sh` restores the newest.

## Debugging a latency regression

Work down the layers — each step rules out one of them.

**1. Did the boot layer survive a kernel update?**
```sh
cat /proc/cmdline
cat /sys/devices/system/cpu/isolated
cat /sys/devices/system/cpu/nohz_full
cat /sys/devices/system/cpu/smt/control
```
A kernel package update that regenerates `grub.cfg` from a stale `/etc/default/grub` is the
most common way isolation silently disappears. `validate` catches it.

**2. Is something running on an exclusive core that should not be?**
```sh
./bin/lltune validate --quiet
ps -eo pid,psr,comm,cgroup --sort=psr | awk '$2 >= 24'      # adjust to your isolated range
```

**3. Did an interrupt land on an isolated core?**
```sh
grep . /proc/irq/*/effective_affinity_list | sort -t: -k2 -n | tail -40
watch -n1 'grep -E "^\s*(LOC|RES|CAL|TLB|IWI)" /proc/interrupts'
```
Rising counts on an isolated core mean either a managed IRQ that `managed_irq` could not
place (check `ethtool -l` queue count vs. irqnet core count) or IPIs from another core doing
`smp_call_function` — usually TLB shootdowns from a process mapping/unmapping on a shared core.

**4. Is the tick actually off?**
```sh
grep -E "^\s*LOC" /proc/interrupts
```
On a properly isolated core running one busy thread, `LOC` should be nearly flat. If it is
ticking at 1000/s, `nohz_full` did not take, or there is more than one runnable task on that
core — `nohz_full` only stops the tick when the run queue has exactly one task.

**5. Frequency or C-state drift?**
```sh
turbostat --quiet --interval 5
cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor | sort -u
```

**6. Is it below the OS entirely?**
```sh
sudo hwlatdetect --duration=60 --threshold=10
```
Outliers here are firmware SMIs. Nothing in this repo will help; replace the instance.

## Application integration

The app reads `/etc/lowlatency/cores.env` and pins its own threads:

```sh
source /etc/lowlatency/cores.env
# EXCLUSIVE_CORES / SHARED_CORES / *_NODE<n> variants
```

Units must declare a slice — that is what grants access to the isolated CPUs:

```ini
[Service]
Slice=pulsar.slice
EnvironmentFile=/etc/lowlatency/cores.env
```

See `systemd/pulsar-broker.service.example`. The slice grants the union of both pools; the
application decides which thread goes where. For a JVM app, put the latency-critical IO
threads on `EXCLUSIVE_CORES` and let GC/JIT threads float on `SHARED_CORES` — pinning GC
threads to isolated cores defeats the isolation, since a GC pause on an exclusive core is
exactly the jitter you removed the tick to avoid.

Because nothing but the app enforces this split, it is worth asserting in the app's own
startup: refuse to boot if a thread pool is configured onto a core outside the pool it
belongs in. `lltune validate` cannot see inside the JVM.

To run an ad-hoc command on the isolated set (SSH sessions are confined to housekeeping):

```sh
sudo systemd-run --slice=pulsar.slice -t /bin/bash
```

## Adding a new instance shape

```sh
# on one instance of the new shape
./bin/lltune topology --live -v > /tmp/topo.json
```
Read off `sockets`, `cores_per_socket`, `threads_per_core`, `numa_nodes`, `cores_per_l3`,
write `profiles/<name>.json`, then confirm the synthetic plan matches the live one:

```sh
diff <(./bin/lltune plan --profile <name>) <(./bin/lltune plan --live)
```
