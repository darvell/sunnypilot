# CP1.5 nested firmware: the missing meter data plane

## Main finding

The old extracted directory was incomplete. The supplied QNX disk image contains nested LZO-compressed boot filesystems, including three relevant binaries that were missing from the previous extraction: `MeterCore`, `devc-metermid`, and `libcanrcv.so`.

Tracing them establishes that the meter's 400-byte vehicle-state snapshot arrives over a separate `/dev/ser3` UART connection. It is not reconstructed from `0x241` by the inspected meter driver. Therefore searching the VCPU's raw CAN inventory alone cannot recover every EyeSight-related state exposed to the head unit.

This is real additional vehicle-integration firmware, but not an identified EyeSight camera firmware image. It contains labels for EyeSight HOLD, set speed, cancellation and automatic-departure display states. The labels describe received/rendered state, not a proven command to accelerate or resume ACC.

## Verified extraction

The source QNX image is a filesystem with a boot sector, not the nonsensical partition map produced by treating boot-code bytes as MBR entries. Its superblock is at `0x2000`, with 4,096-byte blocks. The filesystem holds `basefs.ifs` and `early.ifs`.

| IFS | Stored bytes | Expanded imagefs bytes | LZO blocks | Regular files |
|---|---:|---:|---:|---:|
| basefs.ifs | 40,081,312 | 136,314,968 | 2,083 | 474 |
| early.ifs | 26,346,380 | 58,265,832 | 890 | 43 |

Both expanded images match declared lengths and pass the imagefs trailer word-sum checksum. Previously extracted `stc` and `CANMID_UART` reproduce byte-identical hashes from `basefs.ifs`, independently checking the extraction. This is format integrity, not proof that a public update mirror is manufacturer-authentic.

New binary identities from `early.ifs`:

```text
MeterCore, 367180 bytes
1e325a7ac3cf7f2de7d0a8289c56d46d8fba7ae5a12cab7ca00a31966aecd1e7

devc-metermid, 50951 bytes
61d0e1701180444f68d432d44e0b9db0a14f167629f4d8cda52d9082806796c9

libcanrcv.so, 93784 bytes
74362273b2ee385b3c55d9e1c58ad0eb6defd524ece8d5095ca6e2d9e7f7d947
```

No image was mounted, flashed, patched or executed as vehicle firmware. New IDA analyses use local working copies. Recovery sidecars created by opening older existing databases are analysis artifacts, not firmware modifications.

## Two distinct input paths

### Raw cockpit CAN

The previously traced VCPU -> CANMID_UART -> `/dev/can` path supplies raw CAN notifications. The QNX `stc` binary has a 17-entry receive-signal table at `0x22B8A0`, consumed by `sub_99E0`. It covers feature/customization/status fields such as `0x322 CSTM_ACC_CONT` and `0x32B` AVH status; `0x241` is not in that particular STC table.

The larger `system_setting` receiver at `0x125820` consumes a 35-entry table at `0x4262E0`, with explicit byte/bit/mask metadata. Neither the presence of a table label nor an import name proves a physical steering-wheel input is available there.

### Meter snapshot UART

In `devc-metermid`:

| Address | Observed behavior |
|---|---|
| `0x4F50` | Opens `/dev/ser3`, configures 921600 baud |
| `0x74C0` | Accumulates a 400-byte received UART frame |
| `0x7990-0x7A09` | Checks bytes 396-399 against little-endian `~sum(unsigned frame[0:396])` |
| `0x7AA7-0x7ADF` | Copies the frame unchanged into the shared receive buffer |
| `0x2F40` | Handles `0xC0080602`: returns eight status/error bytes plus the 400-byte snapshot |
| `0x7AEC` / `0x4590` | `/pps/metdebug` may override selected bytes after the checksum check |

The eight header bytes are status/error flags, not a timestamp. No mapping from CAN `0x241`, `0x321` or `0x322` to snapshot byte 315 was found in this driver. Who builds that UART snapshot remains upstream of this code and is not established by a UART device name alone.

The boot script `scripts/qnx_early_setup.sh` starts `devc-metermid` and then `MeterCore`. A commented UART line mentions a GMSL connection; that comment alone is not sufficient to identify the physical peer or wiring. The subsequent hypervisor trace below recovered an active counterpart.

### Active hypervisor setup and the GMSL boundary

The package's `rootfs_hv/hvImage_abl_emmc` also contains a QNX startup/imagefs filesystem, with the startup header embedded at outer-file offset `0x224`. Source SHA256:

```text
f9bd7d88f279e602041eead68d89d9445da70ab31383c4fa3c5050a2ac2e2875
```

The published inventory tool now accepts an explicit startup offset, without scanning for guessed headers:

```sh
python -m scripts.subaru_research.qnx_ifs_inventory rootfs_hv/hvImage_abl_emmc --startup-offset 0x224
```

On that actual image it decompresses 735 LZO blocks into 48,167,692 bytes, verifies the imagefs word-sum checksum, and inventories 282 regular files. Member offsets below refer to that **decompressed image**, not the outer file or execution addresses.

- `scripts/metermid-setup.sh`, offset `0x4CE7AD`, contains an active launch of PCI `8086:5ABC` as UART `/dev/ser3`, at 921600 baud with DMA. It then waits for the serial device and platform state and runs `gmslSerInit`. The startup-script member at `0x2DED09A` contains the corresponding command record.
- `gmslSerInit:0xC80` calls the serializer-reset and serializer-initialization routines. These addresses and the remaining library addresses are ELF virtual addresses.
- `libgmslSerializerLib.so.1:0x1A20` opens `/dev/ser3` and sets 921600 baud. Its register read/write routines at `0x1F20` / `0x2300` use sync `0x79`, a device address, a register address, length 1, and ACK `0xC3`. These are 4/5-byte serializer register requests, **not** the 400-byte meter snapshot protocol.
- Its initializer at `0x28F0` configures HDMI clock, oLDI/VESA mapping and TMDS HSYNC. Reset routine `0x4100` toggles encoded GPIO `0x40000548`; this does not identify or reset a demonstrated far-end meter MCU in this investigation.
- `scripts/graphics-setup.sh` associates HDMI-A-2 with `EDID_MET.bin`, separately from HDMI-A-1 and `EDID_CID.bin`.

This is stronger evidence than a commented UART label: the stored active startup configuration and executable serializer routines put UART0 on the meter/display transport path. It does not establish the far-end chip that constructs the 400-byte snapshot. A downstream meter/display controller is a plausible producer, not an identified one. No firmware code was executed or GPIO operation performed during analysis.

MIDDraw independently opens `/dev/MeterCom` at `0x1A5339`. Its automatic-departure names have data-table references at `0x350FB08` and `0x350FB30`, but no producer or enum meanings were recovered from those references. MeterService's ECU-version import and notification string identify a possible identity-reporting surface, not an observed controller identity. SlaveController watchdog/QVM labels were not evidence of the meter MCU.

## Exact IACC-support chain

In `MeterCore`:

1. `sub_1803A` is the meter control thread and calls `sub_1AD82`.
2. `sub_1AD82` requests `0xC0080602` on `/dev/MeterCom`, strips the eight status bytes and copies the 400-byte payload.
3. `sub_319A3` passes the 32-bit word at payload offset 312 to `sub_30765`.
4. `sub_30765` takes bit 25 of that word, therefore snapshot byte 315 bit 1, and stores it in packed state byte `0x26169B` bit 1.
5. `sub_2B2A6`, exposed in name tables as `iacc_sup` / `IACC_SUP`, returns that bit.
6. The existing libdispmet receive map associates abstract signal `0x5D` with packed byte `0x13B` and mask `0x03`.
7. STC/container conversion exposes the higher-level `Param_Current_Meter.iacc` property.

Do not confuse three address spaces: physical CAN payload positions, the meter's 400-byte UART snapshot, and service-container struct offsets.

The UI uses `iacc` to choose labels and explanatory popups for the MSA/ISA-related settings screens. It is not used as a Resume button event in the inspected presenter code. This supports treating it as a feature/support flag rather than live ACC engagement or driver intent.

## Automatic-departure and HOLD labels

`MeterCore` contains named state getters for:

```text
eye_info_hold
eye_info_ready
eye_info_off
eye_cru_main_lamp
eye_cru_set_lamp
eye_info_set_spd
eye_cc_cancel
eye_auto_departure_icon
eye_auto_departure_pattern
```

The automatic-departure names are at `0x3AA9B` and `0x3AAB3`. These are relevant evidence that this multi-variant cockpit software represents automatic-departure information. They do not establish that the 2024 Crosstrek supports that function, that a particular variant transmits it, or that the field is a writable resume request.

No decoder for those names has been promoted into driving code.

### Exact snapshot field positions recovered in the next tracing pass

`sub_319A3` passes the block beginning at snapshot byte 244 to `sub_2FA71`. That writer copies the EyeSight draw bytes into the same packed-state byte positions. The exported getter descriptors then select these exact bits:

| Report field | UART snapshot position | Getter address |
|---|---|---|
| `cruiseMainLamp` | byte 246 bit 6 | `0x2A121` |
| `cruiseSetLamp` | byte 246 bit 7 | `0x2A145` |
| `setSpeedRaw` | byte 247, eight bits | `0x2A183` |
| `holdDisplay` | byte 249 bit 4 | `0x2A28A` |
| `cancelDisplayRaw` | byte 256 bits 0-3 | `0x2A60A` |
| `automaticDepartureIconRaw` | byte 261 bits 0-2 | `0x2A799` |
| `automaticDeparturePatternRaw` | byte 261 bit 4 | `0x2A7BA` |
| `iaccSupport` | byte 315 bit 1 | `0x2B2A6` |

The automatic-departure fields are display values; their enum meanings and actual vehicle support remain unknown. `setSpeedRaw` is deliberately not converted to mph or km/h. `cancelDisplayRaw` is not labeled as a physical Cancel press.

`meter_snapshot_decode.py` now decodes a saved, aligned stream of raw 400-byte UART records or 408-byte `/dev/MeterCom` records. It checks the real firmware checksum, retains the driver error flags, and prints the raw fields plus an explicit unverified-capability boundary. It does not open a device, generate requests, scan for guessed frame boundaries, or treat flags as timestamps.

```sh
python -m scripts.subaru_research.meter_snapshot_decode saved-meter-uart.bin --format uart400
python -m scripts.subaru_research.meter_snapshot_decode saved-metercom-records.bin --format metercom408
```

The decoder's seven tests use synthetic packets with literal wire offsets independent of the decoder table, real temporary-file input, and the actual command-line entry point. They verify bit positions, neighboring-bit isolation, driver-error reporting, checksum failures, truncation/format/alignment errors and device-node rejection. There is no real saved meter-UART capture in the existing dataset, so this is a firmware-derived parser, not a car-verified signal decoder.

## A real cockpit cruise setting, not a speed/Resume command

The UI's `Set_CruiseLv(0..3)` operation looked potentially relevant, so it was traced beyond its name:

- `system_setting:0xB4660` validates the four values and posts service event 16.
- `0x15E580`, table row `0x1CCFA0`, encodes internal main/sub IDs 2/13.
- The static customization row at `0x42A258` is associated with `VCSVC_dsp_info_cruise_lv` at `0xCFBB0`.
- Its sender is `0x1596E0`, the **meter customization sender**, not the ordinary CAN sender.
- Meter kind 5 at `0x415F50` emits abstract IDs 8 and 11: a request flag and a four-valued setting.
- `libdispmet` maps ID 8 to outgoing meter byte 54 bit 0, and ID 11 to byte 74 bits 0-1.

This is the cockpit's four-level cruise acceleration-characteristic setting. It is not a set-speed command or physical Resume event. The readback metadata is `EYE322_7_0_CSTM_ACC_CONT_SET`; its two-bit value translates to four setting values. Sending the request is outside this research; nothing was sent.

A similarly numbered CAN kind 5 exists in another table, but it is not interchangeable with meter kind 5. Following only the integer and ignoring the callback would decode the wrong transport.

## AVH readback distinction

The firmware exposes three separate labels on `0x32B` byte 5:

- STC: bit 7, `AVH_ACTIVE`.
- system_setting: bit 5, `AVH_ON_LAMP`.
- system_setting: bit 6, `AVH_FUNC_ON_LUMP` (firmware spelling).

Our current comfort prototype uses the name `AVH_ACTIVE` for bit 5 based on a different vehicle's observations. These meanings must not be silently conflated. The research establishes a naming/semantic discrepancy; it does not by itself prove which bit is the right acknowledgement on our car. It remains unvalidated on vehicle hardware.

## Raw-car falsification of the 0x241 lead

Across six complete incident routes, 13 segments and 1,086,114 events:

- `0x241`: 13,037 frames; nominal 20 Hz.
- `0x342`: 6,519 frames; nominal 10 Hz.
- `0x345`: 6,519 frames; nominal 10 Hz.
- All 26,075 target frames are bus 1, DLC 8, with valid checksums.

Only stock-long routes 25/26/27 were used as the primary independent stock engagement pool. Routes 28/29/2a have replacement longitudinal configuration and cannot provide independent engagement labels for that purpose.

The best tested `0x241` raw-active candidate, bit 26, has 167 false positives and 24 false negatives against only 39 positive raw engagement labels. It misses the engagement-on transition. A changing word tracks engine RPM even while cruise main is off. No candidate warrants a cruise/main/set-speed/HOLD decoder. There are zero positive HOLD samples in the complete raw-log pool used by this analysis.

The larger latest rlogs are explicitly truncated prefixes (`Corrupted events detected`); they were not relabeled as complete successful evidence.

## Deliverables and next boundary

- `qnx_ifs_inventory.py`: bounded LZO decompression, imagefs checksum/extent validation and exact-path selected extraction, now including explicit embedded startup-header offsets. No firmware executable is run and no CAN is emitted.
- `test_qnx_ifs_inventory.py`: six behavioral tests for file extents, corruption, traversal, complete compressed input and embedded-header addressing. The embedded-image test failed before offset support was added and passes afterward.
- `meter_snapshot_decode.py` and `test_meter_snapshot_decode.py`: the eight traced receive fields, normal checksum, driver flags and seven synthetic-packet/file/CLI tests. Not vehicle-capture validation.
- `vcpu_can_inventory.py`: the earlier VCPU TX/RX inventory remains valid and separate from this meter plane.

Verification for this pass: 31 combined meter-decoder, image-reader, VCPU-inventory and stop-and-go-research tests passed; Ruff passed for the four Python files changed in this pass. The updated reader also passed the actual hypervisor image integrity/inventory check described above, not just synthetic tests.

The missing evidence is now specifically the **far-end meter-controller firmware, explicit board connectivity, or passive controller-version/traffic evidence**. None was found in the bounded producer trace. A synchronized receive-only meter-UART + CAN + independently labeled button/ACC/HOLD capture, after identifying the installed variant, could connect these serial fields to vehicle traffic. The current work recovers the received fields and active display transport, not the upstream snapshot producer or a working automatic-resume command. No driving behavior was changed in this pass.
