#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path

import numpy as np

SPEED_BREAKPOINTS = [0., 5., 10., 15., 20., 25., 30.]


def aligned(values, frames):
  return values[:-frames] if frames else values


def future(values, frames):
  return values[frames:] if frames else values


def quantile(values, percentile, fallback):
  return float(np.quantile(values, percentile)) if len(values) else fallback


def main() -> None:
  parser = argparse.ArgumentParser(description="Derive empirical stock EyeSight longitudinal tables from analyze_stock_long.py CSV output")
  parser.add_argument("csv", type=Path)
  parser.add_argument("--throttle-delay", type=float, default=0.4)
  parser.add_argument("--brake-delay", type=float, default=0.25)
  parser.add_argument("--sample-period", type=float, default=0.05)
  args = parser.parse_args()

  rows = [row for row in csv.DictReader(args.csv.open()) if row["vEgo"]]
  speed = np.array([float(row["vEgo"]) for row in rows])
  accel = np.array([float(row["aEgo"]) for row in rows])
  throttle = np.array([float(row["ES_Distance.Cruise_Throttle"]) for row in rows])
  rpm = np.array([float(row["ES_Status.Cruise_RPM"]) for row in rows])
  brake = np.array([float(row["ES_Brake.Brake_Pressure"]) for row in rows])
  engaged = np.array([row["cruiseEnabled"] == "True" for row in rows])
  pedal = np.array([row["gasPressed"] == "True" or row["brakePressed"] == "True" for row in rows])

  throttle_frames = round(args.throttle_delay / args.sample_period)
  brake_frames = round(args.brake_delay / args.sample_period)

  gas_speed = aligned(speed, throttle_frames)
  gas_accel = future(accel, throttle_frames)
  gas_throttle = aligned(throttle, throttle_frames)
  gas_rpm = aligned(rpm, throttle_frames)
  gas_brake = aligned(brake, throttle_frames)
  gas_engaged = aligned(engaged, throttle_frames)
  gas_pedal = aligned(pedal, throttle_frames)

  brake_speed = aligned(speed, brake_frames)
  brake_accel = future(accel, brake_frames)
  brake_value = aligned(brake, brake_frames)
  brake_engaged = aligned(engaged, brake_frames)
  brake_pedal = aligned(pedal, brake_frames)

  result = {"source": str(args.csv), "breakpoints": []}
  previous = {"base_throttle": 1818., "max_throttle": 2600., "base_rpm": 100., "max_rpm": 1100., "max_brake": 250.}

  for speed_bp in SPEED_BREAKPOINTS:
    window = 2. if speed_bp < 20. else 3.
    near_gas = ((np.abs(gas_speed - speed_bp) <= window) & gas_engaged & ~gas_pedal &
                (gas_brake == 0) & (gas_throttle > 808))
    base = near_gas & (np.abs(gas_accel) <= 0.15)
    positive = near_gas & (gas_accel >= 0.35)
    if positive.sum() < 30:
      positive = near_gas & (gas_accel >= 0.1)

    near_brake = ((np.abs(brake_speed - speed_bp) <= window) & brake_engaged & ~brake_pedal &
                  (brake_value > 0) & (brake_accel <= -0.2))

    values = {
      "speed_mps": speed_bp,
      "base_throttle": quantile(gas_throttle[base], 0.5, previous["base_throttle"]),
      "max_throttle_p99": quantile(gas_throttle[positive], 0.99, previous["max_throttle"]),
      "base_rpm": quantile(gas_rpm[base], 0.5, previous["base_rpm"]),
      "max_rpm_p99": quantile(gas_rpm[positive], 0.99, previous["max_rpm"]),
      "max_brake_p99": quantile(brake_value[near_brake], 0.99, previous["max_brake"]),
      "sample_counts": {"base": int(base.sum()), "positive": int(positive.sum()), "brake": int(near_brake.sum())},
    }
    result["breakpoints"].append(values)
    previous = {
      "base_throttle": values["base_throttle"],
      "max_throttle": values["max_throttle_p99"],
      "base_rpm": values["base_rpm"],
      "max_rpm": values["max_rpm_p99"],
      "max_brake": values["max_brake_p99"],
    }

  print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
