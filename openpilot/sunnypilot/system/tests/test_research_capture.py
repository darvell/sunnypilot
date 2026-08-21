import csv
import gzip
import json
import os
from pathlib import Path

import openpilot.cereal.messaging as messaging
from openpilot.sunnypilot.system.research_capture import (
  ResearchCaptureManager,
  capture_segment_numbers,
  export_capture,
  parse_segment_name,
)
from openpilot.tools.lib.logreader import save_log


class FakeParams:
  def __init__(self, values=None):
    self.values = values or {}

  def get(self, key):
    return self.values.get(key)

  def get_bool(self, key):
    return bool(self.values.get(key, False))

  def put(self, key, value):
    self.values[key] = value


def can_message(service: str, mono_time: int, address: int, data: bytes, src: int):
  msg = messaging.new_message(service, 1, valid=True, logMonoTime=mono_time)
  frame = getattr(msg, service)[0]
  frame.address = address
  frame.dat = data
  frame.src = src
  return msg.as_reader()


def make_segment(log_root: Path, route: str, segment: int, mono_time: int) -> Path:
  path = log_root / f"{route}--{segment}"
  path.mkdir(parents=True)
  messages = [
    can_message("can", mono_time, 0x228, b"\x00\x01\x02\x03\x04\x05\x06\x07", 0),
    can_message("sendcan", mono_time + 1, 0x122, b"\x10\x11\x12\x13\x14\x15\x16\x17", 2),
  ]
  save_log(str(path / "rlog.zst"), messages)
  (path / "fcamera.hevc").write_bytes(b"road-video")
  return path


def test_parse_segment_name():
  assert parse_segment_name("00000001--abcdef1234--12") == ("00000001--abcdef1234", 12)
  assert parse_segment_name("boot") is None
  assert parse_segment_name("route--bad") is None


def test_capture_segment_numbers_clamps_at_zero():
  assert capture_segment_numbers(1, 2, 1) == [0, 1, 2]
  assert capture_segment_numbers(5, 2, 1) == [3, 4, 5, 6]


def test_export_capture_archives_segments_and_can(tmp_path):
  log_root = tmp_path / "logs"
  capture_root = tmp_path / "captures"
  route = "00000001--abcdef1234"
  for segment in range(3):
    make_segment(log_root, route, segment, 1_000_000_000 + segment * 60_000_000_000)

  request = {
    "capture_id": f"{route}--research-001",
    "route": route,
    "trigger_segment": 1,
    "trigger_log_mono_time": 61_000_000_000,
    "trigger_count": 1,
    "segments_before": 1,
    "segments_after": 1,
  }
  destination = export_capture(request, log_root, capture_root, FakeParams({"GitCommit": "test-commit"}))

  manifest = json.loads((destination / "manifest.json").read_text())
  assert manifest["includedSegments"] == [f"{route}--0", f"{route}--1", f"{route}--2"]
  assert manifest["rows"]["can"] == 3
  assert manifest["rows"]["sendcan"] == 3
  assert manifest["buildAndSettings"]["GitCommit"] == "test-commit"

  with gzip.open(destination / "can.csv.gz", "rt", newline="") as stream:
    rows = list(csv.DictReader(stream))
  assert [row["address_hex"] for row in rows] == ["0x228", "0x228", "0x228"]
  assert [row["src"] for row in rows] == ["0", "0", "0"]

  archived = destination / "segments" / f"{route}--1" / "fcamera.hevc"
  assert archived.read_bytes() == b"road-video"
  assert os.stat(archived).st_ino == os.stat(log_root / f"{route}--1" / "fcamera.hevc").st_ino


def test_manager_queues_deduplicates_and_exports_offroad(tmp_path):
  log_root = tmp_path / "logs"
  capture_root = tmp_path / "captures"
  route = "00000002--abcdef1234"
  for segment in range(2):
    make_segment(log_root, route, segment, 1_000_000_000 + segment * 60_000_000_000)

  params = FakeParams({"CurrentRoute": route, "IsOffroad": False})
  manager = ResearchCaptureManager(log_root, capture_root, params)
  first = manager.record_trigger(61_000_000_000)
  second = manager.record_trigger(62_000_000_000)
  assert first is not None and second is not None
  assert len(list(manager.pending_root.glob("*.json"))) == 1
  queued = json.loads(next(manager.pending_root.glob("*.json")).read_text())
  assert queued["trigger_count"] == 2
  assert manager.process_pending() == []

  params.values["IsOffroad"] = True
  completed = manager.process_pending()
  assert len(completed) == 1
  assert (completed[0] / "manifest.json").is_file()
  assert params.values["ResearchCaptureState"].startswith("complete:")
