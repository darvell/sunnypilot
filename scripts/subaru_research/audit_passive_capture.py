#!/usr/bin/env python3
"""Qualify a local parked passive log for research, never for road readiness."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import warnings

from openpilot.tools.lib.logreader import LogReader


REQUIRED = {"carParams", "carState", "pandaStates", "can"}
STREAMS = {"carState", "pandaStates", "can"}


def audit(path: Path, minimum_seconds: float = 15.0, maximum_gap: float = 1.0) -> dict:
  if not all(math.isfinite(v) and v > 0 for v in (minimum_seconds, maximum_gap)):
    raise ValueError("observation duration and maximum gap must be finite and positive")
  failures = set()
  seen = set()
  times = {service: [] for service in STREAMS}
  buses = set()
  faults = set()
  commits = set()
  fingerprints = set()
  send_frames = 0
  previous_counters = None
  previous_uptimes = None
  # No silent partial decode: a corrupt suffix invalidates the capture.
  with warnings.catch_warnings():
    warnings.simplefilter("error", RuntimeWarning)
    for event in LogReader(str(path)):
      kind = event.which()
      if kind in REQUIRED:
        seen.add(kind)
        if not event.valid:
          failures.add(f"invalid:{kind}")
      if kind == "initData":
        commits.add(str(event.initData.gitCommit))
      elif kind == "sendcan":
        send_frames += len(event.sendcan)
      elif kind == "carParams":
        cp = event.carParams
        fingerprints.add(str(cp.carFingerprint))
        if not cp.passive or cp.openpilotLongitudinalControl or not cp.safetyConfigs or any(str(c.safetyModel) != "noOutput" for c in cp.safetyConfigs):
          failures.add("notPassiveConfiguration")
      elif kind == "carState":
        state = event.carState
        if not math.isfinite(state.vEgo) or abs(state.vEgo) >= 0.01 or str(state.gearShifter) != "park":
          failures.add("notParked")
        if not state.canValid or state.canTimeout:
          failures.add("carCanUnhealthy")
        if state.steerFaultPermanent or state.steerFaultTemporary or state.accFaulted:
          failures.add("vehicleFault")
      elif kind == "pandaStates":
        pandas = list(event.pandaStates)
        if not pandas:
          failures.add("missingPanda")
        for panda in pandas:
          faults.update(str(fault) for fault in panda.faults)
          if str(panda.safetyModel) != "noOutput" or panda.controlsAllowed or panda.controlsAllowedLateral or panda.controlsAllowedLongitudinal:
            failures.add("pandaNotPassive")
          if panda.faults or str(panda.faultStatus) != "none" or panda.heartbeatLost:
            failures.add("pandaFault")
          if str(panda.harnessStatus) != "normal":
            failures.add("harnessNotNormal")
          if any(s.busOff or s.errorWarning or s.errorPassive for s in (panda.canState0, panda.canState1, panda.canState2)):
            failures.add("busFault")
        counters = tuple((int(p.safetyTxBlocked), int(p.safetyRxInvalid), int(p.rxBufferOverflow), int(p.txBufferOverflow),
                          int(p.spiErrorCount), tuple((int(s.totalErrorCnt), int(s.totalRxLostCnt), int(s.totalTxLostCnt),
                                                     int(s.busOffCnt), int(s.canCoreResetCnt))
                                                    for s in (p.canState0, p.canState1, p.canState2))) for p in pandas)
        uptimes = tuple(int(p.uptime) for p in pandas)
        if previous_counters is not None and counters != previous_counters:
          failures.add("pandaCountersChanged")
        if previous_uptimes is not None and (len(uptimes) != len(previous_uptimes) or
                                             any(a < b for a, b in zip(uptimes, previous_uptimes, strict=True))):
          failures.add("pandaRestarted")
        previous_counters, previous_uptimes = counters, uptimes
      elif kind == "carControl":
        if event.carControl.enabled or event.carControl.latActive or event.carControl.longActive:
          failures.add("controlActive")
      elif kind == "can":
        physical = [frame for frame in event.can if frame.src in (0, 1, 2)]
        if not physical:
          continue
        buses.update(frame.src for frame in physical)
      if kind in STREAMS:
        times[kind].append(int(event.logMonoTime) / 1e9)
  failures.update(f"missing:{service}" for service in REQUIRED - seen)
  if send_frames:
    failures.add("sendcanObserved")
  if buses != {0, 1, 2}:
    failures.add("incompletePhysicalBusCoverage")
  coverage = {}
  for service, stamps in times.items():
    deltas = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    coverage[service] = {"samples": len(stamps), "durationSeconds": stamps[-1] - stamps[0] if stamps else 0,
                         "maximumGapSeconds": max(deltas, default=0)}
    if not stamps or any(delta < 0 for delta in deltas) or max(deltas, default=0) > maximum_gap:
      failures.add(f"incompleteStream:{service}")
  overlap = max(0.0, min(s[-1] for s in times.values()) - max(s[0] for s in times.values())) if all(times.values()) else 0.0
  if overlap < minimum_seconds:
    failures.add("incompleteObservationWindow")
  return {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
          "parkedPassiveEvidencePassed": not failures, "roadReadinessEstablished": False,
          "failures": sorted(failures), "faults": sorted(faults), "observedPhysicalBuses": sorted(buses),
          "sendcanFramesObserved": send_frames, "gitCommits": sorted(commits), "fingerprints": sorted(fingerprints),
          "commonStreamCoverageSeconds": overlap, "streams": coverage,
          "policy": {"minimumSeconds": minimum_seconds, "maximumStreamGapSeconds": maximum_gap},
          "limitations": ("Logged observations only. Missing publications can hide states or transmissions. Bus presence is not per-bus continuity. "
                          + "Commit strings do not identify dirty source or Panda firmware. No ECU diagnostic or factory-feature certification.")}


def main() -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("logs", type=Path, nargs="+")
  parser.add_argument("--minimum-seconds", type=float, default=15.0)
  parser.add_argument("--maximum-gap", type=float, default=1.0, help="research intake policy in seconds, not a vehicle timing specification")
  args = parser.parse_args()
  if not all(path.is_file() for path in args.logs):
    parser.error("inputs must be existing local files")
  if not all(math.isfinite(value) and value > 0 for value in (args.minimum_seconds, args.maximum_gap)):
    parser.error("durations must be finite and positive")
  results = [audit(path, args.minimum_seconds, args.maximum_gap) for path in args.logs]
  print(json.dumps(results, indent=2, sort_keys=True, allow_nan=False))
  return 0 if all(result["parkedPassiveEvidencePassed"] for result in results) else 1


if __name__ == "__main__":
  raise SystemExit(main())
