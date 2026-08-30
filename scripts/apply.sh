#!/usr/bin/env bash
# The front door. Shows the whole configuration and exactly what it would change on this
# host, then — only if you ask — applies it.
#
#   ./scripts/apply.sh                          dry run against this host's real topology
#   ./scripts/apply.sh --profile c8i-96xl       dry run for a shape you are not logged into
#   ./scripts/apply.sh --brief                  statuses only, no unified diffs
#   ./scripts/apply.sh --full                   every diff in full, never truncated
#   sudo ./scripts/apply.sh --apply             apply it (asks first)
#   sudo ./scripts/apply.sh --apply --no-boot   cgroup + runtime only, no reboot needed
#   sudo ./scripts/apply.sh --apply --yes       apply without the confirmation prompt
#
# Dry run is the default: it writes nothing, anywhere, and needs no root. Everything the
# dry run prints comes from the same plan that --apply installs, so what you review is
# what lands on the box.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
STATE=/etc/lowlatency
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

SRC=(--live)
EXTRA=()
SRC_LABEL="live topology"
APPLY=0 YES=0 DO_BOOT=1 DO_RUNTIME=1 BRIEF=0
DIFF_MAX=40   # lines of diff per file before truncating; --full lifts the cap
ARGV=("$@")

while (( $# )); do
  case $1 in
    --live)            SRC=(--live); SRC_LABEL="live topology"; shift ;;
    --profile)         SRC=(--profile "$2"); SRC_LABEL="profile $2"; shift 2 ;;
    --policy)          EXTRA+=(--policy "$2"); shift 2 ;;
    --shared-ratio)    EXTRA+=(--shared-ratio "$2"); shift 2 ;;
    --mitigations-off) EXTRA+=(--mitigations-off); shift ;;
    --idle-poll)       EXTRA+=(--idle-poll); shift ;;
    --hugepages-1g-per-node) EXTRA+=(--hugepages-1g-per-node "$2"); shift 2 ;;
    --apply)           APPLY=1; shift ;;
    --yes|-y)          YES=1; shift ;;
    --no-boot)         DO_BOOT=0; shift ;;
    --no-runtime)      DO_RUNTIME=0; shift ;;
    --brief)           BRIEF=1; shift ;;
    --full)            DIFF_MAX=0; shift ;;
    -h|--help)         sed -n '2,14p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ---------------------------------------------------------------- output helpers
if [[ -t 1 ]]; then
  B=$'\e[1m'; DIM=$'\e[2m'; R=$'\e[0m'
  RED=$'\e[31m'; GRN=$'\e[32m'; YEL=$'\e[33m'; BLU=$'\e[34m'
else
  B=""; DIM=""; R=""; RED=""; GRN=""; YEL=""; BLU=""
fi

rule() { printf '%s\n' "${DIM}$(printf '─%.0s' $(seq 1 78))${R}"; }
head1() { printf '\n%s%s%s\n' "$B" "$1" "$R"; rule; }
head2() { printf '\n%s%s%s  %s%s%s\n' "$B" "$1" "$R" "$DIM" "${2:-}" "$R"; }
note()  { printf '  %s%s%s\n' "$DIM" "$1" "$R"; }

CREATED=0 MODIFIED=0 SAME=0

# norm <cpulist> -> sorted comma list of individual cpus, so "0-3" and "0,1,2,3" compare equal
norm() { python3 -c '
import sys
out = set()
for p in sys.argv[1].split(","):
    p = p.strip()
    if not p: continue
    if "-" in p:
        a, b = p.split("-"); out.update(range(int(a), int(b) + 1))
    else: out.add(int(p))
print(",".join(map(str, sorted(out))))' "$1"; }

# cmp_file <staged> <dest> [label]
cmp_file() {
  local src=$1 dest=$2 label=${3:-$2} status colour
  if [[ ! -e $dest ]]; then
    status="CREATE"; colour=$GRN; CREATED=$((CREATED+1))
  elif [[ ! -r $dest ]]; then
    status="UNREADABLE"; colour=$YEL
    printf '  %s%-10s%s %s %s(need root to compare)%s\n' "$colour" "$status" "$R" "$label" "$DIM" "$R"
    return
  elif diff -q "$src" "$dest" >/dev/null 2>&1; then
    status="unchanged"; colour=$DIM; SAME=$((SAME+1))
  else
    status="MODIFY"; colour=$YEL; MODIFIED=$((MODIFIED+1))
  fi
  printf '  %s%-10s%s %s\n' "$colour" "$status" "$R" "$label"
  if (( ! BRIEF )) && [[ $status != unchanged ]]; then
    # A file that does not exist yet diffs against /dev/null, and diff exits 1 whenever
    # there are differences - which is always, here - so the pipeline needs '|| true'
    # under 'set -o pipefail'.
    local base=$dest
    [[ -e $dest ]] || base=/dev/null
    local body total
    body=$({ diff -u --label "$dest (current)" --label "$dest (planned)" "$base" "$src" || true; } \
      | tail -n +3)
    total=$(printf '%s\n' "$body" | wc -l)
    if (( DIFF_MAX > 0 && total > DIFF_MAX )); then
      printf '%s\n' "$body" | head -n "$DIFF_MAX" \
        | sed "s/^-/${RED}-/;s/^+/${GRN}+/;s/^@/${BLU}@/;s/\$/${R}/;s/^/      /"
      printf '      %s... %d more lines (--full to see them all)%s\n' \
        "$DIM" "$((total - DIFF_MAX))" "$R"
    else
      printf '%s\n' "$body" \
        | sed "s/^-/${RED}-/;s/^+/${GRN}+/;s/^@/${BLU}@/;s/\$/${R}/;s/^/      /"
    fi
  fi
}

# ---------------------------------------------------------------- plan
# Swallow the progress chatter, but show everything if either step actually fails -
# "unknown profile" and "policy wants N/M non-isolated cores" both arrive this way.
run_quiet() {
  local out
  if ! out=$("$@" 2>&1); then printf '%s\n' "$out" >&2; exit 1; fi
}
run_quiet "$REPO/bin/lltune" plan "${SRC[@]}" "${EXTRA[@]}" -o "$STAGE/plan.json"
run_quiet "$REPO/bin/lltune" render --plan "$STAGE/plan.json" -o "$STAGE/out"

CMDLINE=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["cmdline"])' "$STAGE/plan.json")
PLAN_NODES=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["topology"]["numa_nodes"])' "$STAGE/plan.json")
LIVE_NODES=$(ls -d /sys/devices/system/node/node[0-9]* 2>/dev/null | wc -l)

if (( APPLY )); then
  MODE="${RED}APPLY${R} — changes will be written to this host"
else
  MODE="${GRN}DRY RUN${R} — nothing will be written"
fi

head1 "lltune apply"
printf '  %-12s %s\n' "mode" "$MODE"
printf '  %-12s %s\n' "source" "$SRC_LABEL"
printf '  %-12s %s\n' "host" "$(hostname) · $(uname -r) · $(nproc) online cpus"
printf '  %-12s %s\n' "layers" "$( ((DO_BOOT)) && echo -n "boot " )cgroup $( ((DO_RUNTIME)) && echo -n "runtime " )contract"
if [[ ${SRC[0]} == --profile ]] && (( LIVE_NODES > 0 )) && (( LIVE_NODES != PLAN_NODES )); then
  printf '  %s%-12s %s%s\n' "$YEL" "warning" \
    "profile assumes $PLAN_NODES NUMA node(s); this host has $LIVE_NODES. Use --live here.$R"
fi

head1 "PLAN"
"$REPO/bin/lltune" show --plan "$STAGE/plan.json" | sed 's/^/  /'

# ---------------------------------------------------------------- layer 1
if (( DO_BOOT )); then
  head1 "LAYER 1 — boot isolation (GRUB)"
  if [[ -d /etc/default/grub.d ]]; then
    cmp_file "$STAGE/out/grub/99-lowlatency.cfg" /etc/default/grub.d/99-lowlatency.cfg
  else
    note "no /etc/default/grub.d on this host: install.sh patches /etc/default/grub in place"
    note "after taking a timestamped backup, replacing any previous lltune block."
    if [[ -r /etc/default/grub ]]; then
      printf '  %s%-10s%s %s\n' "$YEL" "MODIFY" "$R" "/etc/default/grub"
      MODIFIED=$((MODIFIED+1))
    fi
  fi

  head2 "against the running kernel" "/proc/cmdline"
  MISSING=()
  for a in $CMDLINE; do
    grep -qw -- "$a" /proc/cmdline 2>/dev/null || MISSING+=("$a")
  done
  if (( ${#MISSING[@]} == 0 )); then
    printf '  %s%s%s the running kernel already carries every planned argument — no reboot needed\n' \
      "$GRN" "OK" "$R"
    REBOOT=0
  else
    printf '  %s%d of %d planned arguments are not on the running kernel:%s\n' \
      "$YEL" "${#MISSING[@]}" "$(echo "$CMDLINE" | wc -w)" "$R"
    printf '      %s\n' "${MISSING[@]}"
    printf '  %sREBOOT REQUIRED%s before the boot layer takes effect.\n' "$B" "$R"
    REBOOT=1
  fi
else
  head1 "LAYER 1 — boot isolation (GRUB)"
  note "skipped (--no-boot); the running kernel's isolation is left exactly as it is"
  REBOOT=0
fi

# ---------------------------------------------------------------- layer 2
head1 "LAYER 2 — cgroup v2 slices"
for f in system.slice user.slice machine.slice init.scope; do
  cmp_file "$STAGE/out/systemd/$f.d/10-lowlatency.conf" \
           "/etc/systemd/system/$f.d/10-lowlatency.conf"
done
for f in irqnet.slice pulsar.slice; do
  cmp_file "$STAGE/out/systemd/$f" "/etc/systemd/system/$f"
done
cmp_file "$REPO/systemd/lltune-runtime.service"  /etc/systemd/system/lltune-runtime.service
cmp_file "$REPO/systemd/lltune-validate.service" /etc/systemd/system/lltune-validate.service

head2 "against the live cpusets" "/sys/fs/cgroup"
for pair in "system.slice:$STAGE/out/systemd/system.slice.d/10-lowlatency.conf" \
            "irqnet.slice:$STAGE/out/systemd/irqnet.slice" \
            "pulsar.slice:$STAGE/out/systemd/pulsar.slice"; do
  sl=${pair%%:*}; file=${pair#*:}
  want=$(sed -n 's/^AllowedCPUs=//p' "$file" | head -1)
  live=/sys/fs/cgroup/$sl/cpuset.cpus.effective
  if [[ ! -r $live ]]; then
    printf '  %s%-10s%s %-14s planned %s\n' "$DIM" "absent" "$R" "$sl" "$want"
  elif [[ $(norm "$(cat "$live")") == $(norm "$want") ]]; then
    printf '  %s%-10s%s %-14s %s\n' "$GRN" "active" "$R" "$sl" "$want"
  else
    printf '  %s%-10s%s %-14s live %s  ->  planned %s\n' \
      "$YEL" "CHANGE" "$R" "$sl" "$(cat "$live")" "$want"
  fi
done

# The runtime layer writes the same value to one file per CPU, per queue, per workqueue.
# Printed raw that is several hundred lines saying the same four things, so paths that
# differ only in their numbers are collapsed to one line and a count.
collapse_writes() { python3 -c '
import re, sys

def norm(path):
    return re.sub(r"\d+", "#", path)

def merge(paths):
    """Star only the digit runs that actually differ across the group."""
    parts = [re.split(r"(\d+)", p) for p in paths]
    out = []
    for i, piece in enumerate(parts[0]):
        out.append(piece if all(p[i] == piece for p in parts) else "*")
    return "".join(out)

lines = sys.stdin.read().splitlines()
groups, order = {}, []
for line in lines:
    m = re.match(r"^\s*would write (.+?) -> (\S+)(.*)$", line)
    if not m:
        order.append(("raw", line))
        continue
    val, path, tail = m.groups()
    k = (val, norm(path), tail)
    if k not in groups:
        groups[k] = []
        order.append(("group", k))
    groups[k].append(path)

for kind, item in order:
    if kind == "raw":
        print(item)
        continue
    val, _, tail = item
    paths = groups[item]
    shown = merge(paths) if len(paths) > 1 else paths[0]
    count = f"  x{len(paths)}" if len(paths) > 1 else ""
    print(f"  would write {val} -> {shown}{tail}{count}")
'; }

# ---------------------------------------------------------------- layer 3
if (( DO_RUNTIME )); then
  head1 "LAYER 3 — runtime pinning (IRQ, NIC, workqueues, power)"
  note "dry run of scripts/apply-runtime.sh against the plan above; writes nothing."
  if ENV_FILE="$STAGE/out/cores.env" PLAN="$STAGE/plan.json" DRY_RUN=1 \
       "$REPO/scripts/apply-runtime.sh" >"$STAGE/runtime.log" 2>&1; then
    collapse_writes <"$STAGE/runtime.log" | sed 's/^/  /'
    note "per-cpu paths collapsed; for the full list run:"
    note "  ENV_FILE=$STATE/cores.env DRY_RUN=1 $REPO/scripts/apply-runtime.sh"
  else
    printf '  %sthe runtime dry run failed:%s\n' "$YEL" "$R"
    sed 's/^/    /' "$STAGE/runtime.log"
  fi
else
  head1 "LAYER 3 — runtime pinning"
  note "skipped (--no-runtime)"
fi

# ---------------------------------------------------------------- layer 4
head1 "LAYER 4 — application contract and state"
cmp_file "$STAGE/out/cores.env" "$STATE/cores.env"
cmp_file "$STAGE/out/plan.json" "$STATE/plan.json"
cmp_file "$REPO/sysctl/99-lowlatency.conf" /etc/sysctl.d/99-lowlatency.conf

# ---------------------------------------------------------------- summary
head1 "SUMMARY"
printf '  %s%d%s to create · %s%d%s to modify · %s%d%s unchanged\n' \
  "$GRN" "$CREATED" "$R" "$YEL" "$MODIFIED" "$R" "$DIM" "$SAME" "$R"
if (( REBOOT )); then
  printf '  reboot required: %syes%s (boot layer)\n' "$B" "$R"
else
  printf '  reboot required: no\n'
fi

if (( ! APPLY )); then
  CMD="sudo $0"
  (( ${#ARGV[@]} )) && CMD="$CMD ${ARGV[*]}"
  printf '\n  Nothing was written. To apply:\n    %s%s --apply%s\n\n' "$B" "$CMD" "$R"
  exit 0
fi

# ---------------------------------------------------------------- apply
[[ $EUID -eq 0 ]] || { echo; echo "${RED}--apply must run as root${R}" >&2; exit 1; }

if (( ! YES )); then
  if [[ ! -t 0 ]]; then
    echo; echo "${RED}refusing to apply non-interactively without --yes${R}" >&2; exit 1
  fi
  echo
  read -r -p "Apply the ${CREATED} new and ${MODIFIED} changed files to $(hostname)? [y/N] " ans
  [[ ${ans,,} == y* ]] || { echo "aborted; nothing was written"; exit 1; }
fi

head1 "APPLYING"
INSTALL=("$REPO/scripts/install.sh" --plan "$STAGE/plan.json")
(( DO_BOOT ))    || INSTALL+=(--no-boot)
(( DO_RUNTIME )) || INSTALL+=(--no-runtime)
"${INSTALL[@]}"

if (( REBOOT )); then
  printf '\n  %sReboot to activate the boot layer, then:%s  %s/bin/lltune validate\n' \
    "$B" "$R" "$REPO"
fi
