# The instrument dev box, and running the hardware suites

Written 2026-10-01, when hardware testing paused and development moved to a
machine without the instrument. Until then these facts lived in one machine's
Claude memory, which no other machine sees. This file is how they travel.
Read it before running anything under `tests/hardware/`, here or on the
control PC.

## The machine

**CHBE-EBB3233D1**, Windows 10 Enterprise 19045. A development box with the
instrument's own hardware attached, not the control PC.

- **NI PCI-6733 as `Dev1`, physical** (NI-DAQmx 20.0). The simulated board
  is **`Dev1-sim`**, the standard name on every dev machine, so the two
  coexist. NI MAX also holds the instrument's `SPIM_Analog_Task` /
  `SPIM_Digital_Task`, their `_sim` copies, and legacy tasks
  (`LightSheetAO`, `LightSheetTriggers`, `My*Task*`). No `stacscope_test_*`
  task was left behind (checked 2026-10-01).
- **Hamamatsu ORCA-Flash4.0 V3** (C13440-20C, S/N 300661) on an Active
  Silicon FireBird Camera Link grabber. DCAM index 0. `dcamapi.dll` in
  System32.
- **IDS UI-3240CP-NIR** (USB3, 1280×1024 mono, global shutter, S/N
  4103283431), camera ID 1. The instrument's own uEye (Thorlabs-branded),
  run on IDS's driver on purpose. uEye SDK 4.97; `ueye_api_64.dll` in
  System32, headers in `C:\Program Files\IDS\uEye\develop\include`.
- **SDKs downloaded by the user** (never copy any of it into the repo; see
  the plan's "The DCAM SDK licence"):
  - DCAM: `C:\Users\wcalhoun6\Downloads\Hamamatsu_DCAMSDK4_v25056964\Hamamatsu_DCAMSDK4_v25056964\dcamsdk4\`
  - DCIMG: `Downloads\Hamamatsu_DCIMGSDK_v24026764`
- **Drives:** C: is the system drive and nearly full. **E: (HSDATA)** is the
  high-speed data drive. Every hardware test that records writes to
  `E:/stacscope_tests` by default (`STACSCOPE_RECORD_DIR`), and deletes its
  files after. E: took both cameras together, about 700 MB/s, in G13.
- **uv** is at `C:\Users\wcalhoun6\.local\bin`. In Git Bash, `export
  PATH="$HOME/.local/bin:$PATH"` first.

### Wiring

| From | To | Instrument's name | Used by |
|---|---|---|---|
| `Dev1/port0/line0` | Flash BNC trigger in | HamamatsuCamTrigger | DCAM M1, V-tests, G13 |
| `Dev1/port0/line2` | uEye trigger in | ThorCamTrigger | uEye Y/Z/W tests, G13 |
| `Dev1/port0/line1` | PFI9 (ctr0 gate) | — (feedback only) | H10 |

The P0.1→PFI9 wire is moved by hand. Ask the user whether it is in place
before relying on it. **The PCI-6733 has only PFI0–PFI9.** The pin some
connector blocks label "PFI12 / CTR 0 OUT" is counter 0's *output*, which
the board drives; wiring P0.1 there ties two outputs together. The old AO
feedback (AO0→PFI8) is retired: DO is the trusted timing reference.

## Safety rules, as practised

These held through every hardware session. The user decided each one.

- **Claude runs hardware only with the user's permission for that session.**
  It never carries over to a new session or a new machine. When a hardware
  command is blocked, give the user the exact command and say how far an
  allow rule would reach.
- **Analog output is only ever 0 V**, even when the AO task runs only to
  clock the digital lines. The AO channels drive galvos and other actuators;
  digital pulses into a camera trigger are harmless. Check every AO sample a
  script writes before running it.
- **Never load the instrument's `SPIM_*` tasks from a test.** Device suites
  save their own `stacscope_test_*` tasks and delete them after.
- **A physical `Dev1` can be present.** The simulated NI suite runs only on a
  device that is both named `Dev1-sim` and simulated. Before opening any NI
  task, check it cannot reach the real board.
- **Shut cameras down properly.** An improper DCAM shutdown can wedge the
  driver until a reboot. Every camera suite puts back each setting it
  changes. After G13 the Flash was found at its defaults (INTERNAL, EDGE,
  NEGATIVE, no subarray).
- **Close buffer monitors between runs.** A monitor holds its buffer's name,
  so the next `Rig.start` fails with `FileExistsError`.

## The hardware suites

None runs in the default `uv run pytest`. Each skips unless its variables
are set. Each test file's docstring has its exact command.

| Suite | Needs | Extras |
|---|---|---|
| `tests/hardware/nidaqmx` (H1–H10) | `STACSCOPE_HARDWARE=Dev1` (or `Dev1-sim` as a rehearsal); `STACSCOPE_FEEDBACK=PFI9` for H10 | `nidaqmx` |
| `tests/hardware/dcam` (S, K, M, V) | `STACSCOPE_CAMERA=0`; `STACSCOPE_CAMERA_TRIGGER=Dev1/port0/line0` for the triggered conditions | `nidaqmx` for those |
| `tests/hardware/ueye` (Y, K, Z, W) | `STACSCOPE_UEYE=1`, `STACSCOPE_UEYE_TRIGGER=Dev1/port0/line2` | `ueye`, `nidaqmx` |
| `tests/hardware/rig` (G13) | `STACSCOPE_RIG_CONFIG=config/rig.toml`, both trigger variables above | `nidaqmx`, `ueye` |

Other variables:
- `STACSCOPE_RECORD_DIR`: where recording tests write. The default is
  `E:/stacscope_tests`.
- `STACSCOPE_SOAK_SECONDS`: lengthens the long runs (H9, M2, the uEye
  soaks, G13).
- `STACSCOPE_KEEP_RECORDINGS=1`: G13 keeps its files.
- `STACSCOPE_MONITOR=1`: G13 opens a buffer monitor on each camera.
- `STACSCOPE_SENTINEL`: M1's fill byte.
- `STACSCOPE_DCAM_SDK` / `STACSCOPE_DCIMG_SDK`: point the conformance tests
  in `tests/adapters/dcam/` at the SDK folders above. These read headers
  only and touch no hardware.

The last full results are in the plan:
- NI: "Step 3", and the H-test records.
- Flash: the Milestone 5 status, 2026-10-01 (170 passed, 6 skipped).
- uEye: "5f — the uEye camera".
- G13: "The rig config".

## What stays on this machine

These are gitignored, or outside the repo, and a clone will not have them:
- `config/rig.toml`, this machine's rig config: both cameras as process
  children, both saving to `E:/stacscope_tests`, and the Flash with 403
  slots. Recreate it from `config/rig.example.toml`.
- `reference/` (the thesis and the legacy tree): ask the user for a copy.
- `.venv-monitor` and `.venv314`: rebuilt on first use (see CLAUDE.md).
- This machine's Claude memory. Everything in it that matters is in this
  file.

## Setting up another dev machine

What the default suite and the adapter suites need, with no hardware:
1. uv, with CPython 3.14 and 3.14t. Then `uv run pytest`: the default suite
   needs no vendor software.
2. For `tests/adapters/nidaqmx` (run by name, `--extra nidaqmx`): NI-DAQmx,
   and a simulated PCI-6733 named `Dev1-sim` created in NI MAX.
3. `tests/adapters/dcam` and `tests/adapters/ueye` run over fakes, and need
   no SDK. Their conformance tests skip without the SDK variables.
4. A rig config to try `Rig.start` by hand: copy `config/rig.example.toml`
   to `config/rig.toml`, and replace its source and cameras with a
   `simulated` source and `simulated_camera`s. The example holds only the
   instrument's kinds. The format is in `src/stacscope/app/config.py`'s
   docstring, and `rig_file` in `tests/integration/test_rig.py` is a working
   simulated rig.
