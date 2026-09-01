#!/usr/bin/env python3
r"""
Automates EDL flashing of a Qualcomm target device:
  1. Finds the latest nightly Yocto build on \\swayam\QLI_Builds\Yocto
  2. (unless --target is given) Power-cycles the device via the Alpaca TAC for a
     known-good boot state, then logs in over the device's serial console and
     identifies the target (e.g. iq-9075-evk) via `uname -n`
  3. Resolves the matching qcom-multimedia-proprietary-image-<target> flat build folder
  4. Puts the device into EDL mode via the Alpaca TAC COM server
  5. Re-discovers the device via PCAT now that it's in EDL/Sahara mode
  6. Confirms with the user, then flashes via PCAT -PLUGIN SD

Everything -- login, target detection, and (via boot_capture.py) post-flash
boot-time collection -- goes over the serial console; adb/USB is never used.
Which Windows COM port is the console varies per device/board, so the script
auto-detects it by probing every enumerated serial port for a live login/shell
prompt (override with --com-port if needed).

Recovery: if you Ctrl+C while a flash is in progress, the script kills the PCAT
process and power-cycles the device back to normal boot via TAC. If the script has
already exited and the device is stuck in EDL/Sahara mode, run with --recover to just
power-cycle it via TAC and exit (the Alpaca TAC only exposes discrete PowerOnButton /
PowerOffButton commands, not a single "reset" button, so recovery is power-off then
power-on).

Run with the ARM64 Python launcher on this machine:
    py -3 edl_flash.py [--target iq-9075-evk] [--com-port COM40] [--dry-run] [--yes]
    py -3 edl_flash.py --recover

Requires: pip install comtypes pyserial  (for the ARM64 interpreter, i.e. `py -3 -m pip install comtypes pyserial`)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

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
# Serial console -- login, target detection, and (via boot_capture.py)
# post-flash boot-time data collection all go over this. See open_console().
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
                f"'py -3 edl_flash.py --recover' to retry, or use the TAC app / physical "
                f"reset directly."
            )
        raise RuntimeError("Flash aborted by user (Ctrl+C); device power-cycled back to normal boot.")

    if proc.returncode != 0:
        raise RuntimeError(f"PCAT flash failed with exit code {proc.returncode}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="Override target name (skip serial auto-detect), e.g. iq-9075-evk")
    parser.add_argument("--com-port", help="Serial console COM port, used for login and target detection (auto-detected by probing all ports if omitted)")
    parser.add_argument("--tac-port", help="Alpaca TAC COM port name, e.g. VTP8 (auto-detected if only one TAC device)")
    parser.add_argument("--dry-run", action="store_true", help="Resolve paths and print the plan, then exit without touching the device")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt before flashing")
    parser.add_argument("--skip-edl", action="store_true", help="Device is already in EDL mode; skip the TAC BootToEDL step")
    parser.add_argument("--recover", action="store_true", help="Power-cycle the device via TAC (out of EDL/Sahara back to normal boot) and exit")
    parser.add_argument("--skip-boot-capture", action="store_true", help="Skip automatic post-flash boot-time capture over serial console")
    parser.add_argument("--boot-timeout", type=int, default=480, help="Seconds to wait for the device to reach a login prompt after flashing (default: 480)")
    args = parser.parse_args()

    global TAC_PORT_NAME
    TAC_PORT_NAME = args.tac_port

    if args.recover:
        print("Recovering device: power-cycling via TAC...")
        power_cycle_device()
        print("Done. Device should re-boot normally.")
        return

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
        return

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
            return

    try:
        run_pcat_flash(device_id, build_dir)
    except RuntimeError as e:
        print(f"\n{e}")
        sys.exit(1)
    print("\nFlash completed successfully.")

    if args.skip_boot_capture:
        return

    import os
    import boot_capture

    print("\nCapturing post-flash boot-time data over serial console...")
    try:
        html_path = boot_capture.capture_and_record(
            target=target,
            build_path=str(latest_build / PERFORMANCE_SUBDIR),
            com_port=used_com_port,
            boot_timeout=args.boot_timeout,
        )
    except RuntimeError as e:
        print(f"\nBoot-time capture failed (flash itself succeeded): {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nBoot-time capture aborted by user; no run was recorded (flash itself succeeded).")
        return

    os.startfile(html_path)


if __name__ == "__main__":
    main()
