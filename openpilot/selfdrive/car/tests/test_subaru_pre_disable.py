import threading
import unittest
from unittest.mock import Mock, patch

from opendbc.car.can_definitions import CanData
from opendbc.car.subaru.values import CAR
from openpilot.selfdrive.car.card import Car


class TestSubaruPreDisable(unittest.TestCase):
  def setUp(self):
    self.car = Car.__new__(Car)
    self.car.can_callbacks = (Mock(), Mock())
    self.car.eyesight_pre_disabled = False
    self.car.eyesight_keepalive_stop = threading.Event()
    self.car.eyesight_keepalive_thread = None

  @staticmethod
  def params(alpha_long, platform=None, openpilot_enabled=True):
    params = Mock()
    params.get_bool.side_effect = lambda key: {
      "AlphaLongitudinalEnabled": alpha_long,
      "OpenpilotEnabledToggle": openpilot_enabled,
    }.get(key, False)
    params.get.side_effect = lambda key: ({"platform": platform} if key == "CarPlatformBundle" and platform else None)
    return params

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  @patch.object(Car, "_verify_subaru_eyesight_silenced", return_value=True)
  @patch("opendbc.car.disable_ecu.disable_ecu", return_value=True)
  def test_crosstrek_is_disabled_after_fingerprinting(self, disable_ecu, verify_silenced):
    self.car.params = self.params(True, CAR.SUBARU_CROSSTREK_2025)
    self.assertTrue(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
    self.assertTrue(self.car.eyesight_pre_disabled)
    disable_ecu.assert_called_once()
    verify_silenced.assert_called_once()
    self.assertEqual(disable_ecu.call_args.kwargs["bus"], 2)
    self.assertEqual(disable_ecu.call_args.kwargs["addr"], 0x787)
    self.car._stop_eyesight_keepalive()

  @patch("opendbc.car.disable_ecu.disable_ecu")
  def test_disabled_when_openpilot_is_off(self, disable_ecu):
    self.car.params = self.params(True, CAR.SUBARU_CROSSTREK_2025, openpilot_enabled=False)
    self.assertFalse(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
    self.assertFalse(self.car.eyesight_pre_disabled)
    disable_ecu.assert_not_called()

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", False)
  @patch("opendbc.car.disable_ecu.disable_ecu")
  def test_feature_gate_prevents_eyesight_disable(self, disable_ecu):
    self.car.params = self.params(True, CAR.SUBARU_CROSSTREK_2025)
    self.assertFalse(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
    self.car.params.put_bool.assert_called_once_with("AlphaLongitudinalEnabled", False, block=True)
    self.assertFalse(self.car.eyesight_pre_disabled)
    disable_ecu.assert_not_called()

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  @patch("opendbc.car.disable_ecu.disable_ecu")
  def test_alpha_long_is_disabled_on_unsupported_fingerprint(self, disable_ecu):
    self.car.params = self.params(True)
    self.assertFalse(self.car._maybe_disable_subaru_eyesight("MOCK"))
    self.car.params.put_bool.assert_called_once_with("AlphaLongitudinalEnabled", False, block=True)
    disable_ecu.assert_not_called()

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  @patch.object(Car, "_verify_subaru_eyesight_silenced", return_value=False)
  @patch("opendbc.car.disable_ecu.disable_ecu", return_value=True)
  def test_alpha_long_is_disabled_when_stock_eyesight_traffic_remains(self, _disable_ecu, verify_silenced):
    self.car.params = self.params(True, CAR.SUBARU_CROSSTREK_2025)
    self.assertFalse(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
    verify_silenced.assert_called_once()
    self.car.params.put_bool.assert_called_once_with("AlphaLongitudinalEnabled", False, block=True)
    self.assertFalse(self.car.eyesight_pre_disabled)

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  @patch("opendbc.car.disable_ecu.disable_ecu", return_value=False)
  def test_alpha_long_is_disabled_when_eyesight_knockout_fails(self, _disable_ecu):
    self.car.params = self.params(True, CAR.SUBARU_CROSSTREK_2025)
    self.assertFalse(self.car._maybe_disable_subaru_eyesight(CAR.SUBARU_CROSSTREK_2025))
    self.car.params.put_bool.assert_called_once_with("AlphaLongitudinalEnabled", False, block=True)
    self.assertFalse(self.car.eyesight_pre_disabled)

  @patch("openpilot.selfdrive.car.card.time.monotonic", side_effect=[0.0, 0.1])
  def test_eyesight_silence_verification_rejects_stock_actuation_frames(self, _monotonic):
    self.car.can_callbacks = (Mock(side_effect=[[], [[CanData(0x220, b'\x00' * 8, 1)]]]), Mock())
    self.assertFalse(self.car._verify_subaru_eyesight_silenced())

  @patch("openpilot.selfdrive.car.card.time.monotonic", side_effect=[0.0, 0.1])
  def test_eyesight_silence_verification_rejects_stock_lkas_frame(self, _monotonic):
    self.car.can_callbacks = (Mock(side_effect=[[], [[CanData(0x124, b'\x00' * 8, 0)]]]), Mock())
    self.assertFalse(self.car._verify_subaru_eyesight_silenced())

  @patch("openpilot.selfdrive.car.card.time.monotonic", side_effect=[0.0, 0.1, 0.6])
  def test_eyesight_silence_verification_accepts_quiet_bus(self, _monotonic):
    self.car.can_callbacks = (Mock(side_effect=[[], []]), Mock())
    self.assertTrue(self.car._verify_subaru_eyesight_silenced())


if __name__ == "__main__":
  unittest.main()
