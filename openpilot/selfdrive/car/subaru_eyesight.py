"""Receive-only qualification for the locked Gen3 EyeSight-off experiment.

This proves a bounded observation of parked, engine-off CAN and (optionally) absence of factory
actuation traffic, not ECU recovery, retained AEB, crank survival, button semantics or road readiness.
No diagnostic or actuation frames are generated here.
"""
from dataclasses import dataclass
import time

from opendbc.can import CANDefine, CANParser
from opendbc.car.can_definitions import CanData, CanRecvCallable
from opendbc.car.subaru.subarucan import subaru_checksum

DBC = "subaru_global_2017_generated"
# Independently sourced messages that should remain after EyeSight communication control.
RETAINED_MESSAGES = {
  (0, 0x40): "Throttle",
  (0, 0x48): "Transmission",
  (0, 0x119): "Steering_Torque",
  (1, 0x13A): "Wheel_Speeds",
  (1, 0x13C): "Brake_Status",
  (1, 0x240): "CruiseControl",
}
FACTORY_STREAMS = {(2, 0x124), (1, 0x220), (1, 0x221), (1, 0x222)}
FACTORY_ACTUATION_IDS = {addr for _, addr in FACTORY_STREAMS}
MAX_SAMPLE_GAP = 0.1  # observation policy, not a decoded vehicle timeout


def eyesight_traffic_present(packets: list[list[CanData]]) -> bool:
  return any(msg.src in (0, 1, 2) and msg.address in FACTORY_ACTUATION_IDS for packet in packets for msg in packet)


@dataclass(frozen=True)
class StartupEvidence:
  qualified: bool
  reason: str


class EngineOffObservation:
  def __init__(self, start: float, expect_silence: bool):
    self.start = self.last_update = start
    self.expect_silence = expect_silence
    self.required = set(RETAINED_MESSAGES) | (set() if expect_silence else FACTORY_STREAMS)
    self.first: dict[tuple[int, int], float] = {}
    self.last: dict[tuple[int, int], float] = {}
    self.counters: dict[tuple[int, int], int] = {}
    self.failure = ""
    self.parsers = {
      bus: CANParser(DBC, [(name, 0) for (src, _), name in RETAINED_MESSAGES.items() if src == bus], bus)
      for bus in (0, 1)
    }
    self.gears = CANDefine(DBC).dv["Transmission"]["Gear"]

  def update(self, packets: list[list[CanData]], now: float) -> None:
    if now < self.last_update:
      self.failure = "observation clock moved backwards"
    self.last_update = now
    if self.expect_silence and eyesight_traffic_present(packets):
      self.failure = "factory EyeSight actuation traffic remains"

    for packet in packets:
      for msg in packet:
        key = (msg.src, msg.address)
        if key not in self.required:
          continue  # returned/rejected echoes are never physical observation
        if len(msg.dat) != 8 or msg.dat[0] != subaru_checksum(msg.address, None, bytearray(msg.dat)):
          self.failure = f"invalid frame on bus {msg.src} at {msg.address:#x}"
          continue
        counter = msg.dat[1] & 0xF
        if key in self.counters and counter != (self.counters[key] + 1) % 16:
          self.failure = f"counter discontinuity on bus {msg.src} at {msg.address:#x}"
        self.counters[key] = counter
        if now - self.last.get(key, self.start) > MAX_SAMPLE_GAP:
          self.failure = f"receive gap on bus {msg.src} at {msg.address:#x}"
        self.first.setdefault(key, now)
        self.last[key] = now
        if key not in RETAINED_MESSAGES:
          continue
        cp = self.parsers[msg.src]
        cp.update([(int(now * 1e9), [msg])])
        # Inspect each fresh frame, not only the final snapshot: transient crank/motion must latch failure.
        if msg.address == 0x40 and (cp.vl["Throttle"]["Engine_RPM"] != 0 or cp.vl["Throttle"]["Throttle_Pedal"] != 0):
          self.failure = "engine running/cranking or accelerator pressed"
        elif msg.address == 0x48 and self.gears.get(int(cp.vl["Transmission"]["Gear"])) != "P":
          self.failure = "transmission not in Park"
        elif msg.address == 0x13A and any(cp.vl["Wheel_Speeds"][s] != 0 for s in ("FL", "FR", "RL", "RR")):
          self.failure = "vehicle moving"
        elif msg.address == 0x13C and cp.vl["Brake_Status"]["Brake"]:
          self.failure = "brake pressed; engine start may be imminent"
        elif msg.address == 0x119 and any(cp.vl["Steering_Torque"][s] for s in ("Steer_Error_1", "Steer_Warning")):
          self.failure = "steering fault present"

  def result(self, now: float) -> StartupEvidence:
    if now - self.start < 0.5:
      return StartupEvidence(False, "incomplete observation window")
    if self.failure:
      return StartupEvidence(False, self.failure)
    for bus, addr in sorted(self.required):
      key = (bus, addr)
      if key not in self.last or now - self.last[key] > MAX_SAMPLE_GAP:
        return StartupEvidence(False, f"missing/stale physical bus {bus} message {addr:#x}")
      if self.first[key] - self.start > MAX_SAMPLE_GAP:
        return StartupEvidence(False, f"incomplete physical bus {bus} message {addr:#x} coverage")
    return StartupEvidence(True, "bounded engine-off observation qualified; road readiness not established")


def observe_engine_off(can_recv: CanRecvCallable, expect_silence: bool,
                       observation: EngineOffObservation | None = None) -> StartupEvidence:
  # A fresh window is essential: queued frames from before the diagnostic response are not post-response evidence.
  can_recv()
  if observation is None:
    observation = EngineOffObservation(time.monotonic(), expect_silence)
  if observation.expect_silence != expect_silence:
    raise ValueError("observation mode mismatch")
  start = observation.start
  now = time.monotonic()
  while now - start < 0.5:
    packets = can_recv(wait_for_one=True)
    now = time.monotonic()
    observation.update(packets, now)
    if observation.failure:
      return StartupEvidence(False, observation.failure)
  return observation.result(now)
