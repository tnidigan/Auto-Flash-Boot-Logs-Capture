#!/usr/bin/env python3
r"""
bootbench.py -- single-file merge of edl_flash.py + boot_capture.py +
bootchart_report.py.

Automates the flash -> boot -> capture -> report pipeline for a Qualcomm
target device, split into three composable stages you name explicitly on the
command line:

  flash     Find the latest nightly Yocto build, detect/confirm the target,
            enter EDL mode, and flash it via PCAT (waits for --yes or a
            manual y/N answer right before flashing).
  capture   Log in over the serial console and collect boot-time metrics
            (systemd-analyze, dmesg, journalctl, blame, critical-chain, etc.)
            over N consecutive boots, pull them via adb, and record them into
            the per-target JSON + regenerate the HTML report.
  report    Standalone JSON/HTML report maintenance (render the HTML from the
            JSON, or manually append a run) -- only does anything when run
            without 'capture' in the same command; capture already records
            and renders its own data as part of collecting it.

Pass any combination, in any order: `flash`, `flash capture`, `capture`,
`capture report`, or `all` (shorthand for `flash capture report`). See
`--help` for worked examples of each.

Run with the ARM64 Python launcher on this machine (`py -3`), or with a
regular Windows Python install (`python3`):
    py -3 bootbench.py flash [--target iq-9075-evk] [--com-port COM40] [--yes]
    python3 bootbench.py all --yes

Requires: pip install comtypes pyserial  (for the ARM64 interpreter, i.e.
`py -3 -m pip install comtypes pyserial`; for a regular Windows Python
install, `python3 -m pip install comtypes pyserial`)
Requires: adb on PATH, for the capture stage's post-boot log pull.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path, PureWindowsPath

EPILOG = r"""
examples:
  Flash only -- waits for a manual y/N confirmation right before flashing:
      py -3 bootbench.py flash

  Flash, then capture boot-time logs (skip the y/N prompt with --yes):
      py -3 bootbench.py flash capture --yes

  Flash, capture, and report -- the full pipeline in one command:
      py -3 bootbench.py all --yes
      py -3 bootbench.py flash capture report --yes

  Capture only, on a device that's already flashed with this build:
      py -3 bootbench.py capture --build-path "\\swayam\...\performance"

  Resume a capture that flashed/booted fine but failed during the
  adb-pull/report step (no reflash, no repeated boots):
      py -3 bootbench.py capture --build-path "\\swayam\...\performance" ^
          --target iq-9075-evk --resume-pull

  Recover a device stuck in EDL/Sahara mode:
      py -3 bootbench.py flash --recover

  Re-render the HTML report from the existing JSON (no device involved):
      py -3 bootbench.py report --report-cmd render --target iq-9075-evk

  Manually append a run JSON to the report (no device involved):
      py -3 bootbench.py report --report-cmd add-run --run-json new_run.json ^
          --target iq-9075-evk
"""

# =============================================================================
# Section 1 -- edl_flash: EDL flashing, Alpaca TAC power/EDL control, serial
# console login, and PCAT device discovery/flash.
# =============================================================================

YOCTO_SHARE = r"\\swayam\QLI_Builds\Yocto"
PERFORMANCE_SUBDIR = "performance"
BUILD_FOLDER_PREFIX = "qcom-multimedia-proprietary-image"
BUILD_NAME_RE = re.compile(r"_Nightly_Build_master_(\d+)$")

PCAT_EXE = r"C:\Program Files (x86)\Qualcomm\PCAT\bin\PCAT.exe"

SERIAL_BAUD = 115200
LOGIN_USER = "root"
LOGIN_PASSWORD = "oelinux123"

SERIAL_HOSTNAME_RE = re.compile(r"hostname=([^;\\]+)")
LOGIN_PROMPT_RE = re.compile(r"\S+\s+login:")
PASSWORD_PROMPT_RE = re.compile(r"[Pp]assword:\s*$", re.MULTILINE)
SHELL_PROMPT_RE = re.compile(r"[#\$]\s*$", re.MULTILINE)
DONE_MARKER_RE = re.compile(r"__DONE_(\d+)__")

# Async kernel log lines (driver warnings, timeouts, etc.) print to the serial
# console at arbitrary times, including interleaved mid-line with a login/
# password/shell prompt with no newline in between -- which breaks the `$`
# (end-of-line) anchor in the prompt regexes above. Strip them before prompt
# matching so trailing kernel noise doesn't hide a real prompt.
KERNEL_LOG_NOISE_RE = re.compile(r"\[\s*\d+\.\d+\]\s*[^\r\n]*")

# This device's shell wraps every command's output in an OSC shell-integration
# marker (ESC ] 3008;start=...;hostname=...;cwd=...ESC \) with no newline
# separating it from the actual output -- invisible when printed to a real
# terminal (an ANSI-aware terminal consumes it silently), but present verbatim
# in the raw bytes read here, so a `uname -n`/command-output capture that
# doesn't strip it gets that marker's own "hostname="/"cwd=" text spliced into
# the result (confirmed live: corrupted a build-dir Path built from `uname -n`
# output). Always stripped in run_serial_command, regardless of strip_noise --
# unlike kernel log noise, it's never legitimate command payload (e.g. dmesg
# would never emit this OSC format itself).
OSC_SEQUENCE_RE = re.compile(r"\x1b\]\d+;.*?\x1b\\", re.DOTALL)


def _strip_kernel_log_noise(text: str) -> str:
    return KERNEL_LOG_NOISE_RE.sub("", text)


def _strip_osc_sequences(text: str) -> str:
    return OSC_SEQUENCE_RE.sub("", text)

# Fallback default for open_console() when no explicit boot_timeout is given
# (e.g. --recover doesn't open a console at all). Callers that reboot the
# device first (pre-flash target detection, post-flash boot capture) pass
# the longer --boot-timeout explicitly instead of relying on this.
PRE_FLASH_LOGIN_TIMEOUT_S = 60

TAC_PORT_NAME = None  # auto-detected from the single connected TAC device if None
_tac_server_ref = None  # keeps the AlpacaTACServer COM object alive; see _open_tac()


def _run_powershell(command: str) -> str:
    """Runs a PowerShell command and returns stdout. Raises RuntimeError with
    PowerShell's own stderr on failure -- subprocess.run(check=True)'s
    CalledProcessError alone doesn't surface stderr, so a share access/
    permissions/connectivity error (e.g. \\\\swayam not reachable from this
    machine) shows up as a bare non-zero exit code with no explanation."""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", command],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"PowerShell command failed (exit {result.returncode}): {command}\n"
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def find_latest_build(share_root: str) -> Path:
    stdout = _run_powershell(
        f"Get-ChildItem -LiteralPath '{share_root}' -Directory "
        f"| Select-Object -ExpandProperty Name"
    )
    candidates = []
    for name in stdout.splitlines():
        name = name.strip()
        m = BUILD_NAME_RE.search(name)
        if m:
            candidates.append((int(m.group(1)), name))
    if not candidates:
        raise RuntimeError(f"No '_Nightly_Build_master_<N>' folders found under {share_root}")
    candidates.sort()
    _, latest_name = candidates[-1]
    return Path(share_root) / latest_name


def resolve_build_dir(latest_build: Path, target: str) -> Path:
    build_dir = latest_build / PERFORMANCE_SUBDIR / f"{BUILD_FOLDER_PREFIX}-{target}"
    stdout = _run_powershell(f"Test-Path -LiteralPath '{build_dir}\\rawprogram0.xml'")
    if stdout.strip() != "True":
        raise RuntimeError(
            f"Build folder does not look like a flat build (missing rawprogram0.xml): {build_dir}"
        )
    return build_dir


# ---------------------------------------------------------------------------
# Serial console -- login, target detection, and post-flash boot-time data
# collection (Section 3) all go over this. See open_console().
# ---------------------------------------------------------------------------

def _drain(ser, duration_s: float = 0.5) -> str:
    time.sleep(duration_s)
    data = ser.read(65536)
    return data.decode("utf-8", errors="replace")


def _probe_serial_port(port_name: str) -> str | None:
    """Returns the raw console text read from port_name, or None if the port
    couldn't be opened. Used to auto-detect which COM port is the target's own
    console -- other consoles on the board (e.g. a co-processor debug shell)
    also enumerate and may show a bare shell prompt, so a login prompt (which
    carries the target's own hostname) is required to be confident, and a bare
    shell prompt is only accepted as a fallback if no port shows a login prompt."""
    import serial

    try:
        with serial.Serial(port_name, SERIAL_BAUD, timeout=1) as ser:
            ser.reset_input_buffer()
            ser.write(b"\r\n")
            return _drain(ser, 1.0)
    except OSError:
        # Port busy (e.g. held open by another app or the TAC control channel) or
        # doesn't exist -- not a candidate.
        return None


def find_console_port(timeout_s: int = PRE_FLASH_LOGIN_TIMEOUT_S) -> str:
    """Repeatedly sweeps every enumerated COM port (each sweep costs ~1s/port,
    from _probe_serial_port's own read window) until one shows a live target
    console, preferring the strongest signal seen so far. A single sweep run
    right after a power-cycle can catch the real target console before it has
    booted far enough to print anything, while an unrelated already-logged-in
    port (e.g. a co-processor debug shell left open on this machine) answers
    immediately -- so a one-shot scan would lock onto that wrong port forever.
    Looping gives the real console the full timeout to appear and always
    prefers a hostname/login match over a bare shell-prompt fallback,
    regardless of which one is seen first."""
    from serial.tools import list_ports

    deadline = time.time() + timeout_s
    shell_prompt_fallback = None
    while True:
        candidates = [p.device for p in list_ports.comports()]
        if not candidates:
            raise RuntimeError("No serial (COM) ports found on this machine.")

        login_fallback = None
        for port_name in candidates:
            text = _probe_serial_port(port_name)
            if text is None:
                continue
            # OSC hostname marker is the strongest signal -- it names the target's
            # own hostname directly, unlike a bare login/shell prompt which other
            # consoles on the board (e.g. a co-processor debug shell) can also show.
            if SERIAL_HOSTNAME_RE.search(text):
                return port_name
            clean = _strip_kernel_log_noise(text)
            if login_fallback is None and LOGIN_PROMPT_RE.search(clean):
                login_fallback = port_name
            if shell_prompt_fallback is None and SHELL_PROMPT_RE.search(clean.strip()):
                shell_prompt_fallback = port_name

        if login_fallback:
            return login_fallback

        if time.time() >= deadline:
            if shell_prompt_fallback:
                return shell_prompt_fallback
            raise RuntimeError(
                f"No live console found on any of: {', '.join(candidates)} within {timeout_s}s. "
                "Pass --com-port explicitly, or check the device is powered on."
            )


def wait_for_login_prompt(ser, timeout_s: int) -> str:
    """Poll the console until a login: prompt appears, nudging with a
    newline periodically (first boot after flash can take minutes)."""
    deadline = time.time() + timeout_s
    buf = ""
    last_nudge = 0.0
    while time.time() < deadline:
        buf += _drain(ser, 1.0)
        clean = _strip_kernel_log_noise(buf)
        if LOGIN_PROMPT_RE.search(clean) or SHELL_PROMPT_RE.search(clean.strip()):
            return buf
        if time.time() - last_nudge > 5.0:
            ser.write(b"\r\n")
            last_nudge = time.time()
        buf = buf[-4096:]
    raise RuntimeError(
        f"No login/shell prompt seen on serial console within {timeout_s}s. "
        "Device may still be booting or stuck -- check the console manually."
    )


def login(ser, timeout_s: int):
    print(f"Waiting up to {timeout_s}s for a login/shell prompt on serial console...")
    buf = wait_for_login_prompt(ser, timeout_s)
    clean_buf = _strip_kernel_log_noise(buf)

    if SHELL_PROMPT_RE.search(clean_buf.strip()) and not LOGIN_PROMPT_RE.search(clean_buf):
        print("Already at a shell prompt (no login required).")
        return

    print(f"Login prompt seen, logging in as {LOGIN_USER}...")
    ser.write(f"{LOGIN_USER}\r\n".encode())
    time.sleep(1.0)
    resp = _drain(ser, 2.0)

    if PASSWORD_PROMPT_RE.search(_strip_kernel_log_noise(resp)):
        ser.write(f"{LOGIN_PASSWORD}\r\n".encode())
        time.sleep(1.0)
        resp = _drain(ser, 2.0)

    deadline = time.time() + 15
    while not SHELL_PROMPT_RE.search(_strip_kernel_log_noise(resp).strip()) and time.time() < deadline:
        resp += _drain(ser, 1.0)

    if not SHELL_PROMPT_RE.search(_strip_kernel_log_noise(resp).strip()):
        raise RuntimeError(f"Login did not reach a shell prompt. Last console output:\n{resp}")
    print("Logged in.")


def run_serial_command(ser, cmd: str, timeout_s: int = 30, strip_noise: bool = True) -> str:
    """Runs cmd over the serial console, using an echoed exit-code marker to
    detect completion (there's no pexpect-style framework on a raw line).
    strip_noise=False must be used for commands whose own output legitimately
    uses the same `[ddd.ddd] ...` bracket format that _strip_kernel_log_noise
    strips (e.g. `dmesg`) -- stripping would delete the actual payload, not
    just async interleaved noise."""
    marker_cmd = f"{cmd}; echo __DONE_$?__"
    # The console echoes back exactly what was written, but the terminal's own
    # line-wrapping can insert a \r\n at an arbitrary point *inside* that echo
    # -- confirmed live: `echo __DONE_$?__` echoed back as "echo _" + newline +
    # "_DONE_$?__". A single-line substring match (the old approach) silently
    # fails to recognize a wrapped echo, leaving it stuck to the front of the
    # real command output. Tolerate an optional wrap between every character.
    echo_re = re.compile(r"\r?\n?".join(re.escape(c) for c in marker_cmd))
    ser.reset_input_buffer()
    ser.write((marker_cmd + "\r\n").encode())

    deadline = time.time() + timeout_s
    buf = ""
    while time.time() < deadline:
        buf += _drain(ser, 0.5)
        m = DONE_MARKER_RE.search(buf)
        if m:
            rc = int(m.group(1))
            output = _strip_osc_sequences(buf[:m.start()])
            # Drop the echoed command text, wherever it appears in the output
            # (kernel-log noise may have interleaved before it even reached
            # the console) -- echo_re tolerates the mid-echo line wrap.
            em = echo_re.search(output)
            if em:
                output = output[em.end():]
            lines = output.splitlines()
            text = "\n".join(lines).strip()
            if strip_noise:
                text = _strip_kernel_log_noise(text).strip()
            if rc != 0:
                raise RuntimeError(f"Command failed (rc={rc}): {cmd!r}\nOutput:\n{text}")
            return text
    raise RuntimeError(f"Timed out waiting for command to complete: {cmd!r}\nPartial output:\n{buf}")


def open_console(com_port: str = None, boot_timeout: int = PRE_FLASH_LOGIN_TIMEOUT_S):
    """Opens the serial console, waits for a login/shell prompt, and logs in.
    Returns (ser, com_port_used); caller is responsible for closing ser."""
    import serial

    if com_port is None:
        com_port = find_console_port(timeout_s=boot_timeout)

    ser = serial.Serial(com_port, SERIAL_BAUD, timeout=1)
    login(ser, boot_timeout)
    return ser, com_port


def detect_target_via_serial(ser) -> str:
    target = run_serial_command(ser, "uname -n")
    if not target:
        raise RuntimeError("Could not determine target name from 'uname -n' (empty output)")
    return target


# ---------------------------------------------------------------------------
# Alpaca TAC (EDL entry / power control)
# ---------------------------------------------------------------------------

def _open_tac(retries: int = 3, retry_delay_s: float = 2.0):
    import comtypes.client as cc

    # Keep a reference to the top-level AlpacaTACServer COM object at module scope.
    # Create_TAC_Server() returns a *child* COM object (ITACServer); if nothing
    # keeps the parent alive, Python garbage-collects it as soon as this function
    # returns, which disconnects the child's interface and makes every subsequent
    # call (e.g. BootToEDLButton, Close) fail with "The object invoked has
    # disconnected from its clients."
    global _tac_server_ref

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            _tac_server_ref = cc.CreateObject("TACCOM.AlpacaTACServer")
            tac = _tac_server_ref.Create_TAC_Server()
            count = tac.Get_Device_Count()
            if count == 0:
                raise RuntimeError("No Alpaca TAC devices found. Is the debug board connected?")

            port = TAC_PORT_NAME
            if port is None:
                if count > 1:
                    raise RuntimeError(
                        f"Multiple TAC devices found ({count}); pass --tac-port to disambiguate."
                    )
                port = tac.Get_PortName(0)

            if not tac.OpenByName(port):
                raise RuntimeError(f"Failed to open TAC device on port {port}")

            print(f"Opened TAC device: {tac.Get_Name()} ({tac.Get_HardwareVersion()}) on port {port}")
            return tac
        except Exception as e:
            last_error = e
            _tac_server_ref = None
            if attempt < retries:
                print(f"TAC open attempt {attempt}/{retries} failed ({e!r}); retrying in {retry_delay_s}s...")
                time.sleep(retry_delay_s)

    raise RuntimeError(f"Could not open Alpaca TAC server after {retries} attempts: {last_error!r}")


def enter_edl_mode():
    tac = _open_tac()
    try:
        print("Triggering BootToEDL...")
        tac.BootToEDLButton()
    finally:
        tac.Close()


def power_cycle_device():
    """Recover a device stuck in EDL/Sahara (or abort a flash) by power-cycling it
    back to normal boot. The Alpaca TAC has no single 'reset' command -- only
    discrete PowerOffButton / PowerOnButton -- so recovery is off, pause, on."""
    tac = _open_tac()
    try:
        print("Powering device off...")
        tac.PowerOffButton()
        time.sleep(2)
        print("Powering device on...")
        tac.PowerOnButton()
    finally:
        tac.Close()


# ---------------------------------------------------------------------------
# PCAT
# ---------------------------------------------------------------------------

def pcat_list_devices() -> list:
    with tempfile.TemporaryDirectory() as tmp:
        out_file = str(Path(tmp) / "devices.json")
        result = subprocess.run(
            [PCAT_EXE, "-DEVICES", "-JSON", "TRUE", "-OUT", out_file],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"PCAT -DEVICES failed (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        data = json.loads(Path(out_file).read_text(encoding="utf-8-sig"))
    return data


def wait_for_edl_device(timeout_s: int = 90, poll_interval_s: int = 2) -> dict:
    """Polls PCAT -DEVICES until an EDL-capable device shows up. Right after
    BootToEDL, the device is still re-enumerating over USB, and PCAT -DEVICES
    can transiently fail outright (non-zero exit, not just an empty device
    list) during that window -- tolerate that like any other "not there yet"
    result instead of letting it abort the whole poll loop. A single PCAT
    -DEVICES call has been observed taking ~12s on its own (it does its own
    USB device-manager scan), so the default timeout allows for several
    genuine retries rather than 1-2."""
    deadline = time.time() + timeout_s
    attempt = 0
    last_devices = []
    last_error = None
    while time.time() < deadline:
        attempt += 1
        try:
            devices = pcat_list_devices()
        except RuntimeError as e:
            last_error = e
            print(f"  PCAT -DEVICES attempt {attempt} failed ({e}); retrying ...")
            time.sleep(poll_interval_s)
            continue
        last_devices = devices
        candidates = [
            d for d in devices
            if d.get("device_state", "").upper() not in ("NORMAL",) or d.get("device_type")
        ]
        if candidates:
            return candidates[0]
        print(f"  PCAT -DEVICES attempt {attempt}: no EDL device yet ({time.time() - (deadline - timeout_s):.0f}s elapsed) ...")
        time.sleep(poll_interval_s)
    raise RuntimeError(
        f"No EDL-capable device found via PCAT -DEVICES after {timeout_s}s. "
        f"Last seen: {last_devices}"
        + (f"; last error: {last_error}" if last_error else "")
    )


def pcat_device_id(device: dict) -> str:
    """PCAT's -DEVICES JSON sometimes reports id as the literal string "NA"
    (seen in EDL state on this board) instead of a real identifier -- fall
    back to serial_number, which PCAT does accept as -DEVICE, in that case."""
    device_id = device.get("id")
    if device_id and device_id.upper() != "NA":
        return device_id
    serial_number = device.get("serial_number")
    if serial_number:
        return serial_number
    raise RuntimeError(f"No usable device id or serial_number in PCAT device entry: {device}")


def run_pcat_flash(device_id: str, build_dir: Path):
    cmd = [
        PCAT_EXE, "-PLUGIN", "SD",
        "-DEVICE", device_id,
        "-BUILD", str(build_dir),
        "-MEMORYTYPE", "UFS",
        "-SLOT", "0",
    ]
    print("\nRunning:", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    print("(Ctrl+C aborts the flash and power-cycles the device back to normal boot)")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for line in proc.stdout:
            print(line, end="")
        proc.wait()
    except KeyboardInterrupt:
        print("\nAborting flash: killing PCAT...")
        proc.kill()
        proc.wait()
        print("PCAT process killed. Power-cycling device via TAC...")
        try:
            power_cycle_device()
        except Exception as e:
            raise RuntimeError(
                f"Flash aborted (PCAT killed), but power-cycling the device via TAC also "
                f"failed: {e!r}. The device may still be in EDL/Sahara mode -- run "
                f"'py -3 bootbench.py flash --recover' to retry, or use the TAC app / physical "
                f"reset directly."
            )
        raise RuntimeError("Flash aborted by user (Ctrl+C); device power-cycled back to normal boot.")

    if proc.returncode != 0:
        raise RuntimeError(f"PCAT flash failed with exit code {proc.returncode}")


# =============================================================================
# Section 2 -- bootchart_report: per-device JSON history + regenerated HTML
# report.
#
# bootchart-data-<slug>.json is the only file ever hand-edited (by appending a
# run). bootchart-overview-<slug>.html is ALWAYS fully derived from the JSON
# by this script -- never hand-authored. Re-running `render` regenerates it
# byte-for-byte from whatever is currently in the JSON.
#
# Run object schema:
# {
#   "timestamp": "YYYY-MM-DD HH:MM",
#   "metrics": {
#     "nhlos":           {"value": "5.035 s", "note": "4.318 s firmware + 0.717 s loader"},
#     "kernel":          {"value": "1.573 s", "note": ""},
#     "initramfs":       {"value": "0.458 s", "note": "0.092 s -> 0.550 s"},
#     "sysinit_svc":     {"value": "2.734 s", "note": "0.550 s -> 3.284 s",
#                          "hitters": [{"name": "systemd-tmpfiles-setup.service", "time": "0.057 s"}, ...]},
#     "total_sysinit":   {"value": "9.342 s", "note": ""},
#     "total_multiuser": {"value": "19.005 s", "note": "~19.79 s incl. graphical.target",
#                          "hitters": [{"name": "android-tools-adbd.service", "time": "10.416 s"}, ...]}
#   },
#   "critical_chain": ["docker.service", "network-online.target", "..."]   // optional, kept for backup/.txt context
# }
# A metric row's `hitters` list (where present) IS rank -- position 0 is #1, NOT a fixed identity
# across runs. NHLOS/kernel/initramfs carry no `hitters` key: there's no per-component timing source
# for them today (no initcall_debug on this build, no bootloader trace).
#
# Older entries from before hitters were merged into the metrics table may still carry a flat
# top-level "hitters" list instead of a nested one. Rendering falls back to treating that as
# total_multiuser's hitters, so no data migration is needed.
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
BOOT_CHARTS_DIR = BASE_DIR / "Boot-Charts"  # flat: every device's bootchart-data/-overview files land directly here, no per-device subdir
DEFAULT_DATA = BOOT_CHARTS_DIR / "bootchart-data.json"
DEFAULT_HTML = BOOT_CHARTS_DIR / "bootchart-overview.html"
BOOT_LOGS_DIR = BASE_DIR / "Boot-Logs"  # nested Boot-Logs/<target>/<build-folder>/<timestamp>.txt

MAX_RUNS = 30  # bootchart-data.json / .html keep only the most recent N runs;
               # full history lives on in Boot-Logs/*.txt regardless of this cap.

METRIC_ROWS = [
    ("nhlos", "NHLOS (PBL/SBL/XBL firmware + ABL loader)", False),
    ("kernel", "Kernel time", False),
    ("initramfs", "Initramfs (<code>Run /init</code> &rarr; epoch advance)", False),
    ("sysinit_svc", "systemd &rarr; sysinit.target", False),
    ("total_sysinit", "Total time till sysinit.target", True),
    ("total_multiuser", "Total time till multi-user.target", True),
]

# Notes that are identical on every boot (constant text, not derived per-run)
# are shown once under the row's own label instead of being repeated in every
# cell -- see render_metrics_table / _hitters_html.
ROW_CAPTIONS = {
    "initramfs": "dmesg: Run /init → System time advanced to built-in epoch",
    "sysinit_svc": "dmesg epoch advance → journalctl: Reached target System Initialization",
}

DELTA_THRESHOLD = 0.5
TIME_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def parse_seconds(value):
    if not value:
        return None
    m = TIME_RE.search(value)
    return float(m.group()) if m else None


def format_delta(prev_value, cur_value):
    """Return (label, css_class) for the delta annotation, or ("", "") if not computable."""
    p, c = parse_seconds(prev_value), parse_seconds(cur_value)
    if p is None or c is None:
        return "", ""
    delta = c - p
    if delta >= DELTA_THRESHOLD:
        cls = "regressed"
    elif delta <= -DELTA_THRESHOLD:
        cls = "improved"
    else:
        cls = "unchanged"
    sign = "+" if delta >= 0 else "−"
    return f"({sign}{abs(delta):.3f} s)", cls


def esc(value):
    return html.escape(str(value), quote=False)


def load_data(data_path):
    if not data_path.exists():
        return {"device": "unknown", "runs": []}
    with open(data_path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data, data_path):
    data_path.parent.mkdir(parents=True, exist_ok=True)
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _validate_boot(boot):
    required = {"timestamp", "metrics"}
    missing = required - boot.keys()
    if missing:
        raise ValueError(f"boot object missing required keys: {missing}")
    missing_metrics = {k for k, _, _ in METRIC_ROWS} - boot["metrics"].keys()
    if missing_metrics:
        raise ValueError(f"boot.metrics missing required keys: {missing_metrics}")


def add_run(data, run):
    if "boots" in run:
        required_top = {"timestamp", "build_path", "boots"}
        missing = required_top - run.keys()
        if missing:
            raise ValueError(f"multi-boot entry missing required keys: {missing}")
        if not run["boots"]:
            raise ValueError("multi-boot entry has an empty 'boots' list")
        for boot in run["boots"]:
            _validate_boot(boot)
    else:
        _validate_boot(run)
    runs = data.setdefault("runs", [])
    runs.append(run)
    del runs[:-MAX_RUNS]  # trim from the front; full history is preserved in Boot-Logs/*.txt
    return data


def _flatten(runs):
    """Expands entries into a flat list of per-boot records, normalizing
    legacy flat (single-boot) entries to a 1-item boots list. Each record:
    {"boot": <boot dict>, "idx": <position within its entry>, "n": <boots in that entry>}.
    All rendering iterates this flat list so delta/rank/footer logic doesn't
    need to special-case entry boundaries -- boot 0 of entry K just diffs
    against the last boot of entry K-1, like any two consecutive boots."""
    flat = []
    for run in runs:
        boots = run.get("boots") or [run]
        n = len(boots)
        for idx, boot in enumerate(boots):
            flat.append({"boot": boot, "idx": idx, "n": n})
    return flat


def _metric_hitters(boot, key):
    """Returns the hitters list for a given metric row, or [] if none.
    Legacy entries (captured before hitters were merged into the metrics
    table) carry a flat top-level "hitters" list -- treated as
    total_multiuser's hitters, since that's what it always meant before."""
    m = boot["metrics"].get(key, {})
    if "hitters" in m:
        return m["hitters"]
    if key == "total_multiuser":
        return boot.get("hitters") or []
    return []


def _format_run_txt(target, run):
    """Human-readable dump of a single run, for the Boot-Logs/ backup -- the
    full-history record that survives bootchart-data.json's MAX_RUNS trim."""
    lines = [
        f"Device: {target}",
        f"Timestamp: {run['timestamp']}",
        f"Build path: {run.get('build_path', '')}",
        "",
        "Metrics:",
    ]
    for key, label, _ in METRIC_ROWS:
        m = run["metrics"].get(key, {})
        note = f"  ({m['note']})" if m.get("note") else ""
        lines.append(f"  {label}: {m.get('value', '')}{note}")
        for i, h in enumerate(_metric_hitters(run, key)):
            h_note = f"  ({h['note']})" if h.get("note") else ""
            lines.append(f"    #{i + 1}. {h['name']}: {h['time']}{h_note}")

    chain = run.get("critical_chain")
    if chain:
        lines.append("")
        lines.append("Critical chain: " + " -> ".join(chain))

    if run.get("source"):
        lines.append("")
        lines.append("Source: " + run["source"])

    return "\n".join(lines) + "\n"


def boot_logs_run_dir(target, build_path, boot_logs_dir=BOOT_LOGS_DIR):
    """Returns the nested Boot-Logs/<target>/<build-folder-name> directory for
    a given target + build_path, shared by save_run_backup's per-boot .txt
    backups and boot-capture's pulled raw-log folders (Logs-1, Logs-2, ...) so
    both land side-by-side in the same directory."""
    return boot_logs_dir / target / _build_folder_name(build_path) if build_path else boot_logs_dir


def save_run_backup(target, run, boot_logs_dir=BOOT_LOGS_DIR):
    """Writes a permanent per-run backup to
    Boot-Logs/<target>/<build-folder-name>/<timestamp>.txt, since
    bootchart-data.json only retains the most recent MAX_RUNS entries. Adds a
    numeric suffix if a backup for the same minute-granularity timestamp
    already exists (e.g. consecutive boots in a multi-boot entry can finish
    within the same minute), so no boot's backup is silently overwritten.
    Pre-existing flat backups directly under boot_logs_dir (from before this
    nested layout) are left in place -- only new backups go into the nested
    <target>/<build-folder> path."""
    build_path = run.get("build_path", "")
    run_dir = boot_logs_run_dir(target, build_path, boot_logs_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = run["timestamp"].replace(":", "-")
    txt_path = run_dir / f"{safe_ts}.txt"
    n = 2
    while txt_path.exists():
        txt_path = run_dir / f"{safe_ts}_{n}.txt"
        n += 1
    txt_path.write_text(_format_run_txt(target, run), encoding="utf-8")
    return txt_path


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
  :root {
    --bg: #ffffff;
    --panel: #f7f8fa;
    --border: #000000;
    --text: #1c1f26;
    --muted: #6b7280;
    --accent: #2563eb;
    --good: #1a9c63;
    --warn: #b8860b;
    --bad: #d1403f;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5;
    overflow-x: auto;
  }
  .container { padding: 40px 20px; width: max-content; min-width: 100%; }
  h1 { font-size: 1.5rem; margin-bottom: 4px; }
  .subtitle { color: var(--muted); font-size: 0.95rem; margin-bottom: 28px; max-width: 1100px; }
  .subtitle code { background: var(--panel); padding: 2px 6px; border-radius: 4px; border: 1px solid var(--border); }
  h2 { font-size: 1.1rem; margin-top: 36px; margin-bottom: 12px; border-left: 3px solid var(--accent); padding-left: 10px; }
  table { border-collapse: collapse; background: var(--bg); border: 2px solid var(--border); }
  th, td { text-align: left; padding: 12px 16px; border: 1px solid var(--border); white-space: nowrap; }
  th { background: var(--panel); color: var(--muted); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.04em; }
  th.run-col { color: var(--accent); text-transform: none; letter-spacing: normal; font-size: 0.85rem; }
  th.run-col.latest { color: var(--text); background: #e8eefc; }
  tr.total td { font-weight: 600; color: var(--accent); background: #eef2fd; }
  td.timing { font-family: "SF Mono", Consolas, monospace; font-size: 0.95rem; }
  td.timing .note { display: block; color: var(--muted); font-size: 0.78rem; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; white-space: normal; margin-top: 2px; }
  td.timing .delta { font-size: 0.78rem; margin-left: 6px; font-family: "SF Mono", Consolas, monospace; }
  .delta.unchanged { color: var(--muted); }
  .delta.improved { color: var(--good); }
  .delta.regressed { color: var(--bad); }
  td.buildpath { font-family: "SF Mono", Consolas, monospace; font-size: 0.78rem; color: var(--muted); white-space: normal; word-break: break-all; text-align: center; }
  th.entry-start, td.entry-start { border-left: 2px solid var(--accent); }
  td:first-child .row-caption { display: block; font-weight: 400; font-size: 0.75rem; color: var(--muted); margin-top: 2px; white-space: normal; }
  td.timing ol.hitters { list-style: none; margin: 6px 0 0; padding: 0; white-space: normal; }
  td.timing ol.hitters li { font-size: 0.78rem; font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: var(--muted); margin-top: 2px; }
  td.timing ol.hitters li .hitter-name { font-family: "SF Mono", Consolas, monospace; }
"""


def _boot_header_label(rec):
    boot = rec["boot"]
    if rec["n"] == 1:
        return boot["timestamp"]
    time_part = boot["timestamp"].split(" ")[-1]
    return f"Boot {rec['idx'] + 1} · {time_part}"


def _header_cells(flat):
    return "".join(
        f'<th class="run-col{" latest" if i == len(flat) - 1 else ""}'
        f'{" entry-start" if rec["idx"] == 0 and i > 0 else ""}">'
        f'{esc(_boot_header_label(rec))}</th>'
        for i, rec in enumerate(flat)
    )


def _strip_performance_suffix(build_path):
    """Boot analysis is only ever run against a build's '...\\performance'
    subdirectory, so that segment is implied and dropped from display/paths."""
    p = PureWindowsPath(build_path)
    if p.name.lower() == "performance":
        p = p.parent
    return str(p)


def _build_folder_name(build_path):
    return PureWindowsPath(_strip_performance_suffix(build_path)).name or "unknown-build"


def render_build_path_row(runs):
    if not any(r.get("build_path") for r in runs):
        return ""
    cells = "".join(
        f'<td class="buildpath" colspan="{len(run.get("boots") or [run])}">{esc(_strip_performance_suffix(run.get("build_path", "")))}</td>'
        for run in runs
    )
    return f"<tr><td>Nightly build path</td>{cells}</tr>"


def _hitters_html(hitters):
    """Ranked sub-list rendered inside a metric's value cell. Each hitter's
    "note" (e.g. "largest single cost", "critical path"/"off critical path")
    is precomputed at capture time in build_run, not here."""
    if not hitters:
        return ""
    items = []
    for i, h in enumerate(hitters):
        note = h.get("note", "")
        note_html = f" &middot; {esc(note)}" if note else ""
        items.append(
            f'<li class="rank-{i + 1}">#{i + 1} <span class="hitter-name">{esc(h["name"])}</span> '
            f'&mdash; {esc(h["time"])}{note_html}</li>'
        )
    return f'<ol class="hitters">{"".join(items)}</ol>'


def render_metrics_table(runs):
    flat = _flatten(runs)
    header_cells = _header_cells(flat)
    rows_html = [render_build_path_row(runs)]
    for key, label, is_total in METRIC_ROWS:
        caption = ROW_CAPTIONS.get(key)
        label_html = f'{label}<span class="row-caption">{esc(caption)}</span>' if caption else label
        cells = []
        for i, rec in enumerate(flat):
            boot = rec["boot"]
            m = boot["metrics"][key]
            value, note = m.get("value", ""), m.get("note", "")
            delta_html = ""
            if i > 0:
                prev_value = flat[i - 1]["boot"]["metrics"][key].get("value", "")
                delta_label, delta_cls = format_delta(prev_value, value)
                if delta_label:
                    delta_html = f'<span class="delta {delta_cls}">{delta_label}</span>'
            note_html = f'<span class="note">{esc(note)}</span>' if note and note != caption else ""
            hitters_html = _hitters_html(_metric_hitters(boot, key))
            cls = "timing" + (" entry-start" if rec["idx"] == 0 and i > 0 else "")
            cells.append(f'<td class="{cls}">{esc(value)}{delta_html}{note_html}{hitters_html}</td>')
        row_class = ' class="total"' if is_total else ""
        rows_html.append(f"<tr{row_class}><td>{label_html}</td>{''.join(cells)}</tr>")
    return f"""
  <table>
    <thead><tr><th>Stage</th>{header_cells}</tr></thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>"""


def render_html(data):
    runs = data.get("runs", [])
    device = esc(data.get("device", "unknown device"))
    if not runs:
        body = "<p>No runs collected yet. Run <code>bootbench.py report add-run &lt;run.json&gt;</code> to add the first one.</p>"
        metrics_html = ""
    else:
        metrics_html = render_metrics_table(runs)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Boot Time Analysis &mdash; {device}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

  <h1>Boot Time Analysis</h1>
  <div class="subtitle">Device <code>{device}</code> &mdash; history of collected boot runs. Data source: <code>bootchart-data.json</code>, generated by <code>bootbench.py</code>. <b>Boot analysis is performed only on performance builds.</b> High hitters for each stage (where measurable) are ranked inline beneath that stage's timing.</div>

  <h2>Boot Time Summary</h2>
  {metrics_html}

</div>
</body>
</html>
"""


def render(data_path=DEFAULT_DATA, html_path=DEFAULT_HTML):
    data = load_data(data_path)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(data), encoding="utf-8")
    print(f"Rendered {html_path} from {data_path} ({len(data.get('runs', []))} run(s)).")


# =============================================================================
# Section 3 -- boot_capture: collects boot-time metrics from a target device
# over N consecutive boots and records them via Section 2's JSON+HTML report.
#
# Per boot, a device-side script (collect_boot_logs.sh, provisioned to
# /data/collect_boot_logs.sh on first use) dumps systemd-analyze/blame/dmesg/etc.
# into /data/Logs, which is then renamed to /data/Logs-<n> so each boot's dump
# survives the next boot's run. Login, script provisioning/execution, and the
# per-boot rename all happen over the serial console (open_console /
# run_serial_command from Section 1) -- no adb/USB involved for that part. Only
# after all boots are done is adb enabled (touch /etc/usb-debugging-enabled;
# systemctl start android-tools-adbd) for a single `adb pull` of every Logs-<n>
# folder into Boot-Logs/<target>/<build-folder>/, which is then parsed to build
# each boot's metrics.
#
# Normally invoked automatically by the `flash` subcommand right after a
# successful flash. Can also be run standalone (`capture` subcommand) to
# (re-)capture without reflashing.
# =============================================================================

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
    base = BOOT_CHARTS_DIR
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
    check = run_serial_command(
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
    run_serial_command(ser, write_cmd, timeout_s=60)


def collect_boot_on_device(ser, boot_index: int):
    """Runs collect_boot_logs.sh (provisioning it first if needed), syncs,
    renames /data/Logs -> /data/Logs-<boot_index> so it survives the next
    boot's run, and syncs again."""
    ensure_collect_script(ser)

    print(f"Running collect_boot_logs.sh (boot {boot_index}) ...")
    run_serial_command(ser, f"sh {COLLECT_SCRIPT_REMOTE_PATH}", timeout_s=180)
    run_serial_command(ser, "sync")

    print(f"Renaming /data/Logs -> /data/Logs-{boot_index} ...")
    run_serial_command(ser, f"rm -rf /data/Logs-{boot_index}; mv /data/Logs /data/Logs-{boot_index}")
    run_serial_command(ser, "sync")


def enable_adb(ser):
    print("Enabling adb ...")
    run_serial_command(ser, "touch /etc/usb-debugging-enabled")
    run_serial_command(ser, "systemctl start android-tools-adbd")


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
    back in over serial -- same TAC primitive used for --recover. Avoids
    `systemctl reboot` over the console entirely: a hardware power cycle
    boots the device the same way every time, like the very first boot after
    a flash, rather than depending on in-OS reboot timing. Returns (ser,
    com_port_used); the COM port is re-probed from scratch if the original
    one doesn't come back (untested whether the debug-board UART bridge
    stays on the same port across a power cycle)."""
    print("Power-cycling device via Alpaca TAC (off, pause, on) ...")
    power_cycle_device()

    print(f"Waiting up to {timeout_s}s for device to come back online on {com_port} ...")
    try:
        ser, used_port = open_console(com_port, boot_timeout=timeout_s)
        return ser, used_port
    except RuntimeError as e:
        print(f"No login prompt on {com_port} ({e}); re-scanning all serial ports ...")
        ser, used_port = open_console(None, boot_timeout=timeout_s)
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

    local_dir = boot_logs_run_dir(target, build_path)
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
        txt_path = save_run_backup(target, boot)
        print(f"Backed up boot {i + 1} -> {txt_path}")

    entry = {
        "timestamp": boots[0]["timestamp"],
        "build_path": build_path,
        "boots": boots,
    }

    data_path, html_path = _data_html_paths(target)
    data = load_data(data_path)
    if not data.get("runs"):
        data["device"] = target
    add_run(data, entry)
    save_data(data, data_path)
    render(data_path, html_path)

    print(f"\nRecorded entry '{entry['timestamp']}' with {num_boots} boot(s) "
          f"({len(data['runs'])} total entries) -> {data_path}")
    print(f"Report regenerated -> {html_path}")

    return html_path


def capture_and_record(target: str, build_path: str,
                        com_port: str = None, boot_timeout: int = 480,
                        num_boots: int = 3) -> Path:
    print("Logging in over serial console" + (f" on {com_port}" if com_port else " (auto-detecting port)") + " ...")
    ser, com_port = open_console(com_port, boot_timeout=boot_timeout)
    print(f"Logged in via {com_port}")

    if target is None:
        target = detect_target_via_serial(ser)
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


# =============================================================================
# CLI -- stage words (flash / capture / report / all), combinable in one
# invocation. Stages always run in pipeline order (flash -> capture ->
# report) regardless of the order they're typed in. When flash runs in the
# same invocation as capture, capture reuses the target/build-path/com-port
# flash just resolved instead of re-detecting them. capture already writes
# and renders the JSON/HTML report as part of pulling logs, so a standalone
# 'report' stage is only meaningful when 'capture' is NOT also requested.
# =============================================================================

STAGE_ORDER = ["flash", "capture", "report"]


def parse_stages(raw_stages):
    """Expands 'all' to flash+capture+report, dedupes, and returns stages in
    fixed pipeline order regardless of how they were typed on the CLI."""
    requested = set()
    for s in raw_stages:
        requested.update(STAGE_ORDER if s == "all" else [s])
    return [s for s in STAGE_ORDER if s in requested]


def cmd_flash(args):
    """Runs build-discovery -> EDL entry -> confirm -> PCAT flash. Returns a
    dict of {target, build_path, com_port} for capture to reuse when it runs
    in the same invocation, or None if the pipeline should stop here
    (--recover, --dry-run, or the user declined the confirmation prompt)."""
    global TAC_PORT_NAME
    TAC_PORT_NAME = args.tac_port

    if args.recover:
        print("Recovering device: power-cycling via TAC...")
        power_cycle_device()
        print("Done. Device should re-boot normally.")
        return None

    print(f"Finding latest nightly build under {YOCTO_SHARE} ...")
    latest_build = find_latest_build(YOCTO_SHARE)
    print(f"Latest build: {latest_build.name}")

    used_com_port = args.com_port

    if args.target:
        target = args.target
        print(f"Using target override: {target}")
    else:
        print("Power-cycling device via Alpaca TAC before target detection (known-good boot state) ...")
        power_cycle_device()

        print("Logging in over serial console" + (f" on {args.com_port}" if args.com_port else " (auto-detecting port)") + " ...")
        ser, used_com_port = open_console(args.com_port, boot_timeout=args.boot_timeout)
        try:
            target = detect_target_via_serial(ser)
        finally:
            ser.close()
        print(f"Detected target: {target} (via {used_com_port})")

    build_dir = resolve_build_dir(latest_build, target)
    print(f"Resolved build directory: {build_dir}")

    if args.dry_run:
        print("\n--dry-run: stopping before touching the device.")
        print(f"Would run: {PCAT_EXE} -PLUGIN SD -DEVICE <discovered-after-edl> "
              f'-BUILD "{build_dir}" -MEMORYTYPE UFS -SLOT 0')
        return None

    enter_edl_mode() if not args.skip_edl else print("Skipping EDL trigger; assuming device is already in EDL mode.")

    print("Waiting for device to re-enumerate in EDL mode ...")
    time.sleep(5)
    edl_device = wait_for_edl_device()
    device_id = pcat_device_id(edl_device)
    print(f"Found EDL device: {edl_device}")

    print("\nAbout to flash:")
    print(f"  Device ID:    {device_id}")
    print(f"  Build dir:    {build_dir}")
    print(f"  Memory type:  UFS")
    print(f"  Slot:         0")

    if not args.yes:
        reply = input("\nProceed with flashing? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return None

    try:
        run_pcat_flash(device_id, build_dir)
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
    print("\nFlash completed successfully.")

    return {
        "target": target,
        "build_path": str(latest_build / PERFORMANCE_SUBDIR),
        "com_port": used_com_port,
    }


def cmd_capture(args, handoff=None):
    """Runs (or resumes) boot-time capture. Uses target/build_path/com_port
    from `handoff` (set when 'flash' ran in the same invocation) in
    preference to --target/--build-path/--com-port."""
    if args.resume_pull:
        if not args.target:
            print("--resume-pull requires --target (no serial console is opened to auto-detect it).")
            sys.exit(1)
        if not args.build_path:
            print("--resume-pull requires --build-path.")
            sys.exit(1)
        print("--resume-pull: skipping login and the boot loop; pulling already-collected logs ...")
        try:
            pull_and_record(args.target, args.build_path, args.num_boots)
        except RuntimeError as e:
            print(f"\n{e}")
            sys.exit(1)
        return

    target = handoff["target"] if handoff else args.target
    build_path = handoff["build_path"] if handoff else args.build_path
    com_port = handoff["com_port"] if handoff else args.com_port

    if not build_path:
        print("capture (without flash) requires --build-path.")
        sys.exit(1)

    print("\nCapturing boot-time data over serial console...")
    try:
        capture_and_record(
            target=target,
            build_path=build_path,
            com_port=com_port,
            boot_timeout=args.boot_timeout,
            num_boots=args.num_boots,
        )
    except RuntimeError as e:
        print(f"\nBoot-time capture failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nBoot-time capture aborted by user; no run was recorded.")
        sys.exit(1)


def cmd_report(args, handoff=None):
    """Standalone report maintenance: (optionally) append a run JSON, then
    re-render the HTML from the JSON. No device involved. Only reached when
    'capture' isn't also requested in this invocation, since capture already
    records and renders its own data as part of collecting it."""
    target = (handoff["target"] if handoff else None) or args.target
    if not target:
        print("the 'report' stage requires --target (or run together with 'flash' to auto-detect it).")
        sys.exit(1)

    data_path, html_path = _data_html_paths(target)
    if args.report_cmd == "add-run":
        if not args.run_json:
            print("--report-cmd add-run requires --run-json <path|->")
            sys.exit(1)
        raw = sys.stdin.read() if args.run_json == "-" else Path(args.run_json).read_text(encoding="utf-8")
        run = json.loads(raw)
        data = load_data(data_path)
        add_run(data, run)
        save_data(data, data_path)
        print(f"Appended run -> {data_path}")

    render(data_path, html_path)
    print(f"Report regenerated -> {html_path}")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="bootbench.py", description=__doc__, epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "stage", nargs="+", choices=["flash", "capture", "report", "all"],
        help="One or more stages to run, in any order (always executed as "
             "flash -> capture -> report). 'all' is shorthand for all three.",
    )

    common = parser.add_argument_group("common")
    common.add_argument("--target", help="Override/declare target name (e.g. iq-9075-evk). Required for 'report' or 'capture --resume-pull' when run standalone; auto-detected via serial console by 'flash' or plain 'capture' otherwise.")
    common.add_argument("--com-port", help="Serial console COM port, used for login/detection (auto-detected by probing all ports if omitted)")
    common.add_argument("--boot-timeout", type=int, default=480, help="Seconds to wait for a login prompt, on first login and after each reboot (default: 480)")

    flash_group = parser.add_argument_group("flash")
    flash_group.add_argument("--tac-port", help="Alpaca TAC COM port name, e.g. VTP8 (auto-detected if only one TAC device)")
    flash_group.add_argument("--dry-run", action="store_true", help="Resolve paths and print the plan, then exit before touching the device (stops before EDL mode)")
    flash_group.add_argument("--yes", action="store_true", help="Skip the confirmation prompt before flashing (otherwise waits for manual y/N after entering EDL mode)")
    flash_group.add_argument("--skip-edl", action="store_true", help="Device is already in EDL mode; skip the TAC BootToEDL step")
    flash_group.add_argument("--recover", action="store_true", help="Power-cycle the device via TAC (out of EDL/Sahara back to normal boot) and exit -- use with the 'flash' stage")

    capture_group = parser.add_argument_group("capture")
    capture_group.add_argument("--build-path", help=r'Nightly build path this boot used, e.g. "\\swayam\...\performance" (required if capture runs without flash in the same command; supplied automatically otherwise)')
    capture_group.add_argument("--num-boots", type=int, default=3, help="Number of consecutive boots to capture (default: 3)")
    capture_group.add_argument("--resume-pull", action="store_true", help="Skip login/boot-loop entirely: adb is already enabled and all boots' /data/Logs-<n> are already on the device (a prior run reached adb pull and failed partway) -- just pull logs and (re)build the report. Requires --target and --build-path.")

    report_group = parser.add_argument_group("report")
    report_group.add_argument("--report-cmd", choices=["render", "add-run"], default="render", help="'render' (default) just regenerates the HTML from the existing JSON; 'add-run' appends --run-json first")
    report_group.add_argument("--run-json", help="Path to a run JSON to append (or '-' for stdin), used with --report-cmd add-run")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    stages = parse_stages(args.stage)

    if args.recover and "flash" not in stages:
        parser.error("--recover requires the 'flash' stage")
    if "capture" in stages and "flash" not in stages and not args.resume_pull and not args.build_path:
        parser.error("capture without flash requires --build-path")
    if "report" in stages and "capture" not in stages and "flash" not in stages and not args.target:
        parser.error("the 'report' stage requires --target (unless run together with 'flash')")

    handoff = None
    if "flash" in stages:
        handoff = cmd_flash(args)
        if handoff is None:
            return  # --recover, --dry-run, or the user declined to confirm

    if "capture" in stages:
        cmd_capture(args, handoff)

    if "report" in stages and "capture" not in stages:
        cmd_report(args, handoff)


if __name__ == "__main__":
    main()
