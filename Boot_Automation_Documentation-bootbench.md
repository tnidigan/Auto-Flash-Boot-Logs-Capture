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

The flash → boot → capture → report pipeline is automated end-to-end by a
single script, `bootbench.py`, which merges what used to be three separate
scripts (`edl_flash.py`, `boot_capture.py`, `bootchart_report.py`) into one
file with three composable **stages** you name explicitly on the command
line:

| Stage | Responsibility |
|---|---|
| `flash` | Finds the latest build, detects the target device, flashes it over EDL via PCAT |
| `capture` | Logs into the serial console, runs on-device log collection across N boots, pulls logs via `adb`, parses them into structured metrics, records + renders the report |
| `report` | Standalone JSON/HTML report maintenance (re-render the HTML, or manually append a run) — only needed when `capture` isn't also being run, since `capture` already records and renders its own data |

Stages are passed as positional arguments and can be combined in any order in
a single command (they always execute in `flash` → `capture` → `report`
order regardless of how they're typed); `all` is shorthand for all three:

```
bootbench.py flash                     # flash only
bootbench.py flash capture             # flash, then capture (no separate report step needed)
bootbench.py capture                   # capture only (device already flashed)
bootbench.py all                       # flash, capture, and report in one command
```

Results land in two directories:

- `Boot-Charts/` — `bootchart-data-<target>.json` (data) + `bootchart-overview-<target>.html` (report), one pair per target
- `Boot-Logs/<target>/<build-folder>/` — raw per-boot log dumps and permanent `.txt` backups (full history, not capped)

The original `edl_flash.py`, `boot_capture.py`, and `bootchart_report.py`
still exist on disk unchanged, but `bootbench.py` is the actively maintained,
single entry point going forward.

## 3. Prerequisites

`bootbench.py` talks to the device over a serial console (`pyserial`) for
both the `flash` and `capture` stages, and drives the Alpaca TAC power/EDL
control via COM (`comtypes`) for the `flash` stage. Install these before
running it:

```
py -3 -m pip install pyserial comtypes
```

or

```
python3 -m pip install pyserial comtypes
```

- `pyserial` — required for both `flash` and `capture` (serial console login,
  command execution, port auto-detection)
- `comtypes` — required for `flash` only (Alpaca TAC COM automation for
  power-cycle / EDL entry)
- `adb` must also be on `PATH` — used by `capture` to pull logs off the
  device after boot capture

### Run commands

On this machine's ARM64 Python launcher:

```
py -3 bootbench.py all --yes
```

On a regular (non-ARM) Windows Python install:

```
python3 bootbench.py all --yes
```

## 4. Clean Step-by-Step Flow

1. **Find the latest Yocto build** *(`flash` stage)*
   Scans `\\swayam\QLI_Builds\Yocto` for folders named
   `..._Nightly_Build_master_<N>` and picks the highest `<N>`.

2. **Reboot the device and identify the correct serial console port** *(`flash` stage)*
   Power-cycles the device via the Alpaca TAC (for a known-good boot state),
   then probes every enumerated Windows COM port until one shows a live
   login/shell prompt for the target (skipped if `--com-port` is given).

3. **Identify the device name** *(`flash` stage)*
   Logs into the serial console and runs `uname -n` to get the target name
   (e.g. `iq-9075-evk`), unless `--target` is given explicitly.

4. **Resolve the matching build folder** *(`flash` stage)*
   Looks for `<latest_build>\performance\qcom-multimedia-proprietary-image-<target>`
   and confirms it's a valid flat build by checking for `rawprogram0.xml`.

5. **Enter EDL mode and flash** *(`flash` stage)*
   Triggers `BootToEDL` via the Alpaca TAC, waits for the device to
   re-enumerate, discovers it via PCAT, confirms with the user (unless
   `--yes`), then flashes via `PCAT -PLUGIN SD`.

6. **Capture boot-time data** *(`capture` stage — runs when named alongside `flash`, e.g. `bootbench.py flash capture`, or standalone against an already-flashed device via `bootbench.py capture --build-path ...`)*
   - Logs back into the serial console (reusing the target/build-path/COM
     port `flash` just resolved, when run in the same command)
   - For each of N boots (default 3, `--num-boots`):
     - Provisions `collect_boot_logs.sh` to `/data/` on the device if not already present
     - Runs it — captures `systemd-analyze`, `blame`, `critical-chain`, `dmesg`, `journalctl`, etc. into `/data/Logs`
     - Renames `/data/Logs` → `/data/Logs-<n>` so it survives the next boot
     - Power-cycles and logs back in for the next boot (except after the last one)
   - After all boots: enables `adb`, waits for the device, `adb root`, then
     `adb pull`s every `/data/Logs-<n>` folder into `Boot-Logs/<target>/<build-folder>/`
   - Parses each boot's pulled logs into structured metrics: NHLOS, kernel,
     initramfs, sysinit, multi-user time, plus the top "hitter" (slowest)
     units per stage

7. **Record and report** *(automatic at the end of `capture`; or standalone via the `report` stage when `capture` isn't also running)*
   - Appends the new run to `Boot-Charts/bootchart-data-<target>.json`
     (keeps the most recent 30 runs; full history is preserved separately as
     `.txt` backups under `Boot-Logs/`)
   - Regenerates `Boot-Charts/bootchart-overview-<target>.html` **entirely**
     from that JSON (the HTML is never hand-edited) — shows boot-time trends
     per stage across runs, deltas vs. the previous run, and ranked slow
     units
   - The report is written to disk but is **not** auto-opened in a browser —
     open `Boot-Charts/bootchart-overview-<target>.html` yourself when ready

## 5. Recovery / Resume Options

- `flash --recover`: device stuck in EDL/Sahara after an aborted flash →
  power-cycles it back to normal boot and exits
- `capture --resume-pull` (requires `--target` and `--build-path`): flashing
  and all boots already succeeded but the `adb pull`/report step failed
  partway → re-runs just the pull + report step without repeating the flash
  or any boots
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

## 7. Command Reference

```
bootbench.py {flash,capture,report,all} [{flash,capture,report,all} ...] [options]
```

**Common** (apply to `flash` and/or `capture`):
- `--target TARGET` — override/declare target name (e.g. `iq-9075-evk`)
- `--com-port COM_PORT` — serial console COM port (auto-detected if omitted)
- `--boot-timeout SECONDS` — login-prompt wait timeout (default: 480)

**`flash`:**
- `--tac-port TAC_PORT` — Alpaca TAC COM port (auto-detected if only one TAC device)
- `--dry-run` — resolve paths and print the plan, then exit before touching the device
- `--yes` — skip the y/N confirmation prompt before flashing
- `--skip-edl` — device is already in EDL mode; skip the TAC BootToEDL step
- `--recover` — power-cycle the device via TAC out of EDL/Sahara, then exit

**`capture`:**
- `--build-path PATH` — nightly build path used for this boot (required if `capture` runs without `flash` in the same command)
- `--num-boots N` — number of consecutive boots to capture (default: 3)
- `--resume-pull` — skip login/boot-loop; just pull already-collected logs and (re)build the report (requires `--target` and `--build-path`)

**`report`:**
- `--report-cmd {render,add-run}` — `render` (default) just regenerates the HTML; `add-run` appends `--run-json` first
- `--run-json PATH` — run JSON to append (or `-` for stdin), used with `--report-cmd add-run`

## 8. Examples

```
# Flash only -- waits for a manual y/N confirmation right before flashing
py -3 bootbench.py flash

# Flash, then capture boot-time logs (skip the y/N prompt with --yes)
py -3 bootbench.py flash capture --yes

# Flash, capture, and report -- the full pipeline in one command
py -3 bootbench.py all --yes

# Capture only, on a device that's already flashed with this build
py -3 bootbench.py capture --build-path "\\swayam\...\performance"

# Resume a capture that flashed/booted fine but failed during the adb-pull/report step
py -3 bootbench.py capture --build-path "\\swayam\...\performance" --target iq-9075-evk --resume-pull

# Recover a device stuck in EDL/Sahara mode
py -3 bootbench.py flash --recover

# Re-render the HTML report from the existing JSON (no device involved)
py -3 bootbench.py report --report-cmd render --target iq-9075-evk

# Manually append a run JSON to the report (no device involved)
py -3 bootbench.py report --report-cmd add-run --run-json new_run.json --target iq-9075-evk
```
