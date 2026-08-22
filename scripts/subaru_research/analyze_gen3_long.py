#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path

import numpy as np


def bit(data: bytes, index: int) -> int:
  return (data[index // 8] >> (index % 8)) & 1


def analyze_stock_csv(path: Path) -> dict:
  from opendbc.car.subaru.carcontroller import CarController

  rows = [row for row in csv.DictReader(path.open()) if row["vEgo"]]
  speed = np.array([float(row["vEgo"]) for row in rows])
  accel = np.array([float(row["aEgo"]) for row in rows])
  throttle = np.array([float(row["ES_Distance.Cruise_Throttle"]) for row in rows])
  rpm = np.array([float(row["ES_Status.Cruise_RPM"]) for row in rows])
  brake = np.array([float(row["ES_Brake.Brake_Pressure"]) for row in rows])
  engaged = np.array([row["cruiseEnabled"] == "True" for row in rows])
  standstill = np.array([row["standstill"] == "True" for row in rows])
  pedal = np.array([row["gasPressed"] == "True" or row["brakePressed"] == "True" for row in rows])
  clean = engaged & ~pedal

  throttle_delay = 8
  aligned_speed = speed[:-throttle_delay]
  future_accel = accel[throttle_delay:]
  aligned_clean = clean[:-throttle_delay]
  observed_throttle = throttle[:-throttle_delay]
  observed_rpm = rpm[:-throttle_delay]
  observed_brake = brake[:-throttle_delay]
  predicted = np.array([CarController.longitudinal_commands(a, v) for a, v in zip(future_accel, aligned_speed, strict=True)])

  light_decel = aligned_clean & (future_accel >= -0.3) & (future_accel < -0.03)
  standstill_engaged = clean & standstill
  zero_negative = {str(speed_mps): {
    "negative": CarController.longitudinal_commands(-0.001, speed_mps),
    "zero": CarController.longitudinal_commands(0.0, speed_mps),
  } for speed_mps in (0.0, 5.0, 10.0, 20.0, 30.0)}

  def median(values, mask):
    return float(np.median(values[mask])) if mask.any() else None

  return {
    "path": str(path),
    "rows": len(rows),
    "durationSeconds": float(rows[-1]["time_s"]) if rows else 0.0,
    "engagedNoPedalRows": int(clean.sum()),
    "engagedStandstillRows": int(standstill_engaged.sum()),
    "standstillHold": {
      "throttleMedian": median(throttle, standstill_engaged),
      "rpmMedian": median(rpm, standstill_engaged),
      "brakeMedian": median(brake, standstill_engaged),
      "brakeP99": float(np.quantile(brake[standstill_engaged], 0.99)) if standstill_engaged.any() else None,
    },
    "lightDeceleration": {
      "rows": int(light_decel.sum()),
      "observedThrottleMedian": median(observed_throttle, light_decel),
      "observedRpmMedian": median(observed_rpm, light_decel),
      "observedBrakeActiveFraction": float(np.mean(observed_brake[light_decel] > 0)) if light_decel.any() else None,
      "predictedThrottleMedian": median(predicted[:, 0], light_decel),
      "predictedRpmMedian": median(predicted[:, 1], light_decel),
      "predictedBrakeActiveFraction": float(np.mean(predicted[:, 2][light_decel] > 0)) if light_decel.any() else None,
    },
    "zeroAccelContinuity": zero_negative,
  }


def route_events(identifier: str):
  from openpilot.tools.lib.logreader import LogReader, LogsUnavailable, ReadMode

  try:
    return LogReader(identifier, default_mode=ReadMode.RLOG, sort_by_time=True)
  except LogsUnavailable:
    return LogReader(identifier + "/a", default_mode=ReadMode.RLOG, sort_by_time=True)


def analyze_route(identifier: str) -> dict:
  current_state = None
  counts = {
    "cruiseControlFrames": 0,
    "cruiseButtonsFrames": 0,
    "legacyCruiseOnNonzero": 0,
    "legacyCruiseActivatedNonzero": 0,
    "legacyMainButtonNonzero": 0,
    "legacySetButtonNonzero": 0,
    "legacyResumeButtonNonzero": 0,
    "gen3CruiseOffMatchesAvailable": 0,
    "gen3CruiseOffLabeledFrames": 0,
  }

  for event in route_events(identifier):
    which = event.which()
    if which == "carState":
      current_state = event.carState
      continue
    if which != "can":
      continue

    for frame in event.can:
      if frame.src != 1:
        continue
      data = bytes(frame.dat)
      if frame.address == 0x240:
        counts["cruiseControlFrames"] += 1
        counts["legacyCruiseOnNonzero"] += bit(data, 40)
        counts["legacyCruiseActivatedNonzero"] += bit(data, 41)
        if current_state is not None:
          available = bool(current_state.cruiseState.available)
          counts["gen3CruiseOffMatchesAvailable"] += int((not bool(bit(data, 28))) == available)
          counts["gen3CruiseOffLabeledFrames"] += 1
      elif frame.address == 0x146:
        counts["cruiseButtonsFrames"] += 1
        counts["legacyMainButtonNonzero"] += bit(data, 42)
        counts["legacySetButtonNonzero"] += bit(data, 43)
        counts["legacyResumeButtonNonzero"] += bit(data, 44)

  labeled = counts["gen3CruiseOffLabeledFrames"]
  return {
    "route": identifier,
    **counts,
    "gen3CruiseOffAvailableMatchRate": counts["gen3CruiseOffMatchesAvailable"] / labeled if labeled else None,
  }


def main() -> None:
  parser = argparse.ArgumentParser(description="Audit Gen3 Subaru longitudinal assumptions against route evidence")
  parser.add_argument("--stock-csv", type=Path, action="append", default=[])
  parser.add_argument("--route", action="append", default=[])
  parser.add_argument("--output", type=Path)
  args = parser.parse_args()

  result = {
    "stockCommandData": [analyze_stock_csv(path) for path in args.stock_csv],
    "cruiseSignalData": [analyze_route(route) for route in args.route],
  }
  text = json.dumps(result, indent=2, sort_keys=True) + "\n"
  if args.output is None:
    print(text, end="")
  else:
    args.output.write_text(text)
    print(f"wrote {args.output}")


if __name__ == "__main__":
  main()
