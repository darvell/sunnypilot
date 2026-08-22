#!/usr/bin/env python3
import argparse
from collections import Counter
import json
from pathlib import Path
import time

from probe_eyesight_disable import describe, request

BUTTON_DID = b"\x11\x30"
CONFIRMATION = "READ_EYESIGHT_BUTTONS"
DEFAULT_STATES = ("released", "main", "set", "resume", "cancel", "distance")


def read_button_did(panda):
  response = request(panda, b"\x22" + BUTTON_DID, timeout=0.25)
  if response is None or response[:3] != b"\x62" + BUTTON_DID:
    return None, describe(response)
  return response[3:], response.hex()


def capture_state(panda, label: str, duration: float, interval: float) -> dict:
  deadline = time.monotonic() + duration
  payloads = Counter()
  failures = Counter()
  samples = 0

  while time.monotonic() < deadline:
    payload, description = read_button_did(panda)
    samples += 1
    if payload is None:
      failures[description] += 1
    else:
      payloads[payload.hex()] += 1
    time.sleep(interval)

  return {
    "label": label,
    "samples": samples,
    "payloads": dict(payloads.most_common()),
    "failures": dict(failures.most_common()),
  }


def main() -> None:
  parser = argparse.ArgumentParser(description="Capture EyeSight DID 0x1130 while each Gen3 Subaru cruise button is held")
  parser.add_argument("--engine-state", choices=("off",), required=True,
                      help="the engine must be off; ignition may remain on so steering-wheel buttons are powered")
  parser.add_argument("--seconds-per-state", type=float, default=2.0)
  parser.add_argument("--interval", type=float, default=0.05)
  parser.add_argument("--states", nargs="+", default=DEFAULT_STATES)
  parser.add_argument("--output", type=Path, default=Path("gen3_cruise_button_did.json"))
  parser.add_argument("--confirm", required=True, help=f"must be exactly {CONFIRMATION}")
  args = parser.parse_args()

  if args.confirm != CONFIRMATION:
    parser.error(f"--confirm must be exactly {CONFIRMATION}")
  if args.seconds_per_state <= 0:
    parser.error("--seconds-per-state must be positive")
  if args.interval < 0.02:
    parser.error("--interval must be at least 0.02 seconds")

  from opendbc.car.structs import CarParams
  from openpilot.common.params import Params
  from panda import Panda

  if Params().get_bool("IsOnroad"):
    raise RuntimeError("refusing to run while onroad")

  print("Park the car, keep the engine off, and turn the ignition on. This sends diagnostic reads only; it does not disable EyeSight.")
  results = []
  with Panda() as panda:
    panda.set_safety_mode(CarParams.SafetyModel.allOutput)
    try:
      session = request(panda, b"\x10\x03")
      print(f"extended session: {describe(session)}")
      if session is None or session[:1] != b"\x50":
        raise RuntimeError("failed to enter the EyeSight extended diagnostic session")

      for label in args.states:
        action = "release all cruise buttons" if label == "released" else f"press and hold {label.upper()}"
        input(f"{action}, then press Enter to sample for {args.seconds_per_state:g} seconds: ")
        result = capture_state(panda, label, args.seconds_per_state, args.interval)
        results.append(result)
        print(json.dumps(result, sort_keys=True))
    finally:
      panda.set_safety_mode(CarParams.SafetyModel.silent)

  output = {
    "did": "0x1130",
    "engineState": args.engine_state,
    "secondsPerState": args.seconds_per_state,
    "interval": args.interval,
    "states": results,
  }
  args.output.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n")
  print(f"wrote {args.output}")


if __name__ == "__main__":
  main()
