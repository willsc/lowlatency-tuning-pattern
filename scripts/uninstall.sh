#!/usr/bin/env bash
# Full rollback. Boot layer removal needs a reboot; everything else is immediate.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

systemctl disable --now lltune-runtime.service lltune-validate.service 2>/dev/null || true
rm -f /etc/systemd/system/lltune-{runtime,validate}.service
rm -f /etc/systemd/system/{irqnet,pulsar}.slice
rm -f /etc/systemd/system/{system.slice,user.slice,machine.slice,init.scope}.d/10-lowlatency.conf
rm -f /etc/systemd/system/irqbalance.service.d/10-lowlatency.conf
rm -f /etc/sysctl.d/99-lowlatency.conf
rm -f /etc/default/grub.d/99-lowlatency.cfg
rm -rf /etc/lowlatency
systemctl daemon-reload
sysctl --system >/dev/null

if [[ ! -e /etc/default/grub.d/99-lowlatency.cfg ]] && grep -q isolcpus /etc/default/grub 2>/dev/null; then
  latest=$(ls -1t /etc/default/grub.lltune-backup.* 2>/dev/null | head -1 || true)
  if [[ -n $latest ]]; then
    cp -a "$latest" /etc/default/grub
    echo "restored /etc/default/grub from $latest"
  else
    echo "WARNING: lltune args remain in /etc/default/grub and no backup was found; edit by hand." >&2
  fi
fi
command -v update-grub >/dev/null && update-grub || grub2-mkconfig -o /boot/grub2/grub.cfg 2>/dev/null || true
echo "rolled back. reboot to drop isolcpus/nosmt/nohz_full."
