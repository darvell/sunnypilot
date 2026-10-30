#!/usr/bin/env python3
"""Replay saved camera 0x323 packets through the actual Subaru parser and HUD sender, without transmitting.

Checks that only the existing LKAS alert filter and checksum change when keeping the source counter.
An incomplete log remains incomplete; no raw enum is labeled as a Resume request or vehicle capability.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import warnings

from opendbc.can import CANPacker, CANParser
from opendbc.car import structs
from opendbc.car.can_definitions import CanData
from opendbc.car.subaru.subarucan import create_es_infotainment, subaru_checksum
from opendbc.car.subaru.values import CanBus
from openpilot.tools.lib.logreader import LogReader

DBC = "subaru_global_2017_generated"


def audit_file(path: Path):
  if not path.is_file():
    raise ValueError("input must be a saved regular file")
  parser = CANParser(DBC, [("ES_Infotainment", 0)], CanBus.camera)
  packer = CANPacker(DBC)
  counts = Counter()
  metadata = []
  error = None
  with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    try:
      for event in LogReader(str(path), sources=[]):
        counts["events"] += 1
        if event.which() == "carParams":
          params = event.carParams
          metadata.append({"fingerprint": params.carFingerprint, "openpilotLongitudinalControl": params.openpilotLongitudinalControl})
        if event.which() != "can":
          continue
        for msg in event.can:
          if msg.address != 0x323 or msg.src != CanBus.camera:
            continue
          counts["cameraFrames"] += 1
          data = bytes(msg.dat)
          if len(data) != 8 or data[0] != subaru_checksum(0x323, None, bytearray(data)):
            counts["invalidFrames"] += 1
            continue
          if 0x323 not in parser.update([(int(event.logMonoTime), [CanData(0x323, data, CanBus.camera)])]):
            counts["parserRejected"] += 1
            continue
          values = dict(parser.vl["ES_Infotainment"])
          replacement = create_es_infotainment(packer, int(values["COUNTER"]), values, structs.CarControl.HUDControl.VisualAlert.none)[1]
          expected = bytearray(data)
          if ((data[2] >> 4) & 7) in (3, 4):
            expected[2] &= ~0x70
            counts["intentionalAlertEdits"] += 1
          expected[0] = subaru_checksum(0x323, None, expected)
          counts["preservedFrames" if replacement == bytes(expected) else "unexpectedPayloadChanges"] += 1
          # These bits were absent from the old DBC/sender. No meaning is assigned to them here.
          if data[2] & 0x80 or data[3] & 0xFE or any(data[4:]):
            counts["framesUsingPreviouslyDroppedBits"] += 1
    except Exception as exc:
      error = str(exc)
  warning_messages = [str(w.message) for w in caught]
  complete = not warning_messages and error is None
  passed = complete and counts["cameraFrames"] > 0 and counts["cameraFrames"] == counts["preservedFrames"]
  return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "dbc": DBC,
          "counts": dict(counts), "metadata": metadata, "warnings": warning_messages, "error": error,
          "completeLog": complete, "payloadPreservationPassed": passed,
          "physicalAutomaticDepartureMappingEstablished": False, "transmitEnabled": False}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("captures", type=Path, nargs="+")
  args = parser.parse_args()
  reports = [audit_file(path) for path in args.captures]
  print(json.dumps(reports, indent=2, sort_keys=True))
  raise SystemExit(0 if all(report["payloadPreservationPassed"] for report in reports) else 1)


if __name__ == "__main__":
  main()
