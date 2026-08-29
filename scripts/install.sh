#!/usr/bin/env bash
# Install a rendered plan onto this host.
#
#   ./scripts/install.sh --live                 plan from the real topology (normal case)
#   ./scripts/install.sh --profile amd-48xl     plan from a profile (bake into an AMI)
#   ./scripts/install.sh --live --no-boot       cgroup + runtime only, no reboot needed
#
# The boot layer needs a reboot. Nothing else does.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATE=/etc/lowlatency
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

SRC=(--live)
DO_BOOT=1
DO_RUNTIME=1
EXTRA=()

while (( $# )); do
  case $1 in
    --live)        SRC=(--live); shift ;;
    --profile)     SRC=(--profile "$2"); shift 2 ;;
    --policy)      EXTRA+=(--policy "$2"); shift 2 ;;
    --shared-ratio) EXTRA+=(--shared-ratio "$2"); shift 2 ;;
    --mitigations-off) EXTRA+=(--mitigations-off); shift ;;
    --hugepages-1g-per-node) EXTRA+=(--hugepages-1g-per-node "$2"); shift 2 ;;
    --no-boot)     DO_BOOT=0; shift ;;
    --no-runtime)  DO_RUNTIME=0; shift ;;
    -h|--help)     sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

echo "==> planning"
"$REPO/bin/lltune" plan "${SRC[@]}" "${EXTRA[@]}" -o "$STAGE/plan.json"
"$REPO/bin/lltune" render --plan "$STAGE/plan.json" -o "$STAGE/out"
"$REPO/bin/lltune" show  --plan "$STAGE/plan.json"

echo
echo "==> installing plan + core contract into $STATE"
install -d "$STATE"
install -m 0644 "$STAGE/out/plan.json"  "$STATE/plan.json"
install -m 0644 "$STAGE/out/cores.env"  "$STATE/cores.env"

echo "==> installing sysctl"
install -m 0644 "$REPO/sysctl/99-lowlatency.conf" /etc/sysctl.d/99-lowlatency.conf
sysctl --system >/dev/null

echo "==> installing cgroup slices"
# Order matters: parent slices must exist before children reference them.
for f in system.slice user.slice machine.slice init.scope; do
  install -d "/etc/systemd/system/$f.d"
  install -m 0644 "$STAGE/out/systemd/$f.d/10-lowlatency.conf" "/etc/systemd/system/$f.d/10-lowlatency.conf"
done
for f in irqnet.slice pulsar.slice pulsar-exclusive.slice pulsar-shared.slice; do
  install -m 0644 "$STAGE/out/systemd/$f" "/etc/systemd/system/$f"
done
install -m 0644 "$REPO/systemd/lltune-runtime.service"  /etc/systemd/system/
install -m 0644 "$REPO/systemd/lltune-validate.service" /etc/systemd/system/
systemctl daemon-reload
systemctl start irqnet.slice pulsar.slice pulsar-exclusive.slice pulsar-shared.slice
systemctl enable lltune-runtime.service lltune-validate.service >/dev/null

if (( DO_BOOT )); then
  echo "==> installing GRUB cmdline"
  if [[ -d /etc/default/grub.d ]]; then
    install -m 0644 "$STAGE/out/grub/99-lowlatency.cfg" /etc/default/grub.d/99-lowlatency.cfg
  else
    # AL2023 / RHEL have no grub.d: append into /etc/default/grub, replacing our block.
    cp -a /etc/default/grub "/etc/default/grub.lltune-backup.$(date +%s)"
    python3 - "$STAGE/plan.json" <<'PY'
import json, re, sys, pathlib
plan = json.load(open(sys.argv[1]))
p = pathlib.Path("/etc/default/grub")
lines = [l for l in p.read_text().splitlines()
         if not l.startswith("GRUB_CMDLINE_LINUX_LLTUNE")]
out, seen = [], False
for l in lines:
    m = re.match(r'^GRUB_CMDLINE_LINUX=(.*)$', l)
    if m:
        val = m.group(1).strip().strip('"')
        # Strip any previously installed lltune args before re-adding.
        keep = [a for a in val.split() if not re.match(
            r'^(nosmt|isolcpus|nohz_full|rcu_nocbs|rcu_nocb_poll|irqaffinity|nohz|numa_balancing'
            r'|skew_tick|nmi_watchdog|nosoftlockup|mce|tsc|clocksource|audit|rcupdate\.|processor\.'
            r'|intel_idle\.|cpufreq\.|idle|transparent_hugepage|default_hugepagesz|hugepagesz'
            r'|hugepages|pcie_aspm|iommu|systemd\.unified|cgroup_no_v1|mitigations)', a)]
        l = 'GRUB_CMDLINE_LINUX="' + " ".join(keep + plan["cmdline"].split()) + '"'
        seen = True
    out.append(l)
if not seen:
    out.append('GRUB_CMDLINE_LINUX="' + plan["cmdline"] + '"')
p.write_text("\n".join(out) + "\n")
print("  patched /etc/default/grub")
PY
  fi

  if command -v update-grub >/dev/null 2>&1; then
    update-grub
  elif [[ -d /sys/firmware/efi ]]; then
    grub2-mkconfig -o /boot/efi/EFI/*/grub.cfg
  else
    grub2-mkconfig -o /boot/grub2/grub.cfg
  fi
fi

if (( DO_RUNTIME )); then
  echo "==> applying runtime tuning"
  systemctl start lltune-runtime.service || true
fi

echo
if (( DO_BOOT )); then
  echo "REBOOT REQUIRED for the boot layer (isolcpus / nosmt / nohz_full)."
  echo "After reboot run:  $REPO/bin/lltune validate"
else
  "$REPO/bin/lltune" validate --quiet || true
fi
