import threading
import unittest
from unittest.mock import Mock, patch

from opendbc.can import CANDefine, CANPacker
from opendbc.car.can_definitions import CanData
from opendbc.car.subaru.values import CAR
from openpilot.selfdrive.car.card import Car
from openpilot.selfdrive.car.subaru_eyesight import DBC, EngineOffObservation, FACTORY_STREAMS, RETAINED_MESSAGES


class EngineOffCan:
  """Real packed vehicle CAN and ISO-TP replies; no Panda or vehicle is connected."""
  def __init__(self):
    self.now = 1.0
    self.packer = CANPacker(DBC)
    self.counter = 0
    self.rpm = 0
    self.gear = next(value for value, label in CANDefine(DBC).dv["Transmission"]["Gear"].items() if label == "P")
    self.brake = 0
    self.speed = 0
    self.silent = False
    self.response = b'\x68\x03'
    self.pending = []
    self.sent = []
    self.drop_receives_after_disable = False
    self.crank_after_disable = False
    self.crank_after_session = False
    self.keep_factory_after_disable = False

  def frames(self):
    values = {
      "Throttle": {"Engine_RPM": self.rpm}, "Transmission": {"Gear": self.gear},
      "Brake_Status": {"Brake": self.brake}, "Wheel_Speeds": dict.fromkeys(("FL", "FR", "RL", "RR"), self.speed),
    }
    frames = [CanData(*self.packer.make_can_msg(name, bus, {"COUNTER": self.counter, **values.get(name, {})}))
              for (bus, _), name in RETAINED_MESSAGES.items()]
    if not self.silent:
      factory_names = {0x124: "ES_LKAS_ANGLE", 0x220: "ES_Brake", 0x221: "ES_Distance", 0x222: "ES_Status"}
      frames += [CanData(*self.packer.make_can_msg(factory_names[addr], bus, {"COUNTER": self.counter})) for bus, addr in FACTORY_STREAMS]
    self.counter = (self.counter + 1) % 16
    return frames

  def recv(self, wait_for_one=False):
    self.now += 0.01
    frames = self.frames()
    if self.silent and self.drop_receives_after_disable:
      frames = []
    frames += self.pending
    self.pending = []
    return [frames] if frames else []

  def send(self, msgs):
    self.sent.extend(msgs)
    for msg in msgs:
      if msg.dat[1:3] == b'\x10\x03':
        if self.crank_after_session:
          self.rpm = 220
        self.pending.append(CanData(0x78F, bytes.fromhex("065003003201c200"), 2))
      elif msg.dat[1:4] == b'\x28\x03\x01':
        if self.response == b'\x68\x03':
          self.silent = not self.keep_factory_after_disable
          if self.crank_after_disable:
            self.rpm = 300
        if self.response is not None:
          self.pending.append(CanData(0x78F, bytes([len(self.response)]) + self.response + bytes(7 - len(self.response)), 2))


class TestSubaruPreDisable(unittest.TestCase):
  def setUp(self):
    self.bus = EngineOffCan()
    self.car = Car.__new__(Car)
    self.car.can_callbacks = (self.bus.recv, self.bus.send)
    self.car.eyesight_pre_disabled = False
    self.car.eyesight_keepalive_stop = threading.Event()
    self.car.eyesight_keepalive_thread = None
    self.car.params = Mock()
    self.car.params.get_bool.side_effect = lambda key: key in ("AlphaLongitudinalEnabled", "OpenpilotEnabledToggle")
    self.clock = patch("openpilot.selfdrive.car.subaru_eyesight.time.monotonic", side_effect=lambda: self.bus.now)
    self.clock.start()
    self.addCleanup(self.clock.stop)
    self.addCleanup(self.car._stop_eyesight_keepalive)

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  def test_engine_off_startup_uses_real_positive_isotp_response(self):
    self.assertTrue(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
    self.assertTrue(self.car.eyesight_pre_disabled)
    self.assertTrue(any(msg.address == 0x787 and msg.src == 2 and msg.dat[1:4] == b'\x28\x03\x01' for msg in self.bus.sent))
    self.assertIsNotNone(self.car.eyesight_keepalive_thread)

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  def test_nonblocking_diagnostic_drain_keeps_the_fresh_qualified_baseline(self):
    original_receive = self.bus.recv
    drained = False

    def receive(wait_for_one=False):
      nonlocal drained
      if not wait_for_one and self.bus.now >= 1.5 and not drained:
        drained = True
        self.bus.now += 0.001
        return []
      return original_receive(wait_for_one)

    self.car.can_callbacks = (receive, self.bus.send)
    self.assertTrue(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
    self.assertTrue(drained)

  def test_passive_configuration_never_attempts_diagnostics(self):
    self.car.params.get_bool.return_value = False
    self.car.params.get_bool.side_effect = None
    self.assertFalse(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
    self.assertEqual(self.bus.sent, [])

  def test_real_feature_gate_prevents_diagnostics(self):
    # Uses the actual gate, not a fixture that enables the experimental mode.
    self.assertFalse(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
    self.assertEqual(self.bus.sent, [])
    self.assertFalse(self.car.eyesight_pre_disabled)

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  def test_unsupported_fingerprint_never_attempts_diagnostics(self):
    self.assertFalse(self.car._maybe_disable_subaru_eyesight("MOCK"))
    self.assertEqual(self.bus.sent, [])

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  def test_running_engine_brake_or_motion_prevents_any_diagnostic_request(self):
    for field, value in (("rpm", 220), ("brake", 1), ("speed", 2), ("gear", 0)):
      with self.subTest(field=field):
        bus = EngineOffCan()
        setattr(bus, field, value)
        self.car.can_callbacks = (bus.recv, bus.send)
        # Advance the observation clock with this transport too.
        with patch("openpilot.selfdrive.car.subaru_eyesight.time.monotonic", side_effect=lambda bus=bus: bus.now):
          self.assertFalse(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
        self.assertEqual(bus.sent, [])

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  def test_uncertain_disable_never_constructs_a_claimed_stock_fallback(self):
    for condition in ("nrc", "timeout", "receive_loss", "crank", "factory_return"):
      with self.subTest(condition=condition):
        bus = EngineOffCan()
        bus.response = b'\x7f\x28\x22' if condition == "nrc" else (None if condition == "timeout" else b'\x68\x03')
        bus.drop_receives_after_disable = condition == "receive_loss"
        bus.crank_after_disable = condition == "crank"
        bus.keep_factory_after_disable = condition == "factory_return"
        self.car.can_callbacks = (bus.recv, bus.send)
        self.car.eyesight_keepalive_stop = threading.Event()
        with patch("openpilot.selfdrive.car.subaru_eyesight.time.monotonic", side_effect=lambda bus=bus: bus.now):
          with self.assertRaisesRegex(RuntimeError, "ownership indeterminate"):
            self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025)
        self.assertFalse(self.car.eyesight_pre_disabled)
        self.assertTrue(self.car.eyesight_keepalive_stop.is_set())

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  def test_crank_during_session_prevents_the_communication_control_request(self):
    self.bus.crank_after_session = True
    with self.assertRaisesRegex(RuntimeError, "ownership indeterminate"):
      self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025)
    self.assertTrue(any(msg.dat[1:3] == b'\x10\x03' for msg in self.bus.sent))
    self.assertFalse(any(msg.dat[1:4] == b'\x28\x03\x01' for msg in self.bus.sent))

  def test_empty_receive_is_not_verified_silence(self):
    self.car.can_callbacks = (Mock(side_effect=lambda **_: self.empty_receive()), Mock())
    self.assertFalse(self.car._verify_subaru_eyesight_silenced())

  def empty_receive(self):
    self.bus.now += 0.02
    return []

  def test_runtime_return_stops_step_before_any_actuation_or_fallback(self):
    self.car.CP = Mock(openpilotLongitudinalControl=True)
    self.car.state_publish = Mock()
    self.car.controls_update = Mock()
    with patch("openpilot.selfdrive.car.card.messaging.drain_sock_raw", return_value=[b'fixture']), \
         patch("openpilot.selfdrive.car.card.can_capnp_to_list", return_value=[self.bus.frames()]):
      self.car.can_sock = Mock()
      with self.assertRaisesRegex(RuntimeError, "stock EyeSight traffic resumed"):
        self.car.step()
    self.car.state_publish.assert_not_called()
    self.car.controls_update.assert_not_called()

  def test_eyesight_check_ignores_returned_and_rejected_echoes(self):
    packets = [[CanData(0x124, bytes(8), 128), CanData(0x220, bytes(8), 193)]]
    self.assertFalse(self.car._subaru_eyesight_traffic_present(packets))

  def test_runtime_check_leaves_stock_longitudinal_alone(self):
    self.car.CP = Mock(openpilotLongitudinalControl=False)
    self.car._check_subaru_eyesight_remains_silenced([self.bus.frames()])
    self.car.params.put_bool.assert_not_called()


class TestEngineOffObservation(unittest.TestCase):
  def observe(self, mutate=None):
    bus = EngineOffCan()
    observation = EngineOffObservation(1.0, expect_silence=False)
    for i in range(61):
      packets = [bus.frames()]
      if mutate is not None:
        packets = mutate(bus, packets, i)
      observation.update(packets, 1.0 + i * 0.01)
    return observation.result(1.6)

  def test_healthy_parked_factory_traffic_qualifies_only_the_observation(self):
    result = self.observe()
    self.assertTrue(result.qualified, result.reason)
    self.assertIn("road readiness not established", result.reason)

  def test_missing_stream_or_echoes_cannot_supply_coverage(self):
    for missing in RETAINED_MESSAGES:
      def remove(bus, packets, i, missing=missing):
        return [[msg if (msg.src, msg.address) != missing else CanData(msg.address, msg.dat, 128 + msg.src)
                 for msg in packets[0]]]
      with self.subTest(missing=missing):
        self.assertFalse(self.observe(remove).qualified)

  def test_transient_crank_cannot_be_hidden_by_final_engine_off_sample(self):
    def crank(bus, packets, i):
      if i == 20:
        bus.rpm = 300
      elif i == 21:
        bus.rpm = 0
      return packets
    self.assertFalse(self.observe(crank).qualified)

  def test_lost_window_cannot_be_hidden_by_fresh_final_sample(self):
    self.assertFalse(self.observe(lambda bus, packets, i: [] if 20 < i < 40 else packets).qualified)

  def test_bad_checksum_or_repeated_counters_fail_closed(self):
    def corrupt(bus, packets, i):
      if i == 20:
        msg = packets[0][0]
        packets[0][0] = CanData(msg.address, bytes([msg.dat[0] ^ 1]) + msg.dat[1:], msg.src)
      return packets
    self.assertFalse(self.observe(corrupt).qualified)
    self.assertFalse(self.observe(lambda bus, packets, i: [packets[0] + packets[0]] if i == 20 else packets).qualified)


if __name__ == "__main__":
  unittest.main()
