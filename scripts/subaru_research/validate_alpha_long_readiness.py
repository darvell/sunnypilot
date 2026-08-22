#!/usr/bin/env python3
import argparse
import json
import time

import openpilot.cereal.messaging as messaging

from openpilot.common.params import Params


EXPECTED_FINGERPRINT = "SUBARU_CROSSTREK_2025"
EXPECTED_SAFETY_MODEL = "subaru"
EXPECTED_SAFETY_PARAM = 11
STOCK_EYESIGHT_ADDRS = {0x124, 0x220, 0x221, 0x222}
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


def main() -> int:
  parser = argparse.ArgumentParser(description="Read-only parked readiness check for Crosstrek alpha longitudinal")
  parser.add_argument("--sample-seconds", type=float, default=15.0)
  parser.add_argument("--wait-timeout", type=float, default=60.0)
  args = parser.parse_args()
  if args.sample_seconds <= 0 or args.wait_timeout <= 0:
    parser.error("durations must be positive")

  params = Params()
  sm = messaging.SubMaster(["carParams", "carParamsSP", "carState", "pandaStates", "selfdriveState", "onroadEvents"])
  can_sock = messaging.sub_sock("can", timeout=100)

  required_services = set(sm.services)
  seen_services = set()
  stock_eyesight_frames = 0
  blocking_events = set()
  panda_faults = set()
  panda_safety_tx_first = None
  panda_safety_tx_last = None
  latest = {}

  started = time.monotonic()
  sample_started = None
  while True:
    sm.update(100)
    for service in sm.services:
      if sm.updated[service] and sm.valid[service]:
        seen_services.add(service)
        latest[service] = sm[service]

    for msg in messaging.drain_sock(can_sock):
      for frame in msg.can:
        if frame.src in (0, 1, 2) and frame.address in STOCK_EYESIGHT_ADDRS:
          stock_eyesight_frames += 1

    if sm.updated["onroadEvents"]:
      blocking_events.update(enum_name(event.name) for event in sm["onroadEvents"] if enum_name(event.name) in BLOCKING_EVENTS)

    if sm.updated["pandaStates"]:
      for panda in sm["pandaStates"]:
        panda_faults.update(enum_name(fault) for fault in panda.faults)
        if enum_name(panda.safetyModel) == EXPECTED_SAFETY_MODEL:
          if panda_safety_tx_first is None:
            panda_safety_tx_first = int(panda.safetyTxBlocked)
          panda_safety_tx_last = int(panda.safetyTxBlocked)

    now = time.monotonic()
    if sample_started is None and required_services <= seen_services:
      sample_started = now
    if sample_started is not None and now - sample_started >= args.sample_seconds:
      break
    if now - started >= args.wait_timeout:
      break

  checks = {}
  details = {
    "alphaLongitudinalEnabledParam": params.get_bool("AlphaLongitudinalEnabled"),
    "isOffroadParam": params.get_bool("IsOffroad"),
    "seenServices": sorted(seen_services),
    "stockEyeSightFrames": stock_eyesight_frames,
    "blockingEvents": sorted(blocking_events),
    "pandaFaults": sorted(panda_faults),
  }

  checks["allServicesSeen"] = required_services <= seen_services
  if checks["allServicesSeen"]:
    cp = latest["carParams"]
    cp_sp = latest["carParamsSP"]
    cs = latest["carState"]
    selfdrive = latest["selfdriveState"]
    pandas = list(latest["pandaStates"])
    subaru_pandas = [p for p in pandas if enum_name(p.safetyModel) == EXPECTED_SAFETY_MODEL]

    safety_configs = [(enum_name(config.safetyModel), int(config.safetyParam)) for config in cp.safetyConfigs]
    details.update({
      "fingerprint": str(cp.carFingerprint),
      "openpilotLongitudinalControl": bool(cp.openpilotLongitudinalControl),
      "alphaLongitudinalAvailable": bool(cp.alphaLongitudinalAvailable),
      "pcmCruise": bool(cp.pcmCruise),
      "autoResumeSng": bool(cp.autoResumeSng),
      "safetyConfigs": safety_configs,
      "carParamsSPFlags": int(cp_sp.flags),
      "carParamsSPSafetyParam": int(cp_sp.safetyParam),
      "gear": enum_name(cs.gearShifter),
      "vEgo": float(cs.vEgo),
      "canValid": bool(cs.canValid),
      "canTimeout": bool(cs.canTimeout),
      "steerFaultPermanent": bool(cs.steerFaultPermanent),
      "selfdriveEnabled": bool(selfdrive.enabled),
      "selfdriveActive": bool(selfdrive.active),
      "pandaSafetyTxBlockedFirst": panda_safety_tx_first,
      "pandaSafetyTxBlockedLast": panda_safety_tx_last,
    })

    checks.update({
      "alphaParamEnabled": params.get_bool("AlphaLongitudinalEnabled"),
      "fingerprint": str(cp.carFingerprint) == EXPECTED_FINGERPRINT,
      "longitudinalContract": bool(cp.alphaLongitudinalAvailable and cp.openpilotLongitudinalControl and not cp.pcmCruise and cp.autoResumeSng),
      "carParamsSafety": safety_configs == [(EXPECTED_SAFETY_MODEL, EXPECTED_SAFETY_PARAM)],
      "stockStopAndGoDisabled": int(cp_sp.flags) == 0 and int(cp_sp.safetyParam) == 0,
      "vehicleParked": enum_name(cs.gearShifter) == "park" and abs(float(cs.vEgo)) < 0.01,
      "pedalsReleased": not cs.brakePressed and not cs.gasPressed,
      "carCanHealthy": bool(cs.canValid and not cs.canTimeout),
      "steeringHealthy": not cs.steerFaultPermanent and not cs.steerFaultTemporary,
      "selfdriveDisabled": not selfdrive.enabled and not selfdrive.active,
      "subaruPandaPresent": len(subaru_pandas) == 1,
      "pandaSafety": len(subaru_pandas) == 1 and int(subaru_pandas[0].safetyParam) == EXPECTED_SAFETY_PARAM,
      "pandaHarness": len(subaru_pandas) == 1 and enum_name(subaru_pandas[0].harnessStatus) == "normal",
      "pandaHeartbeat": len(subaru_pandas) == 1 and not subaru_pandas[0].heartbeatLost,
      "pandaRxChecks": len(subaru_pandas) == 1 and not subaru_pandas[0].safetyRxChecksInvalid,
      "pandaBusHealth": len(subaru_pandas) == 1 and not any(state.busOff for state in (subaru_pandas[0].canState0,
                                                                                       subaru_pandas[0].canState1,
                                                                                       subaru_pandas[0].canState2)),
      "controlsDisallowed": len(subaru_pandas) == 1 and not subaru_pandas[0].controlsAllowed and
                            not subaru_pandas[0].controlsAllowedLateral and not subaru_pandas[0].controlsAllowedLongitudinal,
      "safetyTxStable": panda_safety_tx_first is not None and panda_safety_tx_last == panda_safety_tx_first,
      "stockEyeSightSilent": stock_eyesight_frames == 0,
      "noBlockingEvents": not blocking_events,
      "noRelayFault": "relayMalfunction" not in panda_faults,
    })

  ready = bool(checks) and all(checks.values())
  print(json.dumps({"ready": ready, "checks": checks, "details": details}, indent=2, sort_keys=True))
  return 0 if ready else 1


if __name__ == "__main__":
  raise SystemExit(main())
