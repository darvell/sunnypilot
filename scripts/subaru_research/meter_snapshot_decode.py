#!/usr/bin/env python3
"""Decode saved CP1.5 meter-UART snapshots using the traced MeterCore layout.

Input must start at a known record boundary. This does not open serial/CAN devices, infer ECU ownership,
find sync bytes, issue commands, or enable any driving feature. Raw enums and speed units remain unlabeled.
"""
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import stat
import struct

SNAPSHOT_BYTES = 400
STATUS_BYTES = 8
PROFILE = "CP1.5-MeterCore-1e325a7a"
METERCORE_SHA256 = "1e325a7ac3cf7f2de7d0a8289c56d46d8fba7ae5a12cab7ca00a31966aecd1e7"


@dataclass(frozen=True)
class Field:
  byte: int
  shift: int
  mask: int
  getter: int


# UART snapshot offsets, NOT physical CAN byte/bit positions. The writer copies the 44-byte EyeSight
# block from snapshot offset 244 to packed state offset 244; IACC support is decoded separately.
FIELDS = {
  "cruiseMainLamp": Field(246, 6, 1, 0x2A121),
  "cruiseSetLamp": Field(246, 7, 1, 0x2A145),
  "setSpeedRaw": Field(247, 0, 255, 0x2A183),
  "holdDisplay": Field(249, 4, 1, 0x2A28A),
  "cancelDisplayRaw": Field(256, 0, 15, 0x2A60A),
  "automaticDepartureIconRaw": Field(261, 0, 7, 0x2A799),
  "automaticDeparturePatternRaw": Field(261, 4, 1, 0x2A7BA),
  "iaccSupport": Field(315, 1, 1, 0x2B2A6),
}


def decode_record(record: bytes, record_format: str) -> dict:
  if record_format not in ("uart400", "metercom408"):
    raise ValueError("format must be uart400 or metercom408")
  header = STATUS_BYTES if record_format == "metercom408" else 0
  if len(record) != header + SNAPSHOT_BYTES:
    raise ValueError("incomplete meter snapshot record")
  status = struct.unpack_from("<Q", record)[0] if header else None
  frame = record[header:]
  received_checksum = struct.unpack_from("<I", frame, 396)[0]
  expected_checksum = ~sum(frame[:396]) & 0xFFFFFFFF
  if received_checksum != expected_checksum:
    raise ValueError("meter snapshot checksum mismatch (corrupt, wrong alignment, or post-checksum debug override)")
  return {"profile": PROFILE, "layoutReferenceSha256": METERCORE_SHA256, "recordFormat": record_format,
          "recordSha256": hashlib.sha256(record).hexdigest(), "driverStatusFlags": status,
          "driverStatusClear": status == 0 if header else None,
          "checksumPassed": True,
          "ecuVersionBytesHex": frame[7:17].hex(), "communicationVersionRaw": frame[17],
          "fields": {name: (frame[field.byte] >> field.shift) & field.mask for name, field in FIELDS.items()},
          "physicalCanMappingEstablished": False, "physicalResumeInputEstablished": False,
          "automaticResumeCapabilityEstablished": False, "transmitEnabled": False,
          "limitations": ("This layout was traced in one MeterCore binary; it does not identify the installed hardware. " +
                          "Display values are not driver requests or proof of automatic-resume support. " +
                          "Driver flags are not a timestamp. Capture time must come from an independent recording.")}


def decode_file(path: Path, record_format: str):
  # Refuse hardware device nodes/FIFOs: this research command accepts saved regular files only.
  if not stat.S_ISREG(path.stat().st_mode):
    raise ValueError("input must be a saved regular file, not a device or stream")
  size = 408 if record_format == "metercom408" else 400
  if record_format not in ("uart400", "metercom408"):
    raise ValueError("unsupported record format")
  if path.stat().st_size == 0 or path.stat().st_size % size:
    raise ValueError("input must contain complete aligned records only")
  with path.open("rb") as handle:
    index = 0
    while (record := handle.read(size)):
      result = decode_record(record, record_format)
      result["recordIndex"] = index
      yield result
      index += 1


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("capture", type=Path)
  parser.add_argument("--format", choices=("uart400", "metercom408"), required=True)
  args = parser.parse_args()
  for result in decode_file(args.capture, args.format):
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
  main()
