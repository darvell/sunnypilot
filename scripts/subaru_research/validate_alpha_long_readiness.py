#!/usr/bin/env python3
import argparse
import json
import math
import time

import openpilot.cereal.messaging as messaging

from openpilot.cereal.services import SERVICE_LIST
from openpilot.common.params import Params


EXPECTED_FINGERPRINT = "SUBARU_CROSSTREK_2025"
EXPECTED_SAFETY_MODEL = "subaru"
EXPECTED_SAFETY_PARAM = 11
STOCK_EYESIGHT_ADDRS = {0x124, 0x220, 0x221, 0x222}
CAN_MAX_AGE_NS = int(10 / SERVICE_LIST["can"].frequency * 1e9)
BLOCKING_EVENTS = {
  "canBusMissing",
  "canError",
  "commIssue",
  "commIssueAvgFreq",
  "controlsMismatch",
  "relayMalfunction",
  "steerUnavailable",
}


def enum_name(value) -> str:
  return str(value).rsplit(".", 1)[-1]


def telemetry_checks(sm, seen_services):
  """Check every observed service, including observations before startup completes."""
  checks, details = {}, {}
  if "carParams" in seen_services:
    cp = sm["carParams"]
    safety_configs = [(enum_name(config.safetyModel), int(config.safetyParam)) for config in cp.safetyConfigs]
    checks.update({
      "fingerprint": str(cp.carFingerprint) == EXPECTED_FINGERPRINT,
      "longitudinalContract": bool(cp.alphaLongitudinalAvailable and cp.openpilotLongitudinalControl and not cp.pcmCruise and cp.autoResumeSng),
      "carParamsSafety": safety_configs == [(EXPECTED_SAFETY_MODEL, EXPECTED_SAFETY_PARAM)],
    })
    details.update({
      "fingerprint": str(cp.carFingerprint),
      "openpilotLongitudinalControl": bool(cp.openpilotLongitudinalControl),
      "alphaLongitudinalAvailable": bool(cp.alphaLongitudinalAvailable),
      "pcmCruise": bool(cp.pcmCruise),
      "autoResumeSng": bool(cp.autoResumeSng),
      "safetyConfigs": safety_configs,
    })
  if "carParamsSP" in seen_services:
    cp_sp = sm["carParamsSP"]
    checks["stockStopAndGoDisabled"] = int(cp_sp.flags) == 0 and int(cp_sp.safetyParam) == 0
    details.update({"carParamsSPFlags": int(cp_sp.flags), "carParamsSPSafetyParam": int(cp_sp.safetyParam)})
  if "carState" in seen_services:
    cs = sm["carState"]
    checks.update({
      "vehicleParked": enum_name(cs.gearShifter) == "park" and math.isfinite(cs.vEgo) and abs(float(cs.vEgo)) < 0.01,
      "pedalsReleased": not cs.brakePressed and not cs.gasPressed,
      "carCanHealthy": bool(cs.canValid and not cs.canTimeout),
      "steeringHealthy": not cs.steerFaultPermanent and not cs.steerFaultTemporary,
      "accHealthy": not cs.accFaulted,
    })
    details.update({
      "gear": enum_name(cs.gearShifter), "vEgo": float(cs.vEgo) if math.isfinite(cs.vEgo) else None,
      "canValid": bool(cs.canValid), "canTimeout": bool(cs.canTimeout), "steerFaultPermanent": bool(cs.steerFaultPermanent),
    })
  if "selfdriveState" in seen_services:
    selfdrive = sm["selfdriveState"]
    checks["selfdriveDisabled"] = not selfdrive.enabled and not selfdrive.active
    details.update({"selfdriveEnabled": bool(selfdrive.enabled), "selfdriveActive": bool(selfdrive.active)})
  if "pandaStates" in seen_services:
    pandas = list(sm["pandaStates"])
    subaru_pandas = [p for p in pandas if enum_name(p.safetyModel) == EXPECTED_SAFETY_MODEL]
    checks.update({
      "subaruPandaPresent": len(subaru_pandas) == 1,
      "pandaSafety": len(subaru_pandas) == 1 and int(subaru_pandas[0].safetyParam) == EXPECTED_SAFETY_PARAM,
      "pandaHarness": len(subaru_pandas) == 1 and enum_name(subaru_pandas[0].harnessStatus) == "normal",
      "pandaHeartbeat": bool(pandas) and all(not p.heartbeatLost for p in pandas),
      "pandaRxChecks": bool(pandas) and all(not p.safetyRxChecksInvalid for p in pandas),
      "pandaBusHealth": bool(pandas) and all(not (state.busOff or state.errorWarning or state.errorPassive)
                                           for p in pandas for state in (p.canState0, p.canState1, p.canState2)),
      "controlsDisallowed": bool(pandas) and all(not p.controlsAllowed and not p.controlsAllowedLateral and
                                                not p.controlsAllowedLongitudinal for p in pandas),
      "noPandaFaults": bool(pandas) and all(not p.faults and enum_name(p.faultStatus) == "none" for p in pandas),
    })
  return checks, details


def main() -> int:
  parser = argparse.ArgumentParser(description="Read-only parked telemetry check; does not establish road readiness or ECU diagnostic acceptance")
  parser.add_argument("--sample-seconds", type=float, default=15.0, help="required continuous observation window")
  parser.add_argument("--wait-timeout", type=float, default=60.0, help="total deadline, including the observation window")
  args = parser.parse_args()
  if not all(math.isfinite(value) and value > 0 for value in (args.sample_seconds, args.wait_timeout)):
    parser.error("durations must be finite and positive")

  params = Params()
  sm = messaging.SubMaster(["carParams", "carParamsSP", "carState", "pandaStates", "selfdriveState", "onroadEvents"])
  can_sock = messaging.sub_sock("can", timeout=100)
  required_services = set(sm.services)
  seen_services = set()
  checks = {}
  details = {}
  failures = set()
  blocking_events = set()
  panda_faults = set()
  stock_eyesight_frames = 0
  physical_can_frames = 0
  physical_can_buses = set()
  last_physical_can_ns = None
  previous_counters = None
  previous_uptimes = None
  panda_safety_tx_first = None
  panda_safety_tx_last = None
  started = time.monotonic()
  sample_started = None
  observed_seconds = 0.0
  complete = False

  while True:
    sm.update(100)
    now = time.monotonic()
    now_ns = int(now * 1e9)
    for service in sm.services:
      if sm.updated[service]:
        seen_services.add(service)
        if not sm.valid[service]:
          failures.add(f"serviceValid:{service}")
      # Unseen services may still be starting. Once seen, loss is retained.
      if service in seen_services and not sm.alive[service]:
        failures.add(f"serviceAlive:{service}")

    for msg in messaging.drain_sock(can_sock):
      physical = [frame for frame in msg.can if frame.src in (0, 1, 2)]
      stock_eyesight_frames += sum(frame.address in STOCK_EYESIGHT_ADDRS for frame in physical)
      if not msg.valid:
        failures.add("canMessagesValid")
      # Source 128+ is a TX return/rejection, not physical receive evidence.
      # Require this run's timestamps and at most 100 ms age (CAN's 10/frequency alive contract).
      if physical and msg.valid and int(started * 1e9) <= msg.logMonoTime <= now_ns and now_ns - msg.logMonoTime <= CAN_MAX_AGE_NS:
        if last_physical_can_ns is not None and msg.logMonoTime - last_physical_can_ns > CAN_MAX_AGE_NS:
          failures.add("physicalCanFresh")
        physical_can_frames += len(physical)
        physical_can_buses.update(frame.src for frame in physical)
        last_physical_can_ns = max(last_physical_can_ns or 0, msg.logMonoTime)

    can_fresh = last_physical_can_ns is not None and now_ns - last_physical_can_ns <= CAN_MAX_AGE_NS
    if last_physical_can_ns is not None and not can_fresh:
      failures.add("physicalCanFresh")
    if sm.updated["onroadEvents"]:
      blocking_events.update(enum_name(event.name) for event in sm["onroadEvents"] if enum_name(event.name) in BLOCKING_EVENTS)
    if sm.updated["pandaStates"]:
      pandas = list(sm["pandaStates"])
      for panda in pandas:
        panda_faults.update(enum_name(fault) for fault in panda.faults)
      # Compare each update, not just endpoints: an increase followed by a
      # reset must not erase evidence. Changes in inventory also fail closed.
      counters = tuple((enum_name(p.safetyModel), int(p.safetyTxBlocked), int(p.safetyRxInvalid),
                        int(p.rxBufferOverflow), int(p.txBufferOverflow), int(p.spiErrorCount),
                        tuple((int(s.busOffCnt), int(s.totalErrorCnt), int(s.totalTxLostCnt), int(s.totalRxLostCnt), int(s.canCoreResetCnt))
                              for s in (p.canState0, p.canState1, p.canState2)))
                       for p in pandas)
      uptimes = tuple(int(p.uptime) for p in pandas)
      if previous_counters is not None:
        if counters != previous_counters:
          failures.add("pandaCountersStable")
        if tuple(c[1] for c in counters) != tuple(c[1] for c in previous_counters):
          failures.add("safetyTxStable")
        if len(uptimes) != len(previous_uptimes) or any(current < previous for current, previous in zip(uptimes, previous_uptimes, strict=True)):
          failures.add("pandaUptimeMonotonic")
      previous_counters, previous_uptimes = counters, uptimes
      subaru_pandas = [p for p in pandas if enum_name(p.safetyModel) == EXPECTED_SAFETY_MODEL]
      if len(subaru_pandas) == 1:
        panda_safety_tx_last = int(subaru_pandas[0].safetyTxBlocked)
        if panda_safety_tx_first is None:
          panda_safety_tx_first = panda_safety_tx_last

    current_checks, current_details = telemetry_checks(sm, seen_services)
    alpha_enabled = params.get_bool("AlphaLongitudinalEnabled")
    current_checks["alphaParamEnabled"] = alpha_enabled
    failures.update(name for name, passed in current_checks.items() if not passed)
    checks.update(current_checks)
    details.update(current_details)
    services_healthy = required_services <= seen_services and all(sm.alive[s] and sm.valid[s] for s in sm.services)
    if services_healthy and can_fresh:
      if sample_started is None:
        sample_started = now
      observed_seconds = now - sample_started
      complete = observed_seconds >= args.sample_seconds and now - started <= args.wait_timeout
    else:
      sample_started = None
      observed_seconds = 0.0
    if complete or now - started >= args.wait_timeout:
      break

  checks.update({
    "allServicesSeen": required_services <= seen_services,
    "servicesAliveAndValid": services_healthy,
    "completeObservationWindow": complete,
    "physicalCanFresh": can_fresh and physical_can_frames > 0,
    "stockEyeSightSilent": complete and physical_can_frames > 0 and stock_eyesight_frames == 0,
    "safetyTxStable": panda_safety_tx_first is not None,
    "pandaCountersStable": previous_counters is not None,
    "pandaUptimeMonotonic": previous_uptimes is not None,
    "canMessagesValid": True,
    "noBlockingEvents": not blocking_events,
    "noRelayFault": "relayMalfunction" not in panda_faults,
  })
  failures.update(name for name, passed in checks.items() if not passed)
  for name in failures:
    checks[name] = False
  details.update({
    "alphaLongitudinalEnabledParam": alpha_enabled,
    "isOffroadParam": params.get_bool("IsOffroad"),
    "seenServices": sorted(seen_services),
    "stockEyeSightFrames": stock_eyesight_frames,
    "physicalCanFrames": physical_can_frames,
    "physicalCanBuses": sorted(physical_can_buses),
    "blockingEvents": sorted(blocking_events),
    "pandaFaults": sorted(panda_faults),
    "pandaSafetyTxBlockedFirst": panda_safety_tx_first,
    "pandaSafetyTxBlockedLast": panda_safety_tx_last,
    "requestedSampleSeconds": args.sample_seconds,
    "continuousObservedSeconds": observed_seconds,
    "failedChecks": sorted(failures),
  })
  passed = all(checks.values())
  print(json.dumps({
    "ready": passed,  # Compatibility alias, scoped explicitly to parked telemetry below.
    "parkedTelemetryPassed": passed,
    "roadReadinessEstablished": False,
    "ecuDiagnosticAcceptance": "not observed by this subscriber",
    "scope": "Parked telemetry only. No road, actuation, or ECU diagnostic acceptance certification.",
    "observationLimits": "SubMaster snapshots can omit intermediate publications; CAN evidence covers only observed physical buses.",
    "checks": checks, "details": details,
  }, indent=2, sort_keys=True, allow_nan=False))
  return 0 if passed else 1


if __name__ == "__main__":
  raise SystemExit(main())
