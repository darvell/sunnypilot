#!/usr/bin/env python3
import argparse
import csv
from pathlib import Path

DBC = "subaru_global_2017_generated"
MESSAGES = ("ES_Brake", "ES_Distance", "ES_Status")
ADDRESSES = {0x220: "ES_Brake", 0x221: "ES_Distance", 0x222: "ES_Status"}

FIELDS = {
  "ES_Brake": ("Brake_Pressure", "AEB_Status", "Cruise_Brake_Lights", "Cruise_Brake_Fault", "Cruise_Brake_Active", "Cruise_Activated"),
  "ES_Distance": ("Cruise_Throttle", "Cruise_Fault", "Car_Follow", "Low_Speed_Follow", "Cruise_Soft_Disable",
                  "Cruise_Brake_Active", "Distance_Swap", "Cruise_EPB", "Close_Distance", "Cruise_Cancel", "Cruise_Set", "Cruise_Resume"),
  "ES_Status": ("Cruise_Fault", "Cruise_RPM", "Cruise_Activated", "Brake_Lights", "Cruise_Hold"),
}


def main() -> None:
  parser = argparse.ArgumentParser(description="Export stock Subaru EyeSight longitudinal commands and vehicle response from a route")
  parser.add_argument("route", help="route or segment accepted by LogReader")
  parser.add_argument("--bus", type=int, default=1, help="EyeSight longitudinal bus on Global Gen2 cars")
  parser.add_argument("--output", type=Path, default=Path("subaru_stock_long.csv"))
  args = parser.parse_args()

  from opendbc.can import CANParser
  from openpilot.tools.lib.logreader import LogReader, ReadMode

  can_parser = CANParser(DBC, [(name, 0) for name in MESSAGES], args.bus)
  latest_state = {
    "vEgo": "",
    "aEgo": "",
    "gasPressed": "",
    "brakePressed": "",
    "cruiseEnabled": "",
    "standstill": "",
  }
  raw = dict.fromkeys(ADDRESSES, "")
  start_time = None

  fieldnames = ["time_s", *latest_state.keys()]
  for message in MESSAGES:
    fieldnames.extend(f"{message}.{field}" for field in FIELDS[message])
  fieldnames.extend(f"raw_{address:#x}" for address in ADDRESSES)

  rows = 0
  with args.output.open("w", newline="") as output:
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for event in LogReader(args.route, default_mode=ReadMode.RLOG, sort_by_time=True):
      which = event.which()
      if which == "carState":
        state = event.carState
        latest_state = {
          "vEgo": state.vEgo,
          "aEgo": state.aEgo,
          "gasPressed": state.gasPressed,
          "brakePressed": state.brakePressed,
          "cruiseEnabled": state.cruiseState.enabled,
          "standstill": state.standstill,
        }
        continue
      if which != "can":
        continue

      target_frames = []
      for frame in event.can:
        if frame.src == args.bus and frame.address in ADDRESSES:
          target_frames.append((frame.address, bytes(frame.dat), frame.src))
          raw[frame.address] = bytes(frame.dat).hex()
      if not target_frames:
        continue

      can_parser.update([event.logMonoTime, target_frames])
      if start_time is None:
        start_time = event.logMonoTime

      row = {"time_s": (event.logMonoTime - start_time) * 1e-9, **latest_state}
      for message in MESSAGES:
        for field in FIELDS[message]:
          row[f"{message}.{field}"] = can_parser.vl[message][field]
      for address in ADDRESSES:
        row[f"raw_{address:#x}"] = raw[address]
      writer.writerow(row)
      rows += 1

  print(f"wrote {rows} samples to {args.output}")


if __name__ == "__main__":
  main()
