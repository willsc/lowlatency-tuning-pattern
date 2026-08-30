"""Shared material for the documentation generators.

Both docs/layers.html and docs/CONFIG-REFERENCE.md describe the same kernel
arguments. Keeping the explanations here means the two cannot disagree about
what a flag does — the same drift the planner exists to prevent, one level up.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_lltune():
    """Import bin/lltune as a module (it has no .py extension)."""
    loader = SourceFileLoader("lltune", str(REPO / "bin" / "lltune"))
    spec = importlib.util.spec_from_loader("lltune", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CPU_NAME = {
    "c7i-24xl": "Intel Sapphire Rapids", "c7i-48xl": "Intel Sapphire Rapids",
    "c8i-48xl": "Intel Xeon 6 (Granite Rapids)", "c8i-96xl": "Intel Xeon 6975P-C",
    "c7a-48xl": "AMD EPYC 9R14 (Genoa)",
    "c8a-24xl": "AMD EPYC Turin", "c8a-48xl": "AMD EPYC Turin",
}

ORDER = ["c7i-24xl", "c7i-48xl", "c8i-48xl", "c8i-96xl", "c7a-48xl", "c8a-24xl", "c8a-48xl"]

# Why each kernel argument is there. Keyed by the part before '=' so values stay data.
ARG_GROUPS = [
    ["Isolation", [
        ["nosmt", "Disable SMT - a sibling thread contends for the same execution ports and L1/L2"],
        ["isolcpus", "Remove these cores from every scheduler domain; steer managed IRQs away"],
        ["nohz_full", "Stop the 1 kHz tick on cores running a single runnable task"],
        ["rcu_nocbs", "Move RCU callback processing onto housekeeping cores"],
        ["rcu_nocb_poll", "Poll for RCU callbacks instead of waking the kthread"],
        ["irqaffinity", "Boot-time default affinity for every non-managed interrupt"],
        ["nohz", "Enable the dynamic tick"],
    ]],
    ["Scheduler and NUMA", [
        ["numa_balancing", "Off - automatic balancing unmaps pages to sample them, and the fault stalls"],
        ["skew_tick", "De-synchronise per-CPU ticks so cores don't contend on the same locks"],
    ]],
    ["Timers, watchdogs, noise", [
        ["nmi_watchdog", "Stop periodic NMI sampling"],
        ["nosoftlockup", "Stop the soft-lockup detector"],
        ["mce", "Ignore corrected machine checks rather than handling a storm"],
        ["tsc", "Trust the TSC; skip the watchdog that can demote the clocksource"],
        ["clocksource", "Pin the clocksource - a mid-flight demotion to HPET makes every clock read a syscall"],
        ["audit", "Disable the audit subsystem"],
        ["rcupdate.rcu_normal_after_boot", "Use normal, non-expedited RCU once boot is done"],
    ]],
    ["Power and frequency", [
        ["processor.max_cstate", "Cap the ACPI C-state - C6 exit latency is tens of microseconds"],
        ["intel_idle.max_cstate", "Cap intel_idle at C1"],
        ["cpufreq.default_governor", "Set the governor before any workload starts"],
        ["idle", "Poll instead of idling - opt-in, costs ~100% power on every core"],
    ]],
    ["Memory", [
        ["transparent_hugepage", "Never - THP defrag stalls are milliseconds long"],
        ["default_hugepagesz", "Explicit hugepage size for the JVM heap"],
        ["hugepagesz", "Hugepage size to reserve"],
        ["hugepages", "Number of hugepages reserved at boot"],
    ]],
    ["PCIe and IOMMU", [
        ["pcie_aspm", "No PCIe link power states - exit latency lands on the first packet after idle"],
        ["iommu", "Passthrough - no DMA translation on the data path"],
    ]],
    ["cgroup v2", [
        ["systemd.unified_cgroup_hierarchy", "Required for the cpuset controller the slice layer uses"],
        ["cgroup_no_v1", "Disable the v1 controllers"],
    ]],
    ["Security trade-off", [
        ["mitigations", "Speculative-execution mitigations off - opt-in, real latency win, real exposure"],
    ]],
]

WHY = {k: v for _, rows in ARG_GROUPS for k, v in rows}
