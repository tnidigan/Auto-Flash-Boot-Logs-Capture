#!/usr/bin/env python3
r"""
Collects boot-time metrics from a target device and records them into a
running multi-run JSON + regenerated HTML report, one report pair per target:

    bootchart-data-<slug>.json
    bootchart-overview-<slug>.html

<slug> is the target name with hyphens stripped, e.g. "iq-9075-evk" -> "iq9075evk".

Per boot, a device-side script (collect_boot_logs.sh, provisioned to
/data/collect_boot_logs.sh on first use) dumps systemd-analyze/blame/dmesg/etc.
into /data/Logs, which is then renamed to /data/Logs-<n> so each boot's dump
survives the next boot's run. Login, script provisioning/execution, and the
per-boot rename all happen over the serial console (see edl_flash.open_console /
edl_flash.run_serial_command) -- no adb/USB involved for that part. Only after
all boots are done is adb enabled (touch /etc/usb-debugging-enabled; systemctl
start android-tools-adbd) for a single `adb pull` of every Logs-<n> folder
into Boot-Logs/<target>/<build-folder>/, which is then parsed to build each
boot's metrics.

Normally invoked automatically by edl_flash.py right after a successful flash.
Can also be run standalone to (re-)capture without reflashing:

    py -3 boot_capture.py --build-path "\\swayam\QLI_Builds\Yocto\<id>_Nightly_Build_master_<N>\performance" \
        [--target iq-9075-evk] [--com-port COM40] [--boot-timeout 480]

Requires: pip install pyserial  (for the ARM64 interpreter, i.e. `py -3 -m pip install pyserial`)
Requires: adb on PATH, for the post-capture log pull.
"""

import argparse
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import edl_flash
import bootchart_report

DELTA_MERGE_S = 0.05  # threshold for noting graphical.target overshoot

COLLECT_SCRIPT_REMOTE_PATH = "/data/collect_boot_logs.sh"
COLLECT_SCRIPT_HEREDOC_MARKER = "BOOT_LOG_SCRIPT_EOF"

COLLECT_SCRIPT_CONTENT = """#!/bin/sh
# collect_boot_logs.sh
# This script runs locally on the target to collect boot optimization logs.

echo "=========================================="
echo "    Target Boot Log Collection Script     "
echo "=========================================="

setenforce 0

echo "[0/4] Disabling Remote FS"
systemctl disable rmtfs.service
systemctl mask rmtfs.service

#echo "[1/4] Disabling Network Wait-Online Services..."
#systemctl stop systemd-networkd-wait-online.service 2>/dev/null
#systemctl disable systemd-networkd-wait-online.service 2>/dev/null
#systemctl disable NetworkManager-wait-online.service 2>/dev/null
#systemctl mask NetworkManager-wait-online.service 2>/dev/null

echo "[2/4] Preparing /data/Logs directory..."
mkdir -p /data/Logs
rm -f /data/Logs/*

echo "Turning off kernel tracing..."
echo 0 > /sys/kernel/tracing/tracing_on 2>/dev/null || true

echo "[3/4] Collecting System Logs..."
cp /sys/kernel/tracing/trace /data/Logs/Boot_Trace.txt 2>/dev/null || true
systemd-analyze > /data/Logs/systemd_analyze.txt
systemd-analyze blame > /data/Logs/blame_systemd.txt
systemd-analyze plot > /data/Logs/plot_systemd.svg
systemctl > /data/Logs/blame_systemdsystemctl.txt
journalctl --output=short-monotonic -b --no-pager -l > /data/Logs/journalctl.log
dmesg > /data/Logs/dmesg.txt
systemd-analyze critical-chain > /data/Logs/systemd-analyze_critical_chain.txt
systemd-analyze critical-chain sysinit.target > /data/Logs/systemd-analyze_critical_chain_sysinit.txt

# Extract config if present
if [ -f /proc/config.gz ]; then
    zcat /proc/config.gz > /data/Logs/proc_config.txt
fi

cat /proc/cmdline > /data/Logs/kernel_cmdline.txt
lsmod > /data/Logs/lsmod.txt

echo
echo "------------------------------------------"
cat /data/Logs/systemd_analyze.txt
echo "------------------------------------------"

echo "[4/4] Generating Summary (brief.txt)..."
"""


def _slugify(target: str) -> str:
    return target.replace("-", "").replace("_", "")


def _data_html_paths(target: str):
    slug = _slugify(target)
    base = bootchart_report.BOOT_CHARTS_DIR
    return base / f"bootchart-data-{slug}.json", base / f"bootchart-overview-{slug}.html"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

TIME_TOKEN_RE = re.compile(r"([\d.]+)(ms|s)")


def _to_seconds(num: str, unit: str) -> float:
    value = float(num)
    return value / 1000.0 if unit == "ms" else value


def parse_systemd_time(text: str) -> dict:
    """Parses the 'Startup finished in ...' line from `systemd-analyze time`."""
    m = re.search(
        r"Startup finished in (.+?)\s*=\s*([\d.]+)(ms|s)",
        text,
    )
    if not m:
        raise RuntimeError(f"Could not parse `systemd-analyze time` output:\n{text}")
    parts_text, total_num, total_unit = m.group(1), m.group(2), m.group(3)

    parts = {}
    for num, unit, label in re.findall(r"([\d.]+)(ms|s)\s*\((\w+)\)", parts_text):
        parts[label] = _to_seconds(num, unit)

    result = {
        "firmware": parts.get("firmware", 0.0),
        "loader": parts.get("loader", 0.0),
        "kernel": parts.get("kernel", 0.0),
        "userspace": parts.get("userspace", 0.0),
        "total": _to_seconds(total_num, total_unit),
    }

    gm = re.search(r"graphical\.target reached after ([\d.]+)(ms|s)", text)
    if gm:
        result["graphical_userspace"] = _to_seconds(gm.group(1), gm.group(2))
    return result


def parse_critical_chain(text: str) -> tuple:
    """Returns (top_level_userspace_seconds, chain_names_top_to_bottom).
    The '@Xs' on each line is time-since-userspace-start (PID 1), NOT
    time-since-power-on -- confirmed against real device output where the
    top target's @ value matches systemd-analyze time's userspace figure."""
    lines = [l for l in text.splitlines() if re.search(r"@[\d.]+(?:ms|s)", l)]
    if not lines:
        raise RuntimeError(f"Could not parse `systemd-analyze critical-chain` output:\n{text}")

    top_m = re.search(r"@([\d.]+)(ms|s)", lines[0])
    if not top_m:
        raise RuntimeError(f"Could not find '@time' on first critical-chain line: {lines[0]!r}")
    top_seconds = _to_seconds(top_m.group(1), top_m.group(2))

    chain = []
    for line in lines:
        m = re.search(r"[`\-\s]*([\w.@:\\+-]+\.(?:service|target|socket|device|mount|slice))\s*@", line)
        if m:
            chain.append(m.group(1))
    return top_seconds, chain


STAGE_HITTER_RE = re.compile(
    r"[`\-\s]*([\w.@:\\+-]+\.(?:service|target|socket|device|mount|slice))"
    r"\s*@[\d.]+(?:ms|s)\s+\+([\d.]+)(ms|s)"
)


def parse_stage_hitters(text: str, top_n: int = 6) -> list:
    """Ranks units in a `systemd-analyze critical-chain` block by their own
    '+cost' (time the unit itself took to start), descending. Unlike
    parse_critical_chain's `chain` (top-to-bottom dependency order), this is
    a ranking -- used for per-stage high hitters (e.g. sysinit_svc), where
    there's no `blame`-style command scoped to a single target."""
    costs = []
    for m in STAGE_HITTER_RE.finditer(text):
        name, num, unit = m.group(1), m.group(2), m.group(3)
        costs.append((_to_seconds(num, unit), name))
    costs.sort(key=lambda t: t[0], reverse=True)
    return [{"name": name, "time": f"{secs:.3f} s"} for secs, name in costs[:top_n]]


def parse_blame(text: str, top_n: int = 6) -> list:
    hitters = []
    for line in text.splitlines():
        m = re.match(r"\s*([\d.]+)(ms|s)\s+(\S+)", line)
        if not m:
            continue
        seconds = _to_seconds(m.group(1), m.group(2))
        hitters.append({"name": m.group(3), "time": f"{seconds:.3f} s"})
        if len(hitters) >= top_n:
            break
    if not hitters:
        raise RuntimeError(f"Could not parse `systemd-analyze blame` output:\n{text}")
    return hitters


def parse_dmesg_milestones(text: str) -> dict:
    def find(pattern):
        m = re.search(pattern, text)
        if not m:
            raise RuntimeError(f"dmesg milestone not found ({pattern!r}) in:\n{text}")
        return float(m.group(1))

    return {
        "init_exec": find(r"\[\s*([\d.]+)\]\s*Run /init as init process"),
        "epoch_advanced": find(r"\[\s*([\d.]+)\]\s*systemd\[1\]: System time advanced to built-in epoch"),
        "systemd_running": find(r"\[\s*([\d.]+)\]\s*systemd\[1\]: systemd .* running in system mode"),
    }


def parse_journalctl_milestone(text: str) -> float:
    """Returns the monotonic timestamp of 'Reached target System
    Initialization.' from journalctl --output=short-monotonic -- this
    milestone isn't reliably present in dmesg's own ring buffer, unlike the
    epoch-advance line, so it's read from the journal instead."""
    m = re.search(r"\[\s*([\d.]+)\]\s*.*Reached target System Initialization\.", text)
    if not m:
        raise RuntimeError(
            f"journalctl milestone not found ('Reached target System Initialization.') in:\n{text}"
        )
    return float(m.group(1))


def build_run(target: str, build_path: str, sat_text: str, cc_text: str,
              cc_sysinit_text: str, blame_text: str, dmesg_text: str, journalctl_text: str) -> dict:
    sat = parse_systemd_time(sat_text)
    cc_multiuser_s, chain = parse_critical_chain(cc_text)
    hitters = parse_blame(blame_text)
    sysinit_hitters = parse_stage_hitters(cc_sysinit_text)
    milestones = parse_dmesg_milestones(dmesg_text)
    sysinit_target_s = parse_journalctl_milestone(journalctl_text)

    nhlos = sat["firmware"] + sat["loader"]
    kernel = sat["kernel"]

    init_exec = milestones["init_exec"]
    epoch_advanced = milestones["epoch_advanced"]
    initramfs_s = epoch_advanced - init_exec
    sysinit_svc = sysinit_target_s - epoch_advanced

    total_sysinit = nhlos + kernel + initramfs_s + sysinit_svc
    total_multiuser = nhlos + kernel + cc_multiuser_s

    multiuser_note = ""
    grand_total = nhlos + kernel + sat["userspace"]
    if grand_total - total_multiuser >= DELTA_MERGE_S:
        multiuser_note = f"≈{grand_total:.3f} s incl. graphical.target"

    run = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "build_path": build_path,
        "metrics": {
            "nhlos": {
                "value": f"{nhlos:.3f} s",
                "note": f"{sat['firmware']:.3f} s firmware + {sat['loader']:.3f} s loader",
            },
            "kernel": {"value": f"{kernel:.3f} s", "note": ""},
            "initramfs": {
                "value": f"{initramfs_s:.3f} s",
                "note": f"{init_exec:.3f} s → {epoch_advanced:.3f} s",
            },
            "sysinit_svc": {
                "value": f"{sysinit_svc:.3f} s",
                "note": f"{epoch_advanced:.3f} s → {sysinit_target_s:.3f} s",
                "hitters": sysinit_hitters,
            },
            "total_sysinit": {"value": f"{total_sysinit:.3f} s", "note": ""},
            "total_multiuser": {
                "value": f"{total_multiuser:.3f} s",
                "note": multiuser_note,
                "hitters": hitters,
            },
        },
        "critical_chain": chain,
        "source": (
            f"systemd-analyze time -> Startup finished in {sat['firmware']:.3f}s (firmware) + "
            f"{sat['loader']:.3f}s (loader) + {sat['kernel']:.3f}s (kernel) + "
            f"{sat['userspace']:.3f}s (userspace) = {sat['total']:.3f}s; "
            f"dmesg: Run /init @{init_exec:.3f}s, System time advanced to built-in epoch @{epoch_advanced:.3f}s, "
            f"systemd running @{milestones['systemd_running']:.3f}s; "
            f"journalctl: Reached target System Initialization @{sysinit_target_s:.3f}s; "
            f"initramfs = Run/init -> epoch-advance; sysinit_svc = epoch-advance -> sysinit.target; "
            f"sysinit hitters from `systemd-analyze critical-chain sysinit.target` "
            f"(+cost per unit); multi-user hitters from `systemd-analyze blame`"
        ),
    }
    return run


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def ensure_collect_script(ser):
    """Writes collect_boot_logs.sh to COLLECT_SCRIPT_REMOTE_PATH if it isn't
    already there, and chmod +x's it. Safe to call every boot -- self-healing
    if /data was ever wiped, but only actually writes once per device."""
    check = edl_flash.run_serial_command(
        ser, f"test -f {COLLECT_SCRIPT_REMOTE_PATH} && echo EXISTS || echo MISSING"
    )
    if check.strip() == "EXISTS":
        return
    print(f"Provisioning {COLLECT_SCRIPT_REMOTE_PATH} on device ...")
    write_cmd = (
        f"cat > {COLLECT_SCRIPT_REMOTE_PATH} << '{COLLECT_SCRIPT_HEREDOC_MARKER}'\n"
        f"{COLLECT_SCRIPT_CONTENT}\n"
        f"{COLLECT_SCRIPT_HEREDOC_MARKER}\n"
        f"chmod +x {COLLECT_SCRIPT_REMOTE_PATH}"
    )
    edl_flash.run_serial_command(ser, write_cmd, timeout_s=60)


def collect_boot_on_device(ser, boot_index: int):
    """Runs collect_boot_logs.sh (provisioning it first if needed), syncs,
    renames /data/Logs -> /data/Logs-<boot_index> so it survives the next
    boot's run, and syncs again."""
    ensure_collect_script(ser)

    print(f"Running collect_boot_logs.sh (boot {boot_index}) ...")
    edl_flash.run_serial_command(ser, f"sh {COLLECT_SCRIPT_REMOTE_PATH}", timeout_s=180)
    edl_flash.run_serial_command(ser, "sync")

    print(f"Renaming /data/Logs -> /data/Logs-{boot_index} ...")
    edl_flash.run_serial_command(ser, f"rm -rf /data/Logs-{boot_index}; mv /data/Logs /data/Logs-{boot_index}")
    edl_flash.run_serial_command(ser, "sync")


def enable_adb(ser):
    print("Enabling adb ...")
    edl_flash.run_serial_command(ser, "touch /etc/usb-debugging-enabled")
    edl_flash.run_serial_command(ser, "systemctl start android-tools-adbd")


def wait_for_adb_device(timeout_s: int = 60, poll_interval_s: float = 2.0):
    """Polls `adb devices` until a device shows up in the "device" (ready)
    state, as opposed to absent, "offline", or "unauthorized"."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        result = subprocess.run(["adb", "devices"], capture_output=True, text=True)
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) == 2 and parts[1] == "device":
                return
        time.sleep(poll_interval_s)
    raise RuntimeError(f"No adb device in 'device' state after {timeout_s}s.")


def _adb_root(retries: int = 3, retry_delay_s: float = 2.0):
    """Runs `adb root`, retrying on transient failure. Right after adbd is
    freshly enabled (systemctl start android-tools-adbd), the device can
    show up in `adb devices` as "device" a moment before adbd is actually
    ready to accept a root-escalation request -- so the first `adb root`
    issued right after wait_for_adb_device() returns can race and fail with
    exit code 1, even though the exact same command succeeds a couple
    seconds later (confirmed live: a manual retry worked immediately)."""
    last_result = None
    for attempt in range(1, retries + 1):
        result = subprocess.run(["adb", "root"], capture_output=True, text=True)
        if result.returncode == 0:
            return
        last_result = result
        if attempt < retries:
            print(f"'adb root' attempt {attempt}/{retries} failed; retrying in {retry_delay_s}s ...")
            time.sleep(retry_delay_s)
    raise RuntimeError(
        f"'adb root' failed after {retries} attempts (rc={last_result.returncode}): "
        f"{last_result.stderr.strip() or last_result.stdout.strip()}"
    )


def adb_pull_logs(local_dir: Path, num_boots: int):
    """Pulls /data/Logs-1 .. /data/Logs-<num_boots> into local_dir in a
    single `adb pull`, one Logs-<n> subfolder per boot. `adb root` first --
    /data/Logs-* is only readable as root on this device -- which restarts
    adbd, so we re-wait for the device to come back before pulling."""
    local_dir.mkdir(parents=True, exist_ok=True)

    print("Restarting adb as root ...")
    _adb_root()
    wait_for_adb_device()

    remote_dirs = [f"/data/Logs-{i}" for i in range(1, num_boots + 1)]
    print(f"Pulling {', '.join(remote_dirs)} -> {local_dir} ...")
    subprocess.run(["adb", "pull", *remote_dirs, str(local_dir)], check=True)


def _read_pulled_file(local_dir: Path, boot_index: int, filename: str) -> str:
    path = local_dir / f"Logs-{boot_index}" / filename
    if not path.exists():
        raise RuntimeError(f"Expected pulled log file not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def read_pulled_logs(local_dir: Path, boot_index: int) -> dict:
    """Reads the collect_boot_logs.sh outputs for one boot out of
    local_dir/Logs-<boot_index>/, already pulled from the device via
    adb_pull_logs. Maps to the same text arguments build_run() needs."""
    return {
        "sat_text": _read_pulled_file(local_dir, boot_index, "systemd_analyze.txt"),
        "cc_text": _read_pulled_file(local_dir, boot_index, "systemd-analyze_critical_chain.txt"),
        "cc_sysinit_text": _read_pulled_file(local_dir, boot_index, "systemd-analyze_critical_chain_sysinit.txt"),
        "blame_text": _read_pulled_file(local_dir, boot_index, "blame_systemd.txt"),
        "dmesg_text": _read_pulled_file(local_dir, boot_index, "dmesg.txt"),
        "journalctl_text": _read_pulled_file(local_dir, boot_index, "journalctl.log"),
    }


def reboot_and_relogin(com_port: str, timeout_s: int):
    """Power-cycles the device via the Alpaca TAC (off, pause, on) and logs
    back in over serial -- same TAC primitive edl_flash.py already uses for
    --recover. Avoids `systemctl reboot` over the console entirely: a
    hardware power cycle boots the device the same way every time, like the
    very first boot after a flash, rather than depending on in-OS reboot
    timing. Returns (ser, com_port_used); the COM port is re-probed from
    scratch if the original one doesn't come back (untested whether the
    debug-board UART bridge stays on the same port across a power cycle)."""
    print("Power-cycling device via Alpaca TAC (off, pause, on) ...")
    edl_flash.power_cycle_device()

    print(f"Waiting up to {timeout_s}s for device to come back online on {com_port} ...")
    try:
        ser, used_port = edl_flash.open_console(com_port, boot_timeout=timeout_s)
        return ser, used_port
    except RuntimeError as e:
        print(f"No login prompt on {com_port} ({e}); re-scanning all serial ports ...")
        ser, used_port = edl_flash.open_console(None, boot_timeout=timeout_s)
        return ser, used_port


def pull_and_record(target: str, build_path: str, num_boots: int = 3) -> Path:
    """Pulls the already-collected /data/Logs-1..N off the device (adb must
    already be enabled and the device already rebooted past the last boot's
    capture) and builds/records the JSON+HTML report. Split out from
    capture_and_record() so a run that reaches this point but then fails
    (e.g. a transient `adb root` error) can be resumed with
    `--resume-pull` instead of repeating the flash and all boots."""
    print("Waiting for adb device ...")
    wait_for_adb_device()

    local_dir = bootchart_report.boot_logs_run_dir(target, build_path)
    adb_pull_logs(local_dir, num_boots)

    boots = []
    for i in range(num_boots):
        texts = read_pulled_logs(local_dir, i + 1)
        boot = build_run(
            target, build_path,
            texts["sat_text"], texts["cc_text"], texts["cc_sysinit_text"],
            texts["blame_text"], texts["dmesg_text"], texts["journalctl_text"],
        )
        boots.append(boot)
        txt_path = bootchart_report.save_run_backup(target, boot)
        print(f"Backed up boot {i + 1} -> {txt_path}")

    entry = {
        "timestamp": boots[0]["timestamp"],
        "build_path": build_path,
        "boots": boots,
    }

    data_path, html_path = _data_html_paths(target)
    data = bootchart_report.load_data(data_path)
    if not data.get("runs"):
        data["device"] = target
    bootchart_report.add_run(data, entry)
    bootchart_report.save_data(data, data_path)
    bootchart_report.render(data_path, html_path)

    print(f"\nRecorded entry '{entry['timestamp']}' with {num_boots} boot(s) "
          f"({len(data['runs'])} total entries) -> {data_path}")
    print(f"Report regenerated -> {html_path}")

    return html_path


def capture_and_record(target: str, build_path: str,
                        com_port: str = None, boot_timeout: int = 480,
                        num_boots: int = 3) -> Path:
    print("Logging in over serial console" + (f" on {com_port}" if com_port else " (auto-detecting port)") + " ...")
    ser, com_port = edl_flash.open_console(com_port, boot_timeout=boot_timeout)
    print(f"Logged in via {com_port}")

    if target is None:
        target = edl_flash.detect_target_via_serial(ser)
        print(f"Detected target: {target} (via {com_port})")

    for i in range(num_boots):
        print(f"\n--- Boot {i + 1}/{num_boots} ---")
        collect_boot_on_device(ser, i + 1)
        if i < num_boots - 1:
            ser.close()
            ser, com_port = reboot_and_relogin(com_port, boot_timeout)

    enable_adb(ser)
    ser.close()

    return pull_and_record(target, build_path, num_boots)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--build-path", required=True, help=r'Nightly build path used for this boot, e.g. "\\swayam\...\performance"')
    parser.add_argument("--target", help="Target name, e.g. iq-9075-evk (auto-detected via serial console if omitted)")
    parser.add_argument("--com-port", help="Serial console COM port, used for login and all data collection (auto-detected by probing all ports if omitted)")
    parser.add_argument("--boot-timeout", type=int, default=480, help="Seconds to wait for a login prompt on first login, and after each reboot (default: 480)")
    parser.add_argument("--num-boots", type=int, default=3, help="Number of consecutive boots to capture per invocation (default: 3)")
    parser.add_argument("--resume-pull", action="store_true",
                         help="Skip flashing/login/boot-capture entirely: adb is already enabled and all "
                              "boots' /data/Logs-<n> are already on the device (e.g. a prior run reached "
                              "adb pull and failed partway) -- just pull logs and (re)build the report. "
                              "Requires --target (no serial console is opened to auto-detect it).")
    args = parser.parse_args()

    if args.resume_pull and not args.target:
        print("--resume-pull requires --target (no serial console is opened to auto-detect it).")
        sys.exit(1)

    try:
        if args.resume_pull:
            pull_and_record(args.target, args.build_path, args.num_boots)
        else:
            capture_and_record(args.target, args.build_path, args.com_port,
                                args.boot_timeout, args.num_boots)
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

