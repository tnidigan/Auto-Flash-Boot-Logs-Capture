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

3. **Identify the device name and on-board memory type** *(`flash` stage)*
   Logs into the serial console and runs `uname -n` to get the target name
   (e.g. `iq-9075-evk`), unless `--target` is given explicitly. In the same
   console session, also runs `lsblk` to determine whether the device's
   storage is **UFS** or **EMMC** — unless `--memory-type` is given
   explicitly — since the two require different PCAT `-MEMORYTYPE` values
   to flash correctly:
   - **UFS**: top-level SCSI disks named `sda`, `sdb`, `sdc`, ... (with
     partitions `sda1`, `sda2`, ...)
   - **EMMC**: a top-level `mmcblk0` disk (with partitions `mmcblk0p1`,
     `mmcblk0p2`, ...), alongside `mmcblk0boot0`/`mmcblk0boot1`
   Only given once both `--target` and `--memory-type` are supplied does
   this step skip opening a serial console entirely.

4. **Resolve the matching build folder** *(`flash` stage)*
   Looks for `<latest_build>\performance\qcom-multimedia-proprietary-image-<target>`
   and confirms it's a valid flat build by checking for `rawprogram0.xml`.

5. **Enter EDL mode and flash** *(`flash` stage)*
   Triggers `BootToEDL` via the Alpaca TAC, waits for the device to
   re-enumerate, discovers it via PCAT, confirms with the user (unless
   `--yes`), then flashes via `PCAT -PLUGIN SD` using the memory type
   detected (or overridden) in step 3 — `-MEMORYTYPE UFS` or
   `-MEMORYTYPE EMMC` as appropriate (no longer hardcoded to `UFS`).

6. **Capture boot-time data** *(`capture` stage — runs when named alongside `flash`, e.g. `bootbench.py flash capture`, or standalone against an already-flashed device via `bootbench.py capture --build-path ...`)*
   - Logs back into the serial console (reusing the target/build-path/COM
     port `flash` just resolved, when run in the same command)
   - Immediately after login (before any boot loop runs), reads device
     identification fields over that same serial console — `Machine`
     (`/proc/device-tree/model`), `SoC` (`/sys/devices/soc0/machine` +
     `/revision`), `OS` (`/etc/os-release`), `Kernel` (`uname -r`), `Serial`
     (`/sys/devices/soc0/serial_number`), plus a `Captured` timestamp.
     Collected once per capture run (not per boot, since these don't change
     between the default/debug boots of one run); any field the device
     doesn't expose is simply omitted rather than failing the capture.
     Skipped entirely by `--resume-pull` (no serial console is opened); the
     run entry it records simply has no snapshot of its own, while every
     other run keeps whatever it captured.
   - Runs a **two-phase** capture, `--num-boots` boots per phase (default
     3 + 3 = 6 boots total):
     - **Default phase** — for each of N boots:
       - Provisions `collect_boot_logs.sh` to `/data/` on the device if not already present
       - Runs it — captures `systemd-analyze`, `blame`, `critical-chain`, `dmesg`, `journalctl`, etc. into `/data/Logs`
       - Renames `/data/Logs` → `/data/Logs-default-<n>` so it survives the next boot
       - Power-cycles and logs back in for the next boot
     - **Enable debug kernel cmdline params** — once, right before the
       first debug-phase boot, runs `bootctl status` on the device to
       determine the bootloader entry file to edit — **never hardcoded**:
       - If `bootctl status | grep type` does not report Boot Loader
         Specification **Type #1**, no per-entry `options` line exists to
         edit. Prints a message and skips this step entirely; the debug
         phase still runs its N boots with the same cmdline as the default
         phase. The report's Debug column shows a **"No debug prints
         enabled"** subtitle in this case (see Step 7).
       - If Type #1, runs `bootctl status | grep conf` and reads the
         current entry's real `source:` path (e.g.
         `/boot//loader/entries/ostree-1.conf`) — resolved dynamically from
         that output, so it's correct on any device regardless of which
         entry file is actually active.
       - Adds any of `initcall_debug`, `log_buf_len=4M`, or
         `systemd.log_level=debug` that aren't already present on that
         file's `options` line, then runs `sync`. Checks are per-parameter
         and order-independent — `log_buf_len` counts as present with *any*
         value (e.g. an existing `log_buf_len=1M` is left alone), while
         `initcall_debug` and `systemd.log_level=debug` must match exactly
         (a differing `systemd.log_level=` value is corrected in place, not
         duplicated). These params are **never reverted** — once added,
         they stay in the conf file permanently, even across future
         `capture`-only runs that don't reflash. Fails loudly with a clear
         error if the resolved entry file is missing or has no `options`
         line.
     - **Debug phase** — same as the default phase, N more boots, logs
       renamed to `/data/Logs-debug-<n>` (power-cycle before the last
       default boot's next boot is skipped; last boot of each phase still
       power-cycles into the next boot as normal)
   - After all boots: enables `adb`, waits for the device, `adb root`, then
     `adb pull`s every `/data/Logs-default-<n>` and `/data/Logs-debug-<n>`
     folder into `Boot-Logs/<target>/<build-folder>/`
   - Parses each boot's pulled logs into structured metrics: NHLOS, kernel,
     initramfs, sysinit, multi-user time, plus the top "hitter" (slowest)
     units per stage. On a debug boot whose dmesg carries `initcall_debug`
     timing output, the **Kernel time** row additionally gets its own
     hitters: the top 3 slowest probes/initcalls (by usecs) followed by up
     to 3 probe/initcall failures — parsed straight from that boot's
     `dmesg.txt`.
   - `--num-boots` is validated: any value below 1 falls back to the
     default of 3 with a printed warning.

7. **Record and report** *(automatic at the end of `capture`; or standalone via the `report` stage when `capture` isn't also running)*
   - **Only the first boot of each phase is recorded into the report** —
     `default-1` and `debug-1` — even when `--num-boots` captured more; all
     `N` boots per phase are still fully captured and permanently backed up
     under `Boot-Logs/` regardless. Column headers are simplified to just
     `Default` / `Debug` (no boot number or time-of-day suffix).
   - If the debug boot's cmdline params weren't actually active (Type #2
     bootloader, or any other reason `dmesg` carries no initcall/probe
     timing), the Debug column header shows a small **"No debug prints
     enabled"** subtitle. This check is content-based (looking at the
     parsed dmesg, not a flag), so it's correct even when `--resume-pull`
     is used, since that path never touches the cmdline-editing step at all.
   - Two additional rows are appended below the per-stage metrics, for both
     the Default and Debug columns:
     - **Overall Suspected Overheads** — pools the individual high-hitters
       already identified within the Kernel row (debug-derived
       probe/initcall costs), the `systemd → sysinit.target` row, and the
       `Total time till multi-user.target` row, then re-ranks that combined
       pool by each entry's actual duration. The result is the single
       biggest real contributors to boot time, regardless of which stage
       they occurred in — not a stage-vs-stage totals comparison. Capped at
       the top 3.
     - **Optimization Possibilities** — a placeholder row (renders as `–`
       until filled in) intended for a later Claude/agent review pass that
       reads the Overall Suspected Overheads row and suggests concrete
       fixes. `bootbench.py` itself does no analysis here. Once filled in,
       it renders as a bulleted list — one `<li>` per newline-separated
       point in the text (a leading `•` on each line is stripped); a
       single-paragraph value with no newlines falls back to one wrapped
       block instead of a list.
   - Appends the new run to `Boot-Charts/bootchart-data-<target>.json`
     (keeps the most recent 30 runs; full history is preserved separately as
     `.txt` backups under `Boot-Logs/`). The `Machine`/`SoC`/`OS`/`Kernel`/
     `Serial`/`Captured` snapshot collected right after login is stored
     **on this run's own entry** (not overwritten globally) — since
     kernel/OS version and the captured timestamp legitimately differ
     across builds, every captured run keeps its own snapshot permanently.
     `--resume-pull` never opens a serial console, so the run it records has
     no snapshot of its own (rendered as `–` in its column).
   - Regenerates `Boot-Charts/bootchart-overview-<target>.html` **entirely**
     from that JSON (the HTML is never hand-edited) — shows a **Device
     Information** table (Machine/SoC/OS/Kernel/Serial/Captured rows) styled
     identically to, and positioned above, the Boot Time Summary table, with
     one column per captured build/run (not per boot, since these fields
     don't change between a run's default/debug boots) — followed by
     boot-time trends per stage across runs, deltas vs. the previous run,
     and ranked slow units
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
  history for one target (`<slug>` = target name with hyphens/underscores stripped);
  each run entry records only the first default boot and first debug boot,
  even if `--num-boots` captured more
- `Boot-Charts/bootchart-overview-<slug>.html` — human-readable report,
  always regenerated from the JSON above; a **Device Information** table
  (Machine/SoC/OS/Kernel/Serial/Captured rows) styled like, and shown above,
  the Boot Time Summary table — one column per captured build/run, since
  these fields can differ build-to-build — then the Boot Time Summary table
  itself with two columns per run ("Default" / "Debug"), each with an
  Overall Suspected Overheads row and an Optimization Possibilities row
- `Boot-Logs/<target>/<build-folder>/` — raw per-boot log dumps
  (`Logs-default-1`, `Logs-default-2`, ..., `Logs-debug-1`, ...) plus
  permanent per-run `.txt` backups (unbounded history, unlike the 30-run
  cap on the JSON/HTML) — **every** captured boot is pulled and backed up
  here regardless of how many are shown in the JSON/HTML

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
- `--memory-type {UFS,EMMC}` — override/declare the on-board storage type passed to PCAT's `-MEMORYTYPE` (auto-detected via `lsblk` over serial console if omitted)
- `--dry-run` — resolve paths and print the plan, then exit before touching the device
- `--yes` — skip the y/N confirmation prompt before flashing
- `--skip-edl` — device is already in EDL mode; skip the TAC BootToEDL step
- `--recover` — power-cycle the device via TAC out of EDL/Sahara, then exit

**`capture`:**
- `--build-path PATH` — nightly build path used for this boot (required if `capture` runs without `flash` in the same command)
- `--num-boots N` — number of consecutive boots to capture **per phase**
  (default: 3 → 3 default-cmdline + 3 debug-cmdline = 6 boots total; only the
  first boot of each phase is recorded into the report, though all N per
  phase are captured and backed up). Values below 1 fall back to 3 with a
  printed warning.
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
