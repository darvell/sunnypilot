import threading
import unittest
from unittest.mock import Mock, patch

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
  @patch("opendbc.car.disable_ecu.disable_ecu", return_value=True)
  def test_forced_crosstrek_is_disabled_before_fingerprinting(self, disable_ecu):
    self.car.params = self.params(True, CAR.SUBARU_CROSSTREK_2025)
    self.car._maybe_pre_disable_subaru_eyesight()
    self.assertTrue(self.car.eyesight_pre_disabled)
    disable_ecu.assert_called_once()
    self.assertEqual(disable_ecu.call_args.kwargs["bus"], 2)
    self.assertEqual(disable_ecu.call_args.kwargs["addr"], 0x787)
    self.car._stop_eyesight_keepalive()

  @patch("opendbc.car.disable_ecu.disable_ecu")
  def test_disabled_when_alpha_long_is_off(self, disable_ecu):
    self.car.params = self.params(False, CAR.SUBARU_CROSSTREK_2025)
    self.car._maybe_pre_disable_subaru_eyesight()
    self.assertFalse(self.car.eyesight_pre_disabled)
    disable_ecu.assert_not_called()

  @patch("opendbc.car.disable_ecu.disable_ecu")
  def test_disabled_when_openpilot_is_off(self, disable_ecu):
    self.car.params = self.params(True, CAR.SUBARU_CROSSTREK_2025, openpilot_enabled=False)
    self.car._maybe_pre_disable_subaru_eyesight()
    self.assertFalse(self.car.eyesight_pre_disabled)
    disable_ecu.assert_not_called()

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", False)
  @patch("opendbc.car.disable_ecu.disable_ecu")
  def test_feature_gate_prevents_eyesight_disable(self, disable_ecu):
    self.car.params = self.params(True, CAR.SUBARU_CROSSTREK_2025)
    self.car._maybe_pre_disable_subaru_eyesight()
    self.car.params.put_bool.assert_called_once_with("AlphaLongitudinalEnabled", False, block=True)
    self.assertFalse(self.car.eyesight_pre_disabled)
    disable_ecu.assert_not_called()

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  @patch("opendbc.car.disable_ecu.disable_ecu")
  def test_alpha_long_is_disabled_without_cached_or_forced_crosstrek(self, disable_ecu):
    self.car.params = self.params(True)
    self.car._maybe_pre_disable_subaru_eyesight()
    self.car.params.put_bool.assert_called_once_with("AlphaLongitudinalEnabled", False, block=True)
    disable_ecu.assert_not_called()

  @patch("opendbc.car.subaru.values.GEN3_LONGITUDINAL_READY", True)
  @patch("opendbc.car.disable_ecu.disable_ecu", return_value=False)
  def test_alpha_long_is_disabled_when_eyesight_knockout_fails(self, _disable_ecu):
    self.car.params = self.params(True, CAR.SUBARU_CROSSTREK_2025)
    self.car._maybe_pre_disable_subaru_eyesight()
    self.car.params.put_bool.assert_called_once_with("AlphaLongitudinalEnabled", False, block=True)
    self.assertFalse(self.car.eyesight_pre_disabled)


if __name__ == "__main__":
  unittest.main()
