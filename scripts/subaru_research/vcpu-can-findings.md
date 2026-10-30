# VCPU CAN routing: traced consumers and four-variant comparison

## Result

The recovered normal application path is a two-channel cockpit CAN bridge, with separate transmit and receive inventories. It forwards received EyeSight/HUD payloads upstream as raw CAN notifications. It does not decode physical Set/Resume/Cancel in the traced receive path and does not expose a normal application transmit entry for the longitudinal actuator IDs.

A concrete command/status pair exists: `0x6BB` is in the application transmit range, while `0x32B` is in the receive range. This is consistent with the separately observed comfort/Auto Vehicle Hold interface. It is not an ACC resume command.

The VCPU also explicitly receives `0x241`, unlike the legacy `0x240` and `0x146` definitions used in early cruise-button experiments. I checked the old `0x241 Cruise_Status` decoding against the car's raw logs: its cruise flags stay false through real ACC engagement and its set-speed field usually reads 512. The frame is real; the legacy signal interpretation is not usable on this car.

No driving code, ECU state, CAN permissions or firmware images were changed by this investigation.

## Scope and identity

The named IDA database was inspected without patching or saving over the original. The detailed consumer trace is for `FK1WHV010-880`, extracted payload SHA-256:

```text
68db2738c0ec250f889a16e95c930522e18b18b059dac01a9680304ecff6bf3c
```

Other variants were compared by their two matching application inventories and hardware filter structures. Their individual instruction-level consumers were not independently decompiled in this pass. Nothing here proves which VCPU version is installed in a particular vehicle.

A subsequent release-table check found that Subaru's August 20, 2026 bulletin lists **F71SHV011-680 for the 2024 Impreza/Crosstrek**. The retained F71 payload matches that version and was rehashed successfully. Use F71 as the documented research target for that model year; do not carry this report's detailed FK1 addresses or per-message layouts into F71. The separate CAN-table comparison remains valid. See `firmware-download-findings.md` for the current release and acquisition evidence.

## Correct address interpretation

The existing database was loaded at zero. Its labels such as `sub_383FC` are serialized file offsets, not the execution addresses stored in the firmware. Using them as execution addresses hides the table references.

For the primary main-program region of this exact payload:

```text
execution address = extracted .bin file offset + 0x81FC
```

Independent corroboration:

| File offset containing pointer | Stored pointer | Corresponding target file offset |
|---|---:|---:|
| `0x62CC` | `0x404EC` | `0x382F0`, application send routine |
| `0x62D4` | `0x405F8` | `0x383FC`, application receive routine |
| `0x753C` | `0xFC84` | `0x7A88`, three hardware-channel descriptors |

Startup instructions at file offsets `0x2CBBE` and `0x2CBC4` load `gp=0xFEE00000` and `r5=0x10688`. The table consumer's `r5-0x1190` resolves to execution `0xF4F8`, exactly the application ID array at file `0x72FC`. The hardware-filter programmer's `r5-0xDF4` resolves to execution `0xF894`, file `0x7698`.

The second serialized copy begins at file `0x3EFC4`, but its stored execution pointers increase by `0x78000`, not `0x3EFC4`. Those are different address domains. This is a recovered relation for the main program, not a complete KWI flash loader or proof of a boot-bank switching procedure.

## Exact application inventory and direction

The consumer reads 66 application IDs. The count is stored at file `0x6348`; the transmit count is 17 at `0x634A`.

Ranges at file `0x6494` (TX) and `0x649C` (RX):

| VCPU channel | TX index range | RX index range |
|---|---|---|
| 0 | 0-9 | 17-20 |
| 1 | 10-16 | 21-65 |

These are VCPU channel indices, not Panda bus numbers.

Transmit IDs:

```text
303 306 307 305 304 310 312 314 316 308
3A4 3FB 513 6B1 6BA 6BB 6BC
```

Selected receive IDs:

| CAN ID | Application index | RAM payload offset from `gp+0xEBA` |
|---|---:|---:|
| `0x40` | 33 | 264 |
| `0x241` | 34 | 272 |
| `0x32B` | 38 | 304 |
| `0x119` | 39 | 312 |
| `0x321` | 40 | 320 |
| `0x322` | 41 | 328 |
| `0x323` | 42 | 336 |
| `0x325` | 43 | 344 |
| `0x228` | 55 | 436 |
| `0x48` | 57 | 452 |
| `0x139` | 62 | 492 |
| `0x13C` | 64 | 508 |

All these selected receive records have the upstream receive-notification flag enabled. `0x6BB` is TX index 15 and its payload begins at offset 120.

`0x124`, `0x146`, `0x240`, `0x220`, `0x221`, `0x222`, and `0x3C4` are absent from these normal application inventories and the matched acceptance filters in all four payloads. This is narrower than saying those numbers never occur anywhere in the binaries. Isolated numeric hits in instructions or unrelated data are not CAN messages.

## Receive path, verified in disassembly and pseudocode

| Existing file-relative label | Execution address | Observed operation |
|---|---:|---|
| `sub_3AC4A` | `0x42E46` | Programs 53 four-word hardware acceptance rules into paged controller registers |
| `sub_3839C` | `0x40598` | Matches incoming ID against the current channel's receive index range; unknown ID returns `0xFFFF` |
| `sub_383FC` | `0x405F8` | Dispatches a recognized frame through length/check hooks to its application buffer |
| `sub_37250` | `0x3F44C` | Copies received bytes into the indexed payload buffer and invokes notification when config byte 4 has bit `0x80` |
| `sub_EC42` | `0x16E3E` | Builds an 11-byte notification: high/low CAN ID, length/status byte, eight payload bytes |
| `sub_FAA8` | `0x17CA4` | Enqueues the notification in a 200-entry, 16-byte queue |
| `sub_FEB2` / `sub_FC60` | `0x180AE` / `0x17E5C` | Drains raw notification type 1 and passes its 11 bytes to the upstream messaging wrapper |
| `sub_F41E` | `0x1761A` | Local side-effect dispatch for a small set of received IDs, not a physical cruise-button decoder |

The local side-effect dispatch handles `0x48`, `0x139`, `0x13C`, `0x345`, `0x390`, `0x39A`, `0x3AC` and a few other status IDs. It does not have cases for the EyeSight `0x321-0x325` group or the missing legacy cruise/button IDs. The selected EyeSight payloads are forwarded as raw message data rather than decoded into a new Resume request in this chain.

Some inherited names in the original database are misleading. For example, `can_build_tx_frame` at file `0x190A0` builds an ISO-TP flow-control payload, and `can_check_rx_filter` at `0x20C98` gates ISO-TP processing rather than comparing application CAN IDs. The application ID matcher is `sub_3839C` above. A name assigned in an earlier analysis is not evidence of function semantics.

## Transmit path and the comfort command

The ordinary request submission at file `0x3777C` (execution `0x3F978`) requires the message index to be below 17. Thus it can request `0x6BB` at index 15, but not the received throttle `0x40` at index 33. The bridge is not a generic Resume-to-throttle relay in this path.

The request-handling chain includes `sub_F170` (file `0xF170`, execution `0x1736C`), indexed payload copying at `sub_385FE`, the message scheduler at `sub_37124`, frame construction at `sub_382AE`, and driver queueing through `sub_382F0` / `sub_3482C`.

The TX-15 hook at file `0xEDF8` invokes two bit-log helpers. They inspect byte 6 bit 0 and byte 2 bit 4 of the buffered `0x6BB` payload. Those are not the already proposed byte-2 low-bit AVH request itself; their physical meanings remain unlabeled. No Resume/Set/Cancel meaning was assigned to them.

## Four-variant comparison

Both copies of the application and hardware filter tables match within each payload.

| Payload version | Size | Application IDs | TX | RX | Hardware rules |
|---|---:|---:|---:|---:|---:|
| `F71SHV011-680` | 499,724 | 58 | 16 | 42 | 46 |
| `F91WHV010-780` | 514,572 | 62 | 16 | 46 | 50 |
| `FC7WHV010-180` | 511,500 | 62 | 16 | 46 | 50 |
| `FK1WHV010-880` | 515,468 | 66 | 17 | 49 | 53 |

Each filter set includes four additional diagnostic IDs: `0x70F`, `0x7D0`, `0x7DE`, `0x7DF`.

- F91 and FC7 have identical ordered application IDs and filter records.
- Compared with F71, F91/FC7 add receive entries `0x3C5`, `0x66D`, `0x677`, `0x6D2`.
- FK1 compared with F91/FC7 removes receive `0x3C5` and `0x677`, and adds `0x119`, `0x2D2`, `0x332`, `0x342`, `0x35B`.
- FK1 adds application TX `0x3A4`.
- All four include TX `0x6BB`, RX `0x32B` and RX `0x241`.
- Earlier variants do not use FK1's exact per-message metadata layout; the parser deliberately omits those detailed fields rather than interpreting them with the wrong stride.

## Cross-check against the actual car

In the preserved 30-second passive CAN export:

- `0x241`: 600 frames, on Panda bus 1 only, 73 distinct payloads after masking checksum/counter.
- `0x342`: 300 frames, bus 1 only, two masked payloads.
- `0x345`: 300 frames, bus 1 only, 21 masked payloads.
- `0x32B`: 300 frames per bus on buses 0/1/2.
- `0x6BB`: 25 frames per bus on buses 0/1/2, with the known idle body ending in `0E 0A`.

I also tested the existing `0x241 Cruise_Status` DBC fields against raw routes 25/0 and 26/0. On route 26, 838 fresh labeled frames included 40 with logged ACC enabled and 456 with ACC available but disabled. Both old cruise bits remained false in every case. The old set-speed field read 512 in 837/838 frames. It is not a usable Gen3 cruise-state decoder.

## Deliverables and verification

`vcpu_can_inventory.py` reads local extracted `.bin` files and emits both bank inventories, exact hashes, direction ranges and selected ID presence. It does not import Panda or emit CAN. Four unit tests protect direction separation, diagnostic boundaries, duplicate-bank agreement, payload metadata consistency and rejection of incidental numeric matches. The tool was also run successfully against all four real payloads.

The detailed disassembly/pseudocode evidence and variant comparison remain in the local research artifacts; no proprietary firmware images are bundled in this repository.

## What this establishes about stop-and-go

This firmware provides a real cockpit request path (`0x6BB`) and a real status path (`0x32B`), plus raw forwarding of EyeSight dashboard messages. It does not supply a verified physical Resume command or a hidden implementation of the EyeSight longitudinal controller in the traced normal application path.

The absence of `0x146`/`0x240` from this bridge, and the falsified legacy `0x241` decoding, narrow the search: old DBC field names cannot substitute for the actual Gen3 signal map. Higher-level QNX signal transfer may give additional labels, but the VCPU trace does not justify spoofing a dashboard announcement or calling AVH an ACC resume mechanism.
