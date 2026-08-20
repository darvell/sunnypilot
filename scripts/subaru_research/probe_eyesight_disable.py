#!/usr/bin/env python3
import argparse
import sys
import time
from typing import Any

EYESIGHT_TX = 0x787
EYESIGHT_RX = 0x78F
EYESIGHT_BUS = 2
CONFIRMATION = "DISABLE_EYESIGHT"

NRC = {
  0x12: "subFunctionNotSupported",
  0x22: "conditionsNotCorrect",
  0x31: "requestOutOfRange",
  0x33: "securityAccessDenied",
  0x78: "responsePending",
}


def request(panda: Any, payload: bytes, timeout: float = 0.75) -> bytes | None:
  if len(payload) > 7:
    raise ValueError("only single-frame UDS requests are supported")

  panda.can_clear(0xFFFF)
  panda.can_send(EYESIGHT_TX, bytes([len(payload)]) + payload + bytes(7 - len(payload)), EYESIGHT_BUS)
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    for address, data, bus in panda.can_recv():
      if bus == EYESIGHT_BUS and address == EYESIGHT_RX and data:
        frame_type = data[0] >> 4
        if frame_type == 0:
          return data[1:1 + (data[0] & 0xF)]
    time.sleep(0.003)
  return None


def describe(response: bytes | None) -> str:
  if response is None:
    return "TIMEOUT"
  if response[:1] == b"\x7f" and len(response) >= 3:
    return f"NRC {response[2]:#04x} ({NRC.get(response[2], 'unknown')}) [{response.hex()}]"
  return response.hex()


def main() -> None:
  parser = argparse.ArgumentParser(description="Probe whether an angle-LKAS Subaru EyeSight ECU accepts CommunicationControl")
  parser.add_argument("--engine-state", choices=("off", "running"), required=True,
                      help="label the actual engine state during this probe")
  parser.add_argument("--hold-seconds", type=int, default=0,
                      help="keep EyeSight disabled with TesterPresent; permitted only with engine-state=off")
  parser.add_argument("--confirm", required=True, help=f"must be exactly {CONFIRMATION}")
  args = parser.parse_args()

  from opendbc.car.structs import CarParams
  from panda import Panda

  if args.confirm != CONFIRMATION:
    parser.error(f"--confirm must be exactly {CONFIRMATION}")
  if args.hold_seconds < 0:
    parser.error("--hold-seconds must be non-negative")
  if args.hold_seconds and args.engine_state != "off":
    parser.error("holding EyeSight disabled is permitted only when --engine-state=off")

  print("Run only while parked, with nobody depending on EyeSight. The script re-enables communication before exit.")
  with Panda() as panda:
    panda.set_safety_mode(CarParams.SafetyModel.allOutput)
    try:
      session = request(panda, b"\x10\x03")
      print(f"extended session: {describe(session)}")
      if session is None or session[:1] != b"\x50":
        raise RuntimeError("failed to enter extended diagnostic session")

      disabled = request(panda, b"\x28\x03\x01")
      print(f"disable, engine={args.engine_state}: {describe(disabled)}")
      if disabled is None or disabled[:1] != b"\x68":
        return

      if args.hold_seconds:
        print(f"holding disable for {args.hold_seconds}s; TesterPresent every 0.5s")
        deadline = time.monotonic() + args.hold_seconds
        while time.monotonic() < deadline:
          panda.can_send(EYESIGHT_TX, b"\x02\x3e\x80\x00\x00\x00\x00\x00", EYESIGHT_BUS)
          time.sleep(0.5)
    finally:
      enabled = request(panda, b"\x28\x00\x01")
      print(f"re-enable: {describe(enabled)}")
      panda.set_safety_mode(CarParams.SafetyModel.silent)


if __name__ == "__main__":
  try:
    main()
  except KeyboardInterrupt:
    print("interrupted", file=sys.stderr)
    raise SystemExit(130) from None
