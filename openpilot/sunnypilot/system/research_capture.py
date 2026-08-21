#!/usr/bin/env python3
import csv
import datetime
import errno
import gzip
import json
import math
import os
import shutil
from pathlib import Path

from opendbc.can import CANDefine, CANParser
from opendbc.can.dbc import DBC
from opendbc.car.structs import car

import openpilot.cereal.messaging as messaging
from openpilot.common.hardware import HARDWARE
from openpilot.common.hardware.hw import Paths
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.system.loggerd.deleter import PRESERVE_ATTR_NAME, PRESERVE_ATTR_VALUE
from openpilot.system.loggerd.xattr_cache import setxattr
from openpilot.tools.lib.logreader import LogReader


CAPTURE_FORMAT_VERSION = 1
DEFAULT_SEGMENTS_BEFORE = 2
DEFAULT_SEGMENTS_AFTER = 1
MAX_COMPLETED_CAPTURES = 5
SUBARU_DBC = "subaru_global_2017_generated"

SAFE_PARAM_KEYS = (
  "Version",
  "GitCommit",
  "GitCommitDate",
  "GitBranch",
  "GitRemote",
  "IsDevelopmentBranch",
  "IsTestedBranch",
  "IsReleaseBranch",
  "IsReleaseSpBranch",
  "AlphaLongitudinalEnabled",
  "ExperimentalMode",
  "DisableDriverMonitoring",
  "Mads",
  "IsMetric",
)


def parse_segment_name(name: str) -> tuple[str, int] | None:
  route, separator, segment = name.rpartition("--")
  if not separator or not route:
    return None
  try:
    return route, int(segment)
  except ValueError:
    return None


def capture_segment_numbers(trigger_segment: int, segments_before: int, segments_after: int) -> list[int]:
  return list(range(max(0, trigger_segment - segments_before), trigger_segment + segments_after + 1))


def segment_path(log_root: Path, route: str, segment: int) -> Path:
  return log_root / f"{route}--{segment}"


def available_route_segments(log_root: Path, route: str) -> dict[int, Path]:
  segments = {}
  for path in log_root.glob(f"{route}--*"):
    parsed = parse_segment_name(path.name)
    if path.is_dir() and parsed is not None and parsed[0] == route:
      segments[parsed[1]] = path
  return segments


def latest_route_segment(log_root: Path, route: str) -> int | None:
  segments = available_route_segments(log_root, route)
  return max(segments) if segments else None


def segment_closed(path: Path) -> bool:
  return path.is_dir() and not any(file.name.endswith(".lock") for file in path.iterdir())


def preserve_segment(path: Path) -> None:
  if path.is_dir():
    setxattr(str(path), PRESERVE_ATTR_NAME, PRESERVE_ATTR_VALUE)


def _write_json_atomic(path: Path, value: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temp_path = path.with_suffix(path.suffix + ".tmp")
  temp_path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
  os.replace(temp_path, path)


def _link_or_copy(source: Path, destination: Path) -> str:
  destination.parent.mkdir(parents=True, exist_ok=True)
  try:
    os.link(source, destination)
    return "hardlink"
  except OSError as error:
    if error.errno not in (errno.EXDEV, errno.EPERM, errno.EACCES, errno.EMLINK):
      raise
  shutil.copy2(source, destination)
  return "copy"


def _safe_params(params: Params) -> dict:
  values = {}
  for key in SAFE_PARAM_KEYS:
    try:
      value = params.get(key)
    except Exception:
      continue
    if value is not None:
      values[key] = value
  return values


def _car_metadata(params: Params) -> dict:
  cp_bytes = params.get("CarParamsPersistent")
  if cp_bytes is None:
    return {}
  try:
    CP = messaging.log_from_bytes(cp_bytes, car.CarParams)
  except Exception:
    cloudlog.exception("research capture failed to decode CarParamsPersistent")
    return {}
  return {
    "fingerprint": CP.carFingerprint,
    "brand": CP.brand,
    "flags": int(CP.flags),
    "openpilotLongitudinalControl": CP.openpilotLongitudinalControl,
    "alphaLongitudinalAvailable": CP.alphaLongitudinalAvailable,
    "steerControlType": str(CP.steerControlType),
  }


def _iter_events(segment_paths: list[Path]):
  for path in segment_paths:
    rlog = path / "rlog.zst"
    if not rlog.is_file():
      continue
    for event in LogReader(str(rlog), sort_by_time=True):
      yield path.name, event


def export_raw_can(segment_paths: list[Path], output_path: Path, service: str) -> int:
  fieldnames = ["logMonoTime", "time_s", "segment", "src", "address", "address_hex", "size", "data_hex"]
  rows = 0
  start_time = None
  with gzip.open(output_path, "wt", newline="") as output:
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for segment, event in _iter_events(segment_paths):
      if event.which() != service:
        continue
      if start_time is None:
        start_time = event.logMonoTime
      for frame in getattr(event, service):
        data = bytes(frame.dat)
        writer.writerow({
          "logMonoTime": event.logMonoTime,
          "time_s": (event.logMonoTime - start_time) * 1e-9,
          "segment": segment,
          "src": frame.src,
          "address": frame.address,
          "address_hex": f"0x{frame.address:X}",
          "size": len(data),
          "data_hex": data.hex(),
        })
        rows += 1
  return rows


def _enum_name(defines: CANDefine, address: int, signal: str, value: float) -> str:
  if not float(value).is_integer():
    return ""
  return defines.dv.get(address, {}).get(signal, {}).get(int(value), "")


def export_decoded_subaru(segment_paths: list[Path], output_path: Path, dbc_name: str = SUBARU_DBC) -> int:
  dbc = DBC(dbc_name)
  messages = [(message.name, math.nan) for message in dbc.msgs.values()]
  parsers = {bus: CANParser(dbc_name, messages, bus) for bus in (0, 1, 2)}
  defines = CANDefine(dbc_name)
  fieldnames = ["logMonoTime", "time_s", "segment", "direction", "src", "address", "address_hex", "message", "signal", "value", "enum"]
  rows = 0
  start_time = None

  with gzip.open(output_path, "wt", newline="") as output:
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for segment, event in _iter_events(segment_paths):
      service = event.which()
      if service not in ("can", "sendcan"):
        continue
      if start_time is None:
        start_time = event.logMonoTime
      frames = [(frame.address, bytes(frame.dat), frame.src) for frame in getattr(event, service)]
      for bus, parser in parsers.items():
        updated = parser.update([event.logMonoTime, frames], sendcan=service == "sendcan")
        for address in sorted(updated):
          message = parser.dbc.addr_to_msg[address]
          for signal, value in sorted(parser.vl[address].items()):
            writer.writerow({
              "logMonoTime": event.logMonoTime,
              "time_s": (event.logMonoTime - start_time) * 1e-9,
              "segment": segment,
              "direction": "rx" if service == "can" else "tx",
              "src": bus,
              "address": address,
              "address_hex": f"0x{address:X}",
              "message": message.name,
              "signal": signal,
              "value": value,
              "enum": _enum_name(defines, address, signal, value),
            })
            rows += 1
  return rows


def export_capture(request: dict, log_root: Path, capture_root: Path, params: Params | None = None) -> Path:
  params = params or Params()
  route = request["route"]
  desired_segments = capture_segment_numbers(request["trigger_segment"], request["segments_before"], request["segments_after"])
  segment_paths = [segment_path(log_root, route, segment) for segment in desired_segments]
  segment_paths = [path for path in segment_paths if segment_closed(path) and (path / "rlog.zst").is_file()]
  if not segment_paths:
    raise RuntimeError(f"no closed rlog segments available for research capture {route}")

  capture_id = request["capture_id"]
  destination = capture_root / capture_id
  partial = capture_root / f".{capture_id}.partial"
  if destination.exists():
    return destination
  shutil.rmtree(partial, ignore_errors=True)
  partial.mkdir(parents=True)

  archived_files = []
  for source_segment in segment_paths:
    destination_segment = partial / "segments" / source_segment.name
    for source in sorted(source_segment.iterdir()):
      if not source.is_file() or source.name.endswith(".lock"):
        continue
      destination_file = destination_segment / source.name
      method = _link_or_copy(source, destination_file)
      archived_files.append({
        "segment": source_segment.name,
        "name": source.name,
        "size": source.stat().st_size,
        "method": method,
      })

  rx_rows = export_raw_can(segment_paths, partial / "can.csv.gz", "can")
  tx_rows = export_raw_can(segment_paths, partial / "sendcan.csv.gz", "sendcan")
  decoded_rows = export_decoded_subaru(segment_paths, partial / "subaru_signals.csv.gz")

  manifest = {
    "formatVersion": CAPTURE_FORMAT_VERSION,
    "captureId": capture_id,
    "createdAt": datetime.datetime.now(datetime.UTC).isoformat(),
    "triggerLogMonoTime": request["trigger_log_mono_time"],
    "triggerCount": request.get("trigger_count", 1),
    "route": route,
    "triggerSegment": request["trigger_segment"],
    "requestedSegments": desired_segments,
    "includedSegments": [path.name for path in segment_paths],
    "segmentWindow": {"before": request["segments_before"], "after": request["segments_after"]},
    "busMap": {"0": "main/powertrain", "1": "alternate", "2": "EyeSight/camera"},
    "dbc": SUBARU_DBC,
    "rows": {"can": rx_rows, "sendcan": tx_rows, "decodedSubaruSignals": decoded_rows},
    "buildAndSettings": _safe_params(params),
    "car": _car_metadata(params),
    "deviceType": HARDWARE.get_device_type(),
    "files": archived_files,
  }
  _write_json_atomic(partial / "manifest.json", manifest)
  os.replace(partial, destination)
  return destination


class ResearchCaptureManager:
  def __init__(self, log_root: Path | None = None, capture_root: Path | None = None, params: Params | None = None):
    self.log_root = log_root or Path(Paths.log_root())
    self.capture_root = capture_root or Path(Paths.research_capture_root())
    self.pending_root = self.capture_root / ".pending"
    self.params = params or Params()
    self.capture_root.mkdir(parents=True, exist_ok=True)
    self.pending_root.mkdir(parents=True, exist_ok=True)

  def _request_path(self, capture_id: str) -> Path:
    return self.pending_root / f"{capture_id}.json"

  def record_trigger(self, trigger_log_mono_time: int) -> dict | None:
    route = self.params.get("CurrentRoute")
    if not route:
      cloudlog.warning("research capture ignored: CurrentRoute unavailable")
      return None
    trigger_segment = latest_route_segment(self.log_root, route)
    if trigger_segment is None:
      cloudlog.warning(f"research capture ignored: no segments for {route}")
      return None

    capture_id = f"{route}--research-{trigger_segment:03d}"
    request_path = self._request_path(capture_id)
    if request_path.is_file():
      request = json.loads(request_path.read_text())
      request["trigger_count"] = request.get("trigger_count", 1) + 1
      request["trigger_log_mono_time"] = trigger_log_mono_time
    else:
      request = {
        "capture_id": capture_id,
        "route": route,
        "trigger_segment": trigger_segment,
        "trigger_log_mono_time": trigger_log_mono_time,
        "trigger_count": 1,
        "segments_before": DEFAULT_SEGMENTS_BEFORE,
        "segments_after": DEFAULT_SEGMENTS_AFTER,
      }
    _write_json_atomic(request_path, request)
    self._preserve_available(request)
    self.params.put("ResearchCaptureState", f"queued:{capture_id}")
    return request

  def _preserve_available(self, request: dict) -> None:
    for segment in capture_segment_numbers(request["trigger_segment"], request["segments_before"], request["segments_after"]):
      path = segment_path(self.log_root, request["route"], segment)
      if path.is_dir():
        try:
          preserve_segment(path)
        except OSError:
          cloudlog.exception(f"research capture failed to preserve {path}")

  def _ready(self, request: dict) -> bool:
    self._preserve_available(request)
    final_path = segment_path(self.log_root, request["route"], request["trigger_segment"] + request["segments_after"])
    if segment_closed(final_path) and (final_path / "rlog.zst").is_file():
      return True
    return self.params.get_bool("IsOffroad") and segment_closed(segment_path(self.log_root, request["route"], request["trigger_segment"]))

  def process_pending(self) -> list[Path]:
    completed = []
    for request_path in sorted(self.pending_root.glob("*.json")):
      try:
        request = json.loads(request_path.read_text())
        if not self._ready(request):
          continue
        destination = export_capture(request, self.log_root, self.capture_root, self.params)
        request_path.unlink()
        self.params.put("ResearchCaptureLastPath", str(destination))
        self.params.put("ResearchCaptureState", f"complete:{destination.name}")
        completed.append(destination)
      except Exception:
        cloudlog.exception(f"research capture export failed for {request_path}")
        self.params.put("ResearchCaptureState", f"error:{request_path.stem}")
    if completed:
      self.prune_completed()
    return completed

  def prune_completed(self) -> None:
    completed = sorted((path for path in self.capture_root.iterdir() if path.is_dir() and not path.name.startswith(".") and
                        (path / "manifest.json").is_file()), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in completed[MAX_COMPLETED_CAPTURES:]:
      shutil.rmtree(path)


def main() -> None:
  manager = ResearchCaptureManager()
  sm = messaging.SubMaster(["userBookmark"], poll="userBookmark")
  while True:
    sm.update(1000)
    if sm.updated["userBookmark"]:
      manager.record_trigger(sm.logMonoTime["userBookmark"])
    manager.process_pending()


if __name__ == "__main__":
  main()
