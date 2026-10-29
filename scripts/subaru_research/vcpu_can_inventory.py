#!/usr/bin/env python3
"""Read the application CAN inventory from extracted Denso VCPU payloads, never vehicle hardware.

The array boundaries and TX/RX interpretation are grounded in the FK1WHV010-880 consumer trace.
Other variants are compared by matching both copies of the same structure, not by raw ID byte hits.
This is not a KWI loader, firmware writer, or proof of which variant is installed on a vehicle.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct

PREFIX = (0x303, 0x306, 0x307, 0x305, 0x304, 0x310, 0x312, 0x314, 0x316, 0x308)
RX_PREFIX = (0x301, 0x311, 0x313, 0x315)
DIAGNOSTIC_FILTERS = (0x70F, 0x7D0, 0x7DE, 0x7DF)
MASK = 0x9FFFFFFF
TARGETS = (0x124, 0x146, 0x240, 0x241, 0x220, 0x221, 0x222, 0x321, 0x322, 0x323, 0x325, 0x32B, 0x6BB, 0x3C4)
FK1_SHA256 = "68db2738c0ec250f889a16e95c930522e18b18b059dac01a9680304ecff6bf3c"


def words(values):
  return struct.pack("<" + "I" * len(values), *values)


def positions(data, pattern):
  found = []
  start = 0
  while (start := data.find(pattern, start)) >= 0:
    if start % 4 == 0:
      found.append(start)
    start += 1
  return found


def filter_records(data, offset):
  result = []
  while offset + 16 <= len(data):
    identifier, mask, index, control = struct.unpack_from("<4I", data, offset)
    expected_control = 0x100 if len(result) < 4 else 0x800
    if identifier > 0x7FF or mask != MASK or index != len(result) << 16 or control != expected_control:
      break
    result.append(identifier)
    offset += 16
  if tuple(result[:4]) != RX_PREFIX or tuple(result[-4:]) != DIAGNOSTIC_FILTERS:
    raise ValueError("incomplete or unsupported hardware filter table")
  return result


def bank_inventory(data, app_offset, filter_offset, decode_fk1_config=False):
  hardware_ids = filter_records(data, filter_offset)
  rx_ids = hardware_ids[:-4]
  rx_offset = data.find(words(RX_PREFIX), app_offset + len(words(PREFIX)), filter_offset)
  if rx_offset < 0 or (rx_offset - app_offset) % 4:
    raise ValueError("no aligned application receive range")
  tx_count = (rx_offset - app_offset) // 4
  if not 10 <= tx_count <= 32:
    raise ValueError("unsupported transmit range size")
  count = tx_count + len(rx_ids)
  identifiers = struct.unpack_from("<" + "I" * count, data, app_offset)
  if tuple(identifiers[tx_count:]) != tuple(rx_ids) or any(value > 0x7FF for value in identifiers):
    raise ValueError("application inventory and hardware receive table disagree")
  if decode_fk1_config and count % 4 != 2:
    raise ValueError("unsupported FK1 configuration alignment")

  # Per-message configuration, flags, payload offsets and flags precede the ID array.
  # Each byte-flags array has two alignment bytes on these four payloads (N mod 4 == 2).
  config_offset = app_offset - 14 * count - 4 if decode_fk1_config else None
  payload_offsets = config_offset + 10 * count + count + 2 if decode_fk1_config else None
  if config_offset is not None and config_offset < 0:
    raise ValueError("configuration precedes input")
  messages = []
  cursor = 0
  for index, identifier in enumerate(identifiers):
    row = {"index": index, "canId": hex(identifier), "direction": "transmit" if index < tx_count else "receive"}
    messages.append(row)
    if not decode_fk1_config:
      continue  # Earlier payloads do not use the FK1 per-message layout; do not guess their fields.
    cfg = data[config_offset + 10 * index:config_offset + 10 * (index + 1)]
    if len(cfg) != 10 or not 0 < cfg[3] <= 8 or cfg[2] >> 4 not in (0, 1):
      raise ValueError(f"invalid configuration index={index} id={identifier:#x} offset={config_offset + 10 * index:#x} bytes={cfg.hex()}")
    ram_offset = struct.unpack_from("<H", data, payload_offsets + 2 * index)[0]
    if ram_offset != cursor:
      raise ValueError("payload offsets do not match configured frame lengths")
    cursor += cfg[3]
    row.update({"vcpuChannel": cfg[2] >> 4, "length": cfg[3], "ramPayloadOffset": ram_offset,
                "receiveNotification": bool(cfg[4] & 0x80), "configHex": cfg.hex()})
  return {"applicationTableFileOffset": hex(app_offset), "applicationCount": count, "transmitCount": tx_count,
          "receiveCount": len(rx_ids), "configurationFileOffset": hex(config_offset) if config_offset is not None else None,
          "hardwareFilterFileOffset": hex(filter_offset), "hardwareFilterCount": len(hardware_ids),
          "diagnosticFilterIds": [hex(value) for value in hardware_ids[-4:]], "messages": messages}


def inventory(data):
  app_offsets = positions(data, words(PREFIX))
  filter_offsets = positions(data, words((0x301, MASK, 0, 0x100)))
  if len(app_offsets) != 2 or len(filter_offsets) != 2:
    raise ValueError("expected two application inventories and two hardware filter copies")
  checksum = hashlib.sha256(data).hexdigest()
  banks = [bank_inventory(data, app, filt, checksum == FK1_SHA256) for app, filt in zip(app_offsets, filter_offsets, strict=True)]
  if banks[0]["messages"] != banks[1]["messages"]:
    raise ValueError("bank inventories disagree")
  version = sorted({match.decode("ascii") for match in re.findall(rb"F[A-Z0-9]{5}[0-9]{3}-[0-9]{3}", data)})
  if len(version) != 1:
    raise ValueError("expected one firmware version shared by both banks")
  by_id = {int(row["canId"], 16): row for row in banks[0]["messages"]}
  return {"sha256": checksum, "size": len(data), "version": version[0], "banks": banks,
          "consumerTraceMatchesExactPayload": checksum == FK1_SHA256,
          "targets": {hex(identifier): by_id.get(identifier) for identifier in TARGETS},
          "limitations": ("VCPU channels are not Panda bus numbers. Other variants share the verified structure, not individually traced semantics. " +
                          "Absent IDs here are not proof that no diagnostic or other firmware path can ever send them.")}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("payloads", type=Path, nargs="+", help="local extracted VCPU .bin files, not KWI containers")
  args = parser.parse_args()
  result = []
  for path in args.payloads:
    report = inventory(path.read_bytes())
    report["file"] = path.name
    result.append(report)
  print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
