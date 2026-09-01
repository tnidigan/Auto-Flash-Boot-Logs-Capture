# Automated EDL Flash + Boot-Time Capture Pipeline

## 1. Context / Issue

Flashing a target device with the latest nightly Yocto build and capturing its
boot-time performance data (systemd-analyze, dmesg, journalctl, critical-chain,
etc.) was previously a manual, multi-step process:

- Manually browsing the Yocto build share to find the latest nightly build
- Manually identifying which Windows COM port was the device's serial console
- Manually power-cycling / EDL-triggering the device and flashing via PCAT
- Manually logging into the serial console after every boot to run log
  collection commands
- Manually pulling logs off the device via `adb` and hand-building a boot-time
  report

This was slow, error-prone (wrong build path, wrong COM port, missed boots),
and produced inconsistent reports across devices and runs.

## 2. Summary of Changes

Three scripts now automate the entire flash → boot → capture → report
pipeline end-to-end:

| Script | Responsibility |
|---|---|
| `edl_flash.py` | Finds the latest build, detects the target device, flashes it over EDL via PCAT, then triggers boot capture |
| `boot_capture.py` | Logs into the serial console, runs on-device log collection across N boots, pulls logs via `adb`, parses them into structured metrics |
| `bootchart_report.py` | Maintains the per-device JSON history and regenerates the HTML report from it |

Results land in two directories:

- `Boot-Charts/` — `bootchart-data-<target>.json` (data) + `bootchart-overview-<target>.html` (report), one pair per target
- `Boot-Logs/<target>/<build-folder>/` — raw per-boot log dumps and permanent `.txt` backups (full history, not capped)

## 3. Prerequisites

Both `edl_flash.py` and `boot_capture.py` talk to the device over a serial
console (`pyserial`); `edl_flash.py` also drives the Alpaca TAC power/EDL
control via COM (`comtypes`). Install these on the ARM64 Python interpreter
before running either script:

```
py -3 -m pip install pyserial comtypes
```

or 

```
python3 -m pip install pyserial comtypes
```

- `pyserial` — required by both scripts (serial console login, command
  execution, port auto-detection)
- `comtypes` — required by `edl_flash.py` only (Alpaca TAC COM automation for
  power-cycle / EDL entry)
- `adb` must also be on `PATH` — used by `boot_capture.py` to pull logs off
  the device after boot capture

## 4. Clean Step-by-Step Flow

1. **Find the latest Yocto build**
   Scans `\\swayam\QLI_Builds\Yocto` for folders named
   `..._Nightly_Build_master_<N>` and picks the highest `<N>`.

2. **Reboot the device and identify the correct serial console port**
   Power-cycles the device via the Alpaca TAC (for a known-good boot state),
   then probes every enumerated Windows COM port until one shows a live
   login/shell prompt for the target (skipped if `--com-port` is given).

3. **Identify the device name**
   Logs into the serial console and runs `uname -n` to get the target name
   (e.g. `iq-9075-evk`), unless `--target` is given explicitly.

4. **Resolve the matching build folder**
   Looks for `<latest_build>\performance\qcom-multimedia-proprietary-image-<target>`
   and confirms it's a valid flat build by checking for `rawprogram0.xml`.

5. **Enter EDL mode and flash**
   Triggers `BootToEDL` via the Alpaca TAC, waits for the device to
   re-enumerate, discovers it via PCAT, confirms with the user (unless
   `--yes`), then flashes via `PCAT -PLUGIN SD`.

6. **Capture boot-time data (automatic after a successful flash)**
   - Logs back into the serial console after flashing
   - For each of N boots (default 3):
     - Provisions `collect_boot_logs.sh` to `/data/` on the device if not already present
     - Runs it — captures `systemd-analyze`, `blame`, `critical-chain`, `dmesg`, `journalctl`, etc. into `/data/Logs`
     - Renames `/data/Logs` → `/data/Logs-<n>` so it survives the next boot
     - Power-cycles and logs back in for the next boot (except after the last one)
   - After all boots: enables `adb`, waits for the device, `adb root`, then
     `adb pull`s every `/data/Logs-<n>` folder into `Boot-Logs/<target>/<build-folder>/`
   - Parses each boot's pulled logs into structured metrics: NHLOS, kernel,
     initramfs, sysinit, multi-user time, plus the top "hitter" (slowest)
     units per stage

7. **Record and report**
   - Appends the new run to `Boot-Charts/bootchart-data-<target>.json`
     (keeps the most recent 30 runs; full history is preserved separately as
     `.txt` backups under `Boot-Logs/`)
   - Regenerates `Boot-Charts/bootchart-overview-<target>.html` **entirely**
     from that JSON (the HTML is never hand-edited) — shows boot-time trends
     per stage across runs, deltas vs. the previous run, and ranked slow
     units
   - Opens the HTML report automatically when done

## 5. Recovery / Resume Options

- `--recover` (`edl_flash.py`): device stuck in EDL/Sahara after an aborted
  flash → power-cycles it back to normal boot and exits
- `--resume-pull` (`boot_capture.py`): flashing and all boots already
  succeeded but the `adb pull`/report step failed partway → re-runs just the
  pull + report step without repeating the flash or any boots
- **Ctrl+C during a flash**: kills the PCAT process and automatically
  power-cycles the device back to normal boot

## 6. Output Locations

- `Boot-Charts/bootchart-data-<slug>.json` — machine-readable, multi-run
  history for one target (`<slug>` = target name with hyphens/underscores stripped)
- `Boot-Charts/bootchart-overview-<slug>.html` — human-readable report,
  always regenerated from the JSON above
- `Boot-Logs/<target>/<build-folder>/` — raw per-boot log dumps
  (`Logs-1`, `Logs-2`, ...) plus permanent per-run `.txt` backups (unbounded
  history, unlike the 30-run cap on the JSON/HTML)
</content>
</invoke>
