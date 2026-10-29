import contextlib
import io
import json
import math
import subprocess
import sys
import unittest
from unittest.mock import patch

import openpilot.cereal.messaging as messaging
from openpilot.common.prefix import OpenpilotPrefix
from scripts.subaru_research import validate_alpha_long_readiness as checker


SERVICES = ["carParams", "carParamsSP", "carState", "pandaStates", "selfdriveState", "onroadEvents"]


def healthy_messages(now):
  messages = {s: messaging.new_message(s, 1 if s == "pandaStates" else 0 if s == "onroadEvents" else None,
                                        valid=True, logMonoTime=int(now * 1e9)) for s in SERVICES}
  cp = messages["carParams"].carParams
  cp.carFingerprint = checker.EXPECTED_FINGERPRINT
  cp.alphaLongitudinalAvailable = True
  cp.openpilotLongitudinalControl = True
  cp.autoResumeSng = True
  cp.safetyConfigs = [{"safetyModel": "subaru", "safetyParam": 11}]
  cs = messages["carState"].carState
  cs.gearShifter = "park"
  cs.canValid = True
  panda = messages["pandaStates"].pandaStates[0]
  panda.safetyModel = "subaru"
  panda.safetyParam = 11
  panda.harnessStatus = "normal"
  return messages


def can_message(now, src=0, address=0x100, valid=True):
  msg = messaging.new_message("can", 1, valid=valid, logMonoTime=int(now * 1e9))
  msg.can[0].src = src
  msg.can[0].address = address
  msg.can[0].dat = b"\x00"
  return msg


class TestParkedTelemetry(unittest.TestCase):
  def setUp(self):
    self.enterContext(OpenpilotPrefix())

  def run_check(self, mutate=None, can=None, sample=0.5, timeout=1.5, params=None):
    # The actual SubMaster update_msgs supplies seen/alive/valid behavior; only
    # transport and time are replaced. Round-trip payloads through real capnp.
    sm = messaging.SubMaster(SERVICES)
    sm.simulation = False
    now = 100.0
    tick = 0

    def update(_timeout):
      nonlocal now, tick
      now = 100.0 + (tick + 1) * 0.1
      messages = healthy_messages(now)
      if mutate is not None:
        mutate(tick, messages)
      readers = [messaging.log_from_bytes(msg.to_bytes()) for msg in messages.values()]
      sm.update_msgs(now, readers)
      tick += 1

    def drain(_sock):
      messages = [can_message(now)] if can is None else can(tick - 1, now)
      return [messaging.log_from_bytes(msg.to_bytes()) for msg in messages]

    def get_bool(name):
      return params(tick - 1, name) if params else name == "AlphaLongitudinalEnabled"

    output = io.StringIO()
    with patch.object(checker.messaging, "SubMaster", return_value=sm), \
         patch.object(sm, "update", side_effect=update), \
         patch.object(checker.messaging, "sub_sock"), \
         patch.object(checker.messaging, "drain_sock", side_effect=drain), \
         patch.object(checker.time, "monotonic", side_effect=lambda: now), \
         patch.object(checker, "Params") as param_class, \
         patch("sys.argv", ["checker", "--sample-seconds", str(sample), "--wait-timeout", str(timeout)]), \
         contextlib.redirect_stdout(output):
      param_class.return_value.get_bool.side_effect = get_bool
      code = checker.main()
    return code, json.loads(output.getvalue())

  def test_healthy_parked_window(self):
    code, result = self.run_check()
    self.assertEqual(code, 0)
    self.assertTrue(result["ready"])
    self.assertTrue(result["parkedTelemetryPassed"])
    self.assertFalse(result["roadReadinessEstablished"])
    self.assertGreaterEqual(result["details"]["continuousObservedSeconds"], 0.5)

  def test_absent_can_is_not_silence(self):
    code, result = self.run_check(can=lambda tick, now: [])
    self.assertEqual(code, 1)
    self.assertFalse(result["ready"])

  def test_incomplete_window(self):
    code, _ = self.run_check(timeout=0.2)
    self.assertEqual(code, 1)

  def test_once_seen_services_expire(self):
    def once(tick, messages):
      if tick:
        messages.clear()
    code, _ = self.run_check(mutate=once, sample=1.1)
    self.assertEqual(code, 1)

  def test_on_demand_parameters_may_remain_cached(self):
    def once(tick, messages):
      if tick:
        del messages["carParams"]
        del messages["carParamsSP"]
    self.assertEqual(self.run_check(mutate=once)[0], 0)

  def test_transient_failures_remain_blocking(self):
    def movement(messages):
      messages["carState"].carState.vEgo = 1.0
    def engagement(messages):
      messages["selfdriveState"].selfdriveState.enabled = True
    def mismatch(messages):
      messages["carParamsSP"].carParamsSP.safetyParam = 1
    def steering(messages):
      messages["carState"].carState.steerFaultTemporary = True
    def panda_fault(messages):
      messages["pandaStates"].pandaStates[0].faults = ["interruptRateCan1"]
    for failure in (movement, engagement, mismatch, steering, panda_fault):
      with self.subTest(failure=failure.__name__):
        def mutate(tick, messages, failure=failure):
          if tick == 1:
            failure(messages)
        self.assertEqual(self.run_check(mutate=mutate)[0], 1)

  def test_invalid_update_cannot_be_hidden_by_cached_valid_data(self):
    for invalid_tick in (1, 5):
      for service in SERVICES:
        with self.subTest(service=service, tick=invalid_tick):
          def mutate(tick, messages, invalid_tick=invalid_tick, service=service):
            if tick == invalid_tick:
              messages[service].valid = False
          self.assertEqual(self.run_check(mutate=mutate)[0], 1)

  def test_counter_increase_then_reset(self):
    def mutate(tick, messages):
      messages["pandaStates"].pandaStates[0].safetyTxBlocked = 1 if tick == 1 else 0
    self.assertEqual(self.run_check(mutate=mutate)[0], 1)

  def test_parameter_recovers(self):
    self.assertEqual(self.run_check(params=lambda tick, name: name == "AlphaLongitudinalEnabled" and tick != 1)[0], 1)

  def test_echoes_and_stale_can_are_not_physical_evidence(self):
    for source in (128, 192, 3):
      with self.subTest(source=source):
        self.assertEqual(self.run_check(can=lambda tick, now, source=source: [can_message(now, src=source)])[0], 1)
    self.assertEqual(self.run_check(can=lambda tick, now: [can_message(99.0)])[0], 1)
    self.assertEqual(self.run_check(can=lambda tick, now: [can_message(now + 1)])[0], 1)
    self.assertEqual(self.run_check(can=lambda tick, now: [can_message(now, valid=False)])[0], 1)

  def test_can_loss_and_stock_frames_are_retained(self):
    self.assertEqual(self.run_check(can=lambda tick, now: [can_message(now)] if tick == 0 else [])[0], 1)
    self.assertEqual(self.run_check(can=lambda tick, now: [can_message(now, address=0x220 if tick == 1 else 0x100)])[0], 1)

  def test_failures_before_all_services_arrive(self):
    def mutate(tick, messages):
      if tick == 0:
        del messages["carParams"]
        messages["carState"].carState.vEgo = 1.0
    code, result = self.run_check(mutate=mutate)
    self.assertEqual(code, 1)
    self.assertFalse(result["checks"]["vehicleParked"])
    self.assertEqual(result["details"]["vEgo"], 0)

  def test_fault_status_without_fault_list(self):
    def mutate(tick, messages):
      if tick == 1:
        messages["pandaStates"].pandaStates[0].faultStatus = "faultTemp"
    code, result = self.run_check(mutate=mutate)
    self.assertEqual(code, 1)
    self.assertFalse(result["checks"]["noPandaFaults"])

  def test_other_panda_fault_blocks(self):
    def mutate(tick, messages):
      if tick == 1:
        message = messaging.new_message("pandaStates", 2, valid=True)
        message.pandaStates[0] = messages["pandaStates"].pandaStates[0]
        message.pandaStates[1].faults = ["interruptRateCan1"]
        messages["pandaStates"] = message
    self.assertEqual(self.run_check(mutate=mutate)[0], 1)

  def test_counter_reset_and_uptime_reset(self):
    for field in ("safetyTxBlocked", "safetyRxInvalid", "rxBufferOverflow", "txBufferOverflow", "spiErrorCount", "uptime"):
      with self.subTest(field=field):
        def mutate(tick, messages, field=field):
          setattr(messages["pandaStates"].pandaStates[0], field, 5 if tick == 0 else 0)
        self.assertEqual(self.run_check(mutate=mutate)[0], 1)

  def test_service_dropout_then_recovery(self):
    def mutate(tick, messages):
      if 1 <= tick <= 3:
        del messages["carState"]
    code, result = self.run_check(mutate=mutate)
    self.assertEqual(code, 1)
    self.assertFalse(result["checks"]["serviceAlive:carState"])

  def test_can_gap_between_polls_cannot_disappear(self):
    code, result = self.run_check(can=lambda tick, now: [] if tick == 1 else [can_message(now)])
    self.assertEqual(code, 1)
    self.assertFalse(result["checks"]["physicalCanFresh"])

  def test_late_invalid_can_remains_blocking(self):
    self.assertEqual(self.run_check(can=lambda tick, now: [can_message(now, valid=tick != 1)])[0], 1)

  def test_acc_fault_cannot_disappear(self):
    def mutate(tick, messages):
      messages["carState"].carState.accFaulted = tick == 1
    code, result = self.run_check(mutate=mutate)
    self.assertEqual(code, 1)
    self.assertFalse(result["checks"]["accHealthy"])

  def test_can_receive_loss_and_core_reset_cannot_disappear(self):
    for field in ("totalRxLostCnt", "canCoreResetCnt"):
      with self.subTest(field=field):
        def mutate(tick, messages, field=field):
          setattr(messages["pandaStates"].pandaStates[0].canState1, field, 1 if tick == 1 else 0)
        self.assertEqual(self.run_check(mutate=mutate)[0], 1)

  def test_bus_error_states_block(self):
    for field in ("errorWarning", "errorPassive"):
      with self.subTest(field=field):
        def mutate(tick, messages, field=field):
          setattr(messages["pandaStates"].pandaStates[0].canState1, field, tick == 1)
        self.assertEqual(self.run_check(mutate=mutate)[0], 1)

  def test_nonfinite_speed_blocks(self):
    def mutate(tick, messages):
      messages["carState"].carState.vEgo = math.nan
    code, result = self.run_check(mutate=mutate)
    self.assertEqual(code, 1)
    self.assertIsNone(result["details"]["vEgo"])

  def test_real_cli_without_publishers_fails_closed(self):
    # Isolated real msgq subscriptions and Params, with no publishers or vehicle.
    result = subprocess.run([sys.executable, checker.__file__, "--sample-seconds", "0.1", "--wait-timeout", "0.2"],
                            capture_output=True, text=True, check=False, timeout=10)
    self.assertEqual(result.returncode, 1, result.stderr)
    report = json.loads(result.stdout)
    self.assertFalse(report["parkedTelemetryPassed"])
    self.assertFalse(report["checks"]["allServicesSeen"])
    self.assertFalse(report["checks"]["physicalCanFresh"])
    self.assertFalse(report["checks"]["stockEyeSightSilent"])

  def test_nonfinite_or_nonpositive_durations(self):
    for flag in ("--sample-seconds", "--wait-timeout"):
      for duration in (math.nan, math.inf, -math.inf, 0, -1):
        with self.subTest(flag=flag, duration=duration), \
             patch("sys.argv", ["checker", f"{flag}={duration}"]), \
             patch.object(checker, "Params") as params, contextlib.redirect_stderr(io.StringIO()):
          with self.assertRaises(SystemExit) as error:
            checker.main()
          self.assertEqual(error.exception.code, 2)
          params.assert_not_called()


if __name__ == "__main__":
  unittest.main()
