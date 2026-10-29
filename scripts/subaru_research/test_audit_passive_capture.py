import math
from pathlib import Path
import tempfile
import unittest

from openpilot.cereal import messaging
from openpilot.tools.lib.logreader import save_log
from scripts.subaru_research.audit_passive_capture import audit


def messages():
  cp = messaging.new_message("carParams", valid=True, logMonoTime=1_000_000_000)
  cp.carParams.passive = True
  cp.carParams.carFingerprint = "SUBARU_CROSSTREK_2025"
  cp.carParams.safetyConfigs = [{"safetyModel": "noOutput"}]
  result = [cp]
  for tick in range(5):
    now = 1_000_000_000 + tick * 500_000_000
    cs = messaging.new_message("carState", valid=True, logMonoTime=now)
    cs.carState.canValid = True
    cs.carState.gearShifter = "park"
    ps = messaging.new_message("pandaStates", 1, valid=True, logMonoTime=now)
    ps.pandaStates[0].safetyModel = "noOutput"
    ps.pandaStates[0].harnessStatus = "normal"
    can = messaging.new_message("can", 3, valid=True, logMonoTime=now)
    for bus in range(3):
      can.can[bus].src = bus
      can.can[bus].address = 0x100
      can.can[bus].dat = b"\x00"
    result.extend([cs, ps, can])
  return result


class TestPassiveCapture(unittest.TestCase):
  def run_audit(self, events, minimum=1.5):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "rlog.zst"
      save_log(str(path), [event.as_reader() for event in events])
      return audit(path, minimum_seconds=minimum)

  def test_real_compressed_log_healthy_baseline(self):
    result = self.run_audit(messages())
    self.assertTrue(result["parkedPassiveEvidencePassed"])
    self.assertFalse(result["roadReadinessEstablished"])
    self.assertEqual(result["commonStreamCoverageSeconds"], 2.0)
    self.assertEqual(result["observedPhysicalBuses"], [0, 1, 2])
    self.assertEqual(len(result["sha256"]), 64)

  def test_transient_fault_and_motion_remain_blocking(self):
    for field, value, failure in (("vEgo", 1.0, "notParked"), ("vEgo", math.nan, "notParked"),
                                  ("accFaulted", True, "vehicleFault"), ("steerFaultPermanent", True, "vehicleFault")):
      with self.subTest(field=field):
        events = messages()
        setattr(events[4].carState, field, value)
        self.assertIn(failure, self.run_audit(events)["failures"])

  def test_echoes_never_supply_physical_bus_evidence(self):
    events = messages()
    for event in events:
      if event.which() == "can":
        for frame in event.can:
          frame.src += 128
    result = self.run_audit(events)
    self.assertFalse(result["parkedPassiveEvidencePassed"])
    self.assertEqual(result["streams"]["can"]["samples"], 0)

  def test_missing_service_and_short_window_fail(self):
    self.assertIn("missing:carParams", self.run_audit(messages()[1:])["failures"])
    self.assertIn("incompleteObservationWindow", self.run_audit(messages(), minimum=3)["failures"])

  def test_send_attempt_is_not_passive_evidence(self):
    events = messages()
    tx = messaging.new_message("sendcan", 1, logMonoTime=3_000_000_000)
    tx.sendcan[0].address = 0x100
    events.append(tx)
    self.assertIn("sendcanObserved", self.run_audit(events)["failures"])

  def test_parameter_or_panda_configuration_is_not_hidden(self):
    events = messages()
    events[0].carParams.openpilotLongitudinalControl = True
    self.assertIn("notPassiveConfiguration", self.run_audit(events)["failures"])
    events = messages()
    events[5].pandaStates[0].safetyModel = "subaru"
    self.assertIn("pandaNotPassive", self.run_audit(events)["failures"])

  def test_panda_fault_and_counter_reset_are_retained(self):
    events = messages()
    events[5].pandaStates[0].faults = ["interruptRateCan2"]
    events[5].pandaStates[0].canState1.totalRxLostCnt = 1
    result = self.run_audit(events)
    self.assertIn("pandaFault", result["failures"])
    self.assertIn("pandaCountersChanged", result["failures"])

  def test_late_invalid_publication_fails(self):
    events = messages()
    events[4].valid = False
    self.assertIn("invalid:carState", self.run_audit(events)["failures"])

  def test_stream_gap_fails_even_with_long_total_span(self):
    events = messages()
    events = [event for event in events if event.which() != "carState" or event.logMonoTime in (1_000_000_000, 3_000_000_000)]
    self.assertIn("incompleteStream:carState", self.run_audit(events)["failures"])

  def test_empty_log_never_passes(self):
    result = self.run_audit([])
    self.assertFalse(result["parkedPassiveEvidencePassed"])

  def test_corrupt_suffix_is_not_silently_accepted(self):
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "rlog"
      save_log(str(path), [event.as_reader() for event in messages()], compress=False)
      with path.open("ab") as stream:
        stream.write(b"\x00\x00\x00\x00\xff\xff\xff\x7f")
      with self.assertRaises(RuntimeWarning):
        audit(path, minimum_seconds=1.5)

  def test_invalid_duration_rejected(self):
    for value in (0, -1, math.inf, math.nan):
      with self.subTest(value=value), self.assertRaises(ValueError):
        self.run_audit(messages(), minimum=value)


if __name__ == "__main__":
  unittest.main()
