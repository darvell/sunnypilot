#!/usr/bin/env python3
import argparse
from collections import Counter
import json
from pathlib import Path

from opendbc.can import CANParser
from opendbc.car.tests.routes import routes
from openpilot.tools.lib.logreader import LogReader, LogsUnavailable, ReadMode

DBC = "subaru_global_2017_generated"


def route_events(identifier: str):
  try:
    return LogReader(identifier, default_mode=ReadMode.RLOG)
  except LogsUnavailable:
    return LogReader(f"{identifier}/a", default_mode=ReadMode.RLOG)


def scan_route(identifier: str, car: str) -> dict:
  parser = CANParser(DBC, [("Transmission", 0), ("BSD_RCTA", 0)], 0)
  sonar_states = Counter()
  reverse_sonar_states = Counter()
  blindspot_states = Counter()

  for event in route_events(identifier):
    if event.which() != "can":
      continue

    parser_frames = []
    sonar_frames = []
    for frame in event.can:
      if frame.src == 0 and frame.address in (0x48, 0x228):
        parser_frames.append((frame.address, bytes(frame.dat), frame.src))
      if frame.src in (0, 2) and frame.address == 0x325:
        data = bytes(frame.dat)
        state = (data[2], data[3], data[4])
        sonar_states[state] += 1
        sonar_frames.append(state)

    if parser_frames:
      parser.update([event.logMonoTime, parser_frames])
      bsm = parser.vl["BSD_RCTA"]
      blindspot_states[(int(bsm["L_ADJACENT"]), int(bsm["L_APPROACHING"]),
                        int(bsm["R_ADJACENT"]), int(bsm["R_APPROACHING"]))] += 1
      if int(parser.vl["Transmission"]["Gear"]) == 3:
        reverse_sonar_states.update(sonar_frames)

  def serialize(counter):
    return [[list(state), count] for state, count in counter.most_common()]

  return {
    "route": identifier,
    "car": car,
    "sonar_states": serialize(sonar_states),
    "reverse_sonar_states": serialize(reverse_sonar_states),
    "blindspot_states": serialize(blindspot_states),
  }


def main() -> None:
  parser = argparse.ArgumentParser(description="Scan public Subaru test routes for BSM, RCTA, RAB, and rear-sonar states")
  parser.add_argument("--output", type=Path, default=Path("subaru_surroundings_route_scan.json"))
  args = parser.parse_args()

  results = []
  for route in routes:
    if route.car_model is None or not str(route.car_model).startswith("SUBARU_"):
      continue
    segment = route.segment if route.segment is not None else 0
    identifier = f"{route.route}--{segment}"
    try:
      result = scan_route(identifier, str(route.car_model))
      results.append(result)
      print(identifier, result["sonar_states"][:4], result["reverse_sonar_states"][:4])
    except Exception as error:
      results.append({"route": identifier, "car": str(route.car_model), "error": repr(error)})
      print(f"failed {identifier}: {error}")

  args.output.write_text(json.dumps(results, indent=2) + "\n")
  print(f"wrote {args.output}")


if __name__ == "__main__":
  main()
