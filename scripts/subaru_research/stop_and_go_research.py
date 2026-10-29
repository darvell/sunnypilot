#!/usr/bin/env python3
"""Two locked, receive-only stop-and-go research modes over complete local rlogs.

No Panda connection, CAN generation, diagnostic queries, or runtime feature unlocking. Candidate timing
is not a validated resume actuator; a positive diagnostic response is not a verified handover or AEB.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import warnings

from opendbc.can import CANDefine, CANParser
from opendbc.car.can_definitions import CanData
from opendbc.car.subaru.subarucan import subaru_checksum
from openpilot.selfdrive.car.subaru_eyesight import DBC, EngineOffObservation, RETAINED_MESSAGES, eyesight_traffic_present
from openpilot.tools.lib.logreader import LogReader

STOCK_MESSAGES = {
  (0, 0x40): "Throttle", (0, 0x48): "Transmission", (0, 0x119): "Steering_Torque",
  (1, 0x13A): "Wheel_Speeds", (1, 0x13C): "Brake_Status", (1, 0x221): "ES_Distance",
  (1, 0x222): "ES_Status", (2, 0x321): "ES_DashStatus",
}
# Conservative research policies only, not vehicle-calibrated safety constants.
AGES = {key: (0.3 if key == (2, 0x321) else 0.15 if key in ((1, 0x221), (1, 0x222)) else 0.1) for key in STOCK_MESSAGES}


class ReceivedCan:
  def __init__(self, messages, ages=None):
    self.messages = messages
    self.ages = ages or dict.fromkeys(messages, 0.1)
    self.parsers = {bus: CANParser(DBC, [(name, 0) for (src, _), name in messages.items() if src == bus], bus)
                    for bus in {src for src, _ in messages}}
    self.last = {}
    self.counters = {}
    self.last_time = None

  def frame(self, t, msg):
    key = (msg.src, msg.address)
    if key not in self.messages:
      return None, None
    if len(msg.dat) != 8 or msg.dat[0] != subaru_checksum(msg.address, None, bytearray(msg.dat)):
      return key, "invalid_frame"
    counter = msg.dat[1] & 15
    discontinuity = key in self.counters and counter != (self.counters[key] + 1) % 16
    self.counters[key] = counter
    parser = self.parsers[msg.src]
    accepted = parser.update([(int(t * 1e9), [msg])])
    if msg.address not in accepted:
      return key, "parser_rejected"
    previous = self.last.get(key)
    self.last[key] = t
    if previous is not None and t < previous:
      return key, "clock_reversed"
    if previous is not None and t - previous > self.ages[key]:
      return key, "receive_gap"
    return key, "counter_discontinuity" if discontinuity else None

  def values(self, bus, addr):
    return self.parsers[bus].vl[self.messages[(bus, addr)]]


class StockResumeResearch:
  """Annotate one hypothetical resume candidate per uninterrupted stock HOLD episode, never transmit."""
  def __init__(self):
    self.can = ReceivedCan(STOCK_MESSAGES, AGES)
    self.gears = CANDefine(DBC).dv["Transmission"]["Gear"]
    self.hold_start = self.minimum_distance = self.moving_since = None
    self.annotated = False
    self.driver_veto = False
    self.candidates = []
    self.vetoes = Counter()
    self.hold_episodes = 0

  def reset(self, reason):
    if reason in ("driver_gas", "driver_brake", "stock_distance_veto"):
      self.driver_veto = True
    self.vetoes[reason] += 1
    self.hold_start = self.minimum_distance = self.moving_since = None
    self.annotated = False

  def update(self, t, packets):
    if self.can.last_time is not None and t < self.can.last_time:
      self.reset("clock_reversed")
      self.can = ReceivedCan(STOCK_MESSAGES, AGES)
    self.can.last_time = t
    # Inspect every frame so a gas/brake/fault transition cannot hide behind a later sample in the batch.
    distance_updated = invalid_batch = False
    for packet in packets:
      for msg in packet:
        key, error = self.can.frame(t, msg)
        if error:
          invalid_batch = True
          self.reset(error)
        if key is None or error:
          continue
        v = self.can.values(*key)
        if key == (0, 0x40) and v["Throttle_Pedal"]:
          self.reset("driver_gas")
        elif key == (1, 0x13C) and v["Brake"]:
          self.reset("driver_brake")
        elif key == (0, 0x119) and (v["Steer_Error_1"] or v["Steer_Warning"]):
          self.reset("steering_fault")
        elif key == (1, 0x222) and (v["Cruise_Fault"] or not v["Cruise_Activated"]):
          self.reset("stock_status_change")
          invalid_batch = True
        elif key == (2, 0x321) and (v["Cruise_Fault"] or v["Cruise_Soft_Disable"] or not v["Cruise_On"] or
                                   v["Cruise_State"] != 3 or v["Conventional_Cruise"]):
          self.reset("stock_dash_change")
          invalid_batch = True
        elif key == (1, 0x221):
          distance_updated = True
          if any(v[s] for s in ("Cruise_Fault", "Cruise_Soft_Disable", "Cruise_Cancel", "Cruise_Set", "Cruise_Resume", "Distance_Swap")):
            self.reset("stock_distance_veto")
          elif not v["Car_Follow"]:
            self.reset("lead_lost")

    if any(key not in self.can.last or t - self.can.last[key] > age for key, age in AGES.items()):
      self.reset("missing_or_stale_can")
      return
    pt, gear, steer = (self.can.values(0, addr) for addr in (0x40, 0x48, 0x119))
    wheels, brake, distance, status = (self.can.values(1, addr) for addr in (0x13A, 0x13C, 0x221, 0x222))
    dash = self.can.values(2, 0x321)
    if (pt["Throttle_Pedal"] or brake["Brake"] or steer["Steer_Error_1"] or steer["Steer_Warning"] or
        any(distance[s] for s in ("Cruise_Fault", "Cruise_Soft_Disable", "Cruise_Cancel", "Cruise_Set", "Cruise_Resume", "Distance_Swap")) or
        status["Cruise_Fault"] or dash["Cruise_Fault"] or dash["Cruise_Soft_Disable"]):
      self.reset("pedal_or_factory_fault")
      return
    if (self.gears.get(int(gear["Gear"])) != "D" or any(wheels[s] for s in ("FL", "FR", "RL", "RR")) or
        not status["Cruise_Activated"] or not dash["Cruise_On"] or dash["Cruise_State"] != 3 or dash["Conventional_Cruise"]):
      self.reset("not_adaptive_hold")
      self.driver_veto = False
      return
    if invalid_batch:
      return
    if self.driver_veto:
      self.reset("driver_input_veto_until_hold_exit")
      return
    if not distance["Car_Follow"]:
      self.reset("no_stock_lead")
      return
    if self.hold_start is None:
      self.hold_start = t
      self.minimum_distance = distance["Close_Distance"]
      self.hold_episodes += 1
    if not distance_updated:
      return  # cached samples never advance the lead-motion qualification
    close = distance["Close_Distance"]
    self.minimum_distance = min(self.minimum_distance, close)
    if not (3.0 < close < 4.5 and close - self.minimum_distance >= 0.5):
      self.moving_since = None
      return
    if self.moving_since is None:
      self.moving_since = t
    if not self.annotated and t - self.hold_start >= 0.5 and t - self.moving_since >= 0.15:
      self.candidates.append({"timeSeconds": t, "holdSeconds": t - self.hold_start,
                              "distanceMeters": close, "leadGainMeters": close - self.minimum_distance,
                              "action": "annotation_only_no_transmission"})
      self.annotated = True

  def result(self):
    return {"holdEpisodes": self.hold_episodes, "resumeCandidates": self.candidates, "vetoCounts": dict(self.vetoes),
            "actuatorValidated": False, "physicalResumeButtonDecoded": False,
            "missingProof": ["labeled stock HOLD release with physical input and ACC retained",
                             "receiver and arbitration behavior for any proposed resume actuation"]}


class EyeSightOffResearch:
  """Audit the actual disable/acknowledgement/silence/crank sequence; never infer restoration."""
  def __init__(self):
    self.pre = self.silence = None
    self.request_at = self.ack_at = self.crank_at = None
    self.engine_off_before_request = self.quiet_before_crank = False
    self.crank_survival_observed = False
    self.can = ReceivedCan(RETAINED_MESSAGES)
    self.timeline = []
    self.failures = set()

  def diagnostic(self, t, address, dat, src, outgoing=False):
    if src != 2 or len(dat) != 8 or not 0 < dat[0] <= 7:
      return
    payload = dat[1:1 + dat[0]]
    if outgoing and address == 0x787 and payload == b'\x28\x03\x01':
      if self.request_at is not None:
        self.failures.add("repeated_disable_attempt")
      self.request_at = t
      self.engine_off_before_request = self.pre is not None and self.pre.result(t).qualified
      if not self.engine_off_before_request:
        self.failures.add("no_complete_engine_off_baseline_before_request")
      self.timeline.append({"timeSeconds": t, "event": "disable_request_logged_not_proof_of_delivery"})
    elif not outgoing and address == 0x78F and self.request_at is not None:
      if payload == b'\x68\x03':
        if self.ack_at is not None:
          self.failures.add("duplicate_disable_acknowledgement")
        self.ack_at = t
        self.silence = EngineOffObservation(t, expect_silence=True)
        self.timeline.append({"timeSeconds": t, "event": "positive_communication_control_response"})
      elif payload[:2] == b'\x7f\x28':
        self.failures.add("communication_control_rejected_" + payload.hex())
        self.timeline.append({"timeSeconds": t, "event": "negative_communication_control_response", "responseHex": payload.hex()})

  def update(self, t, packets):
    if self.pre is None:
      self.pre = EngineOffObservation(t, expect_silence=False)
    if self.request_at is None:
      # A sliding clean interval: running/cranking/moving data must not qualify, but a later complete
      # engine-off window can. This only annotates a request already in the log; it cannot trigger one.
      self.pre.update(packets, t)
      if self.pre.failure:
        self.pre = EngineOffObservation(t, expect_silence=False)
    for packet in packets:
      for msg in packet:
        self.diagnostic(t, msg.address, msg.dat, msg.src)
        key, error = self.can.frame(t, msg)
        if self.ack_at is not None and error:
          self.failures.add(error)
        if key == (0, 0x40) and not error and self.ack_at is not None:
          if self.can.values(0, 0x40)["Engine_RPM"] > 0 and self.crank_at is None:
            self.crank_at = t
            self.timeline.append({"timeSeconds": t, "event": "first_engine_rotation_after_ack"})
    if self.ack_at is None:
      return
    if eyesight_traffic_present(packets):
      self.failures.add("factory_actuation_after_ack_no_verified_handover")
    if self.crank_at is None and self.silence is not None:
      self.silence.update(packets, t)
      if self.silence.failure:
        self.failures.add(self.silence.failure)
      self.quiet_before_crank = self.quiet_before_crank or self.silence.result(t).qualified
    if self.crank_at is not None:
      if not self.quiet_before_crank:
        self.failures.add("no_qualified_silence_window_before_crank")
      if any(key not in self.can.last or t - self.can.last[key] > 0.1 for key in RETAINED_MESSAGES):
        self.failures.add("retained_can_missing_during_crank")
      if t - self.crank_at >= 1.0 and not self.failures:
        self.crank_survival_observed = True

  def result(self):
    failures = set(self.failures)
    if self.request_at is None:
      failures.add("no_disable_request")
    if self.ack_at is None:
      failures.add("no_positive_communication_control_response")
    if not self.quiet_before_crank:
      failures.add("no_qualified_engine_off_silence_window")
    if not self.crank_survival_observed:
      failures.add("no_continuous_silence_observation_through_crank")
    return {"engineOffBeforeRequestObserved": self.engine_off_before_request,
            "silenceBeforeCrankObserved": self.quiet_before_crank,
            "silenceThroughCrankObserved": self.crank_survival_observed and not failures,
            "failures": sorted(failures), "timeline": self.timeline,
            "stockHandoverEstablished": False, "physicalButtonsVerifiedAfterSilence": False,
            "missingProof": ["physical button semantics after suppression", "message ownership and recovery",
                             "2024 car-specific longitudinal response and factory safety-feature behavior"]}


def audit(path: Path, mode: str) -> dict:
  if mode not in ("eyesight-on", "eyesight-off"):
    raise ValueError("mode must be eyesight-on or eyesight-off")
  analyzer = StockResumeResearch() if mode == "eyesight-on" else EyeSightOffResearch()
  faults = set()
  seen = Counter()
  with warnings.catch_warnings():
    warnings.simplefilter("error", RuntimeWarning)
    for event in LogReader(str(path)):
      kind = event.which()
      seen[kind] += 1
      t = event.logMonoTime / 1e9
      if kind == "can":
        analyzer.update(t, [[CanData(c.address, bytes(c.dat), c.src) for c in event.can]])
      elif kind == "sendcan" and mode == "eyesight-off":
        for c in event.sendcan:
          analyzer.diagnostic(t, c.address, bytes(c.dat), c.src, outgoing=True)
      elif kind == "pandaStates":
        for panda in event.pandaStates:
          faults.update(str(fault) for fault in panda.faults)
      if kind in ("can", "pandaStates", "carState") and not event.valid:
        faults.add(f"invalid_{kind}_event")
  return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": mode,
          "modeLocked": True, "transmitEnabled": False, "roadReadinessEstablished": False,
          "observedPandaOrLogFaults": sorted(faults), "canEvents": seen["can"],
          "analysis": analyzer.result()}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--mode", required=True, choices=("eyesight-on", "eyesight-off"))
  parser.add_argument("logs", nargs="+", type=Path, help="complete, local rlogs (no device or network access)")
  args = parser.parse_args()
  for path in args.logs:
    if not path.is_file():
      parser.error(f"not a local file: {path}")
  print(json.dumps([audit(path, args.mode) for path in args.logs], indent=2))


if __name__ == "__main__":
  main()
