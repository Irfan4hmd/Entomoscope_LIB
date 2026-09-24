# Updates — stage safety, step scaling and repository hygiene

Work done on this fork in September 2026, against `v1.4.11`. Everything here is
local to this machine's build and has not been sent upstream.

The headline: **the stage was driving the lens into the base plate, and the
cause was step scaling, not the safety limits.** The limits were correct all
along — they were being applied to a position that reported half of reality.

---

## 1. The crash: `MICROSTEPS` was wrong by a factor of two

### Symptom

During autofocus, when the camera could not resolve a focus peak — no specimen,
or an empty plate — the stage descended until the lens struck the base plate.

### Diagnosis

`debug.log` showed autofocus was being bounded correctly:

```
New limit set to 179mm for lens: LCM-TELECENTRIC-1X-WD65-1.5-NI
autofocus retract to z=0.000, then descend to at most z=144.000
...
autofocus move to 90.000
autofocus abort requested
```

The geometry checks out: with a 91mm lens on a PI2AI, the lens meets the plate
at `200 - 91 + 70 = 179mm`, and the 35mm margin puts the boundary at 144mm. Yet
contact happened at a _booked_ z of 90.

`179 / 90 = 1.99`. The stage was travelling almost exactly twice as far as the
software recorded.

Working backwards from that ratio, the true resolution was 200 steps/mm, not the
400 that `constants.py` computed. Solving `200 x 1.8 x 8 / 360` gives **8** — the
driver is physically at 1/8 microstepping. The firmware had been saying so all
along, in a comment nobody had reconciled with the Python side:

```c
// servo.ino
// 1 step = 1.8/8 = 0.225    <- 1/8 microstepping
```

```python
# constants.py
MICROSTEPS = 16              # <- 1/16
```

### Fix

`MICROSTEPS = 16` -> `8`.

This is the change that stopped the crashes. **Do not revert it** without
simultaneously changing the driver's MS jumpers — the two must always agree.
A `MICROSTEPS` larger than the hardware makes the stage overshoot, which is the
dangerous direction; smaller makes it undershoot, which merely misfocuses.

> **Unverified:** the 1/8 conclusion is inferred from crash geometry, not from a
> direct measurement. Reference the axis, jog down 50mm, and measure the actual
> travel. It should read 50mm. This is still worth doing.

---

## 2. Axis boundary hardening

None of this caused the crash. It closes off the _same class_ of failure at
other entry points, where a wrong scaling or a missing limit would go unnoticed.

### The limits used to fail open

`Axis.__init__` defaulted `lower_limit` to `constants.AXIS_LENGHT` (343) — the
most permissive value available. On a PI2AI, whose real travel is 200mm, that
authorised descent to 308mm: **108mm past the mechanical end.**

Both limits now start unset, and nothing configured means no descent at all:

```python
self.lower_limit = None      # from the lens length, via set_current_limit
self.travel_limit = None     # the device's physical travel, via set_travel_limit
```

`travel_limit` is a new, independent guard fed from the device profile, so the
mechanical range is enforced separately from the lens geometry. `max_z` takes
whichever is tighter, minus `AXIS_SAFETY_MARGIN`. Callers can narrow the
boundary but never widen it — `ui._autofocus_max_z` now defers to `axis.max_z`
instead of recomputing it from `lower_limit`, so the two cannot disagree.

### Step scaling was sticky across device profiles

Only the PI2AI branch set `DIST_STEPS`. Selecting PI2AI and then switching to
PIs left the instance scaled for the 8mm screw on a machine using the 10mm one —
a 25% overshoot, the same class of bug as the `MICROSTEPS` mismatch above. All
three profiles now call `axis.set_rotation_height()` explicitly.

### Retraction is always permitted

`check_limit` previously refused moves in _both_ directions once the stage was
below the boundary. A stage left low — a shorter lens selected, lost steps, a
limit that changed underneath it — could not be lifted off the specimen.
Retraction is now allowed as long as it stays above the endstop.

### Rounding is booked from the steps actually sent

`self.z += dz` recorded the _requested_ distance while `int(DIST_STEPS * dz)`
truncated the steps actually commanded. Every move booked slightly more travel
than it performed, accumulating in the dangerous direction over a long sweep.
`_move_for` now converts the commanded step count back to millimetres and books
that.

### Move refusals are now visible

`move_for` / `move_to` return a consistent boolean, and a `wait=True` caller no
longer gets queued silently without waiting — it drains the queue first
(`_wait_for_idle`, 15s) or refuses. Autofocus checks the result and stops the
sweep rather than measuring against a position the stage never reached.

---

## 3. Autofocus behaviour

- **Refuses to start without a camera.** With no camera every sharpness reading
  is noise, so the search can never find a peak and simply descends the whole
  travel. `autofocus_clicked` now checks `self.camera is None` and explains
  rather than driving the stage down for nothing. This is the guard that would
  have prevented the observed descents.
- **Retract-or-home on failure.** If the post-failure retract is refused, the
  axis is homed instead — homing runs against the endstop and does not depend on
  `z`. A new `axis_reference_lost_signal` lets the worker thread invalidate the
  reference safely on the GUI thread.

---

## 4. Motor controller robustness

`tracking_thread` had no exit for an unrecognised reply. An empty read satisfies
neither condition (`""` is not `"done"`, and `""[1:].isdigit()` is `False`), so a
silent Arduino — a reset, a USB re-enumeration, a brownout under motor load —
left it spinning forever. Since `_move_for` calls `join()` when `wait=True`, that
froze autofocus permanently, with no log line and no way out but a restart.

The loop is now bounded by `AXIS_MOVE_REPLY_TIMEOUT` (60s, in `constants.py`
with the other tunables). A `while/else` clears the queue and logs on the
give-up path. **All parsing inside the loop is unchanged.**

### Known, deliberately not fixed

- The `except` branches still `break` without clearing the queue, so a
  `SerialException` mid-move leaves the queue wedged. It degrades visibly —
  blocking callers get a 15s wait then a refusal — rather than hanging.
- After a reply timeout the position is not exactly known. `z` is incremented
  before the move, so a lost reply during a _descent_ errs safe, but during a
  _retraction_ it reads closer to the top than the stage really is.
  Re-reference if the timeout ever fires.

---

## 5. Constants

| Constant                  | Was | Now   | Why                                                                     |
| ------------------------- | --- | ----- | ----------------------------------------------------------------------- |
| `MICROSTEPS`              | 16  | **8** | Matches the driver. The crash fix                                       |
| `AXIS_LENGHT`             | 343 | 300   | Measured rail. Read only by the "Entomoscope PI" profile, so inert here |
| `DEFAULT_SPEED`           | 5   | 3     | Reduce resonance noise and plate vibration                              |
| `MAX_SPEED`               | 6   | 4     | As above                                                                |
| `AXIS_MOVE_REPLY_TIMEOUT` | —   | 60    | New                                                                     |

`TRAVEL_LENGHT_N` is deliberately **unchanged at 200**. It, not `AXIS_LENGHT`,
bounds this machine, and raising it would let the lens travel further down.
Only change it against a measured travel.

Two comments were corrected because they were actively wrong and would mislead
anyone re-deriving the geometry:

- `SENSOR_TO_BASE_PLATE_N` was documented as the distance at the _highest_
  position. It is the distance at the **lowest** position — the geometry only
  closes that way (at z=0 the sensor is 270mm up; at z=200 it is 70mm), and the
  block comment further down already said so.
- `AXIS_LENGHT_S` is dead code; nothing reads it.

---

## 6. Firmware (`MotorController/servo/servo.ino`)

- All German comments translated to English. Code unchanged.
- `setup()` now sets the direction through `set_direction(HIGH)` rather than
  writing the pin directly. Previously `Y_DIR` was HIGH while the `direction`
  flag was `false`, so a stop arriving before the first move command would sign
  the remaining step count backwards and the host would correct its position the
  wrong way.
- An endstop guard in `move_motor()` was added and then **removed**: it cost a
  `digitalRead()` on every step, contributing jitter to an already noisy axis,
  and it only protected upward travel into the endstop — there is no switch at
  the bottom, so it did nothing for the plate contact that actually occurred.

> The Arduino IDE relocated this file from `MotorController/servo.ino` into a
> sketch folder, `MotorController/servo/servo.ino`. Git sees that as a deletion
> plus an untracked folder; it needs `git add MotorController/servo/` and
> `git rm --cached MotorController/servo.ino`. `update_manifest.json` still
> references the old path.

---

## 7. Repository and deployment

### Push was blocked twice

- `models/obb/yolov8m_obb3_best.onnx` (101 MB) exceeded GitHub's 100 MB limit.
  The `.gitattributes` LFS rule pointed at `OBB/yolov8m_obb3_best.onnx` while
  the file lived in `models/obb/`, so LFS never matched it. Now gitignored and
  distributed as a release asset; the stale rule is still in `.gitattributes`.
- Secret scanning found a **GitHub PAT** in `Tools/zenodo_config.json`. Removed,
  with `zenodo_uploader.py` reading `GITHUB_TOKEN` from the environment instead.
  A Google service-account key including its `private_key` was also committed
  and has been untracked. **Both credentials should be treated as compromised
  and revoked** — they existed in plaintext on disk and in the git reflog.

### `Tools/sync_to_install.sh`

The app installs to `C:\Program Files\ENIMAS\src`, which mirrors this repo but
is updated from a KIT GitLab manifest. This script pushes local changes onto it.

It enumerates `git ls-files`, so untracked files — the venv, the ONNX models,
`lenses.json`, credentials — are invisible to it and survive. It never deletes.
It backs up what it replaces, clears `__pycache__`, verifies afterwards, and
refuses to run while ENIMAS is up (asking the updater's own `application-status`
rather than guessing). Excluded: `*.ino`, `config.txt`, `lenses.json`,
`update_manifest.json`, repo metadata.

```bash
./Tools/sync_to_install.sh -n     # preview
./Tools/sync_to_install.sh        # apply, with confirmation
```

Note that an explicit update or `install.bat --repair` pulls `src/` from GitLab
and will overwrite all of this. Startup checks do not — they are local only.

---
