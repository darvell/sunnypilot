from pathlib import Path
import json
import struct
import subprocess
import sys
import tempfile
import unittest

from scripts.subaru_research.meter_snapshot_decode import FIELDS, decode_file, decode_record


def frame(values=None):
  """Synthetic transport fixture, not a captured vehicle frame."""
  data = bytearray(400)
  data[7:17] = b"EXAMPLE001"
  data[17] = 2
  # Literal wire offsets are intentionally independent of the decoder's FIELDS table.
  for byte, value in (values or {}).items():
    data[byte] = value
  struct.pack_into("<I", data, 396, ~sum(data[:396]) & 0xFFFFFFFF)
  return bytes(data)


class TestMeterSnapshotDecoder(unittest.TestCase):
  def test_exact_bit_positions_and_overlaid_fields(self):
    values = {"cruiseMainLamp": 1, "cruiseSetLamp": 1, "setSpeedRaw": 80, "holdDisplay": 1,
              "cancelDisplayRaw": 9, "automaticDepartureIconRaw": 5, "automaticDeparturePatternRaw": 1, "iaccSupport": 1}
    result = decode_record(frame({246: 0xC0, 247: 80, 249: 0x10, 256: 9, 261: 0x15, 315: 2}), "uart400")
    self.assertEqual(result["fields"], values)
    self.assertEqual(result["communicationVersionRaw"], 2)
    self.assertIsNone(result["driverStatusFlags"])
    self.assertFalse(result["automaticResumeCapabilityEstablished"])
    self.assertFalse(result["physicalResumeInputEstablished"])
    self.assertFalse(result["transmitEnabled"])

  def test_every_field_ignores_neighbor_bits(self):
    for name, field in FIELDS.items():
      with self.subTest(field=name):
        data = bytearray(frame())
        data[field.byte] |= ~(field.mask << field.shift) & 255
        struct.pack_into("<I", data, 396, ~sum(data[:396]) & 0xFFFFFFFF)
        self.assertEqual(decode_record(bytes(data), "uart400")["fields"][name], 0)

  def test_driver_header_is_flags_not_time_and_does_not_hide_error_status(self):
    result = decode_record(struct.pack("<Q", 4) + frame({249: 0x10}), "metercom408")
    self.assertEqual(result["driverStatusFlags"], 4)
    self.assertFalse(result["driverStatusClear"])
    self.assertEqual(result["fields"]["holdDisplay"], 1)
    self.assertFalse(result["automaticResumeCapabilityEstablished"])
    self.assertTrue(decode_record(bytes(8) + frame(), "metercom408")["driverStatusClear"])

  def test_checksum_truncation_wrong_format_and_alignment_fail(self):
    data = bytearray(frame())
    data[249] ^= 16
    with self.assertRaisesRegex(ValueError, "checksum mismatch"):
      decode_record(bytes(data), "uart400")
    for record, fmt in ((frame()[:-1], "uart400"), (frame(), "metercom408"), (bytes(8) + frame(), "uart400")):
      with self.subTest(size=len(record), format=fmt):
        with self.assertRaisesRegex(ValueError, "incomplete"):
          decode_record(record, fmt)
    with self.assertRaisesRegex(ValueError, "format must"):
      decode_record(frame(), "guess")

  def test_saved_binary_records_keep_each_record_and_reject_partial_suffix(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "snapshots.bin"
      path.write_bytes(frame({249: 0x10}) + frame({261: 3}))
      result = list(decode_file(path, "uart400"))
      self.assertEqual([r["recordIndex"] for r in result], [0, 1])
      self.assertEqual([r["fields"]["holdDisplay"] for r in result], [1, 0])
      self.assertEqual(result[1]["fields"]["automaticDepartureIconRaw"], 3)
      path.write_bytes(path.read_bytes() + b"X")
      with self.assertRaisesRegex(ValueError, "complete aligned"):
        list(decode_file(path, "uart400"))

  def test_cli_decodes_saved_records_and_exits_unsuccessfully_on_corruption(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "capture.bin"
      command = [sys.executable, "-m", "scripts.subaru_research.meter_snapshot_decode", str(path), "--format", "metercom408"]
      path.write_bytes(struct.pack("<Q", 4) + frame({261: 0x16}))
      result = subprocess.run(command, capture_output=True, text=True, check=True)
      decoded = json.loads(result.stdout)
      self.assertEqual(decoded["fields"]["automaticDepartureIconRaw"], 6)
      self.assertEqual(decoded["fields"]["automaticDeparturePatternRaw"], 1)
      self.assertEqual(decoded["driverStatusFlags"], 4)
      self.assertFalse(decoded["transmitEnabled"])
      damaged = bytearray(path.read_bytes())
      damaged[8 + 261] ^= 1
      path.write_bytes(damaged)
      result = subprocess.run(command, capture_output=True, text=True)
      self.assertNotEqual(result.returncode, 0)
      self.assertEqual(result.stdout, "")
      self.assertIn("checksum mismatch", result.stderr)

  def test_device_nodes_are_not_capture_inputs(self):
    with self.assertRaisesRegex(ValueError, "saved regular file"):
      list(decode_file(Path("/dev/null"), "uart400"))


if __name__ == "__main__":
  unittest.main()
