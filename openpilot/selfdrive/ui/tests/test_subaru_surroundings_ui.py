import unittest
from types import SimpleNamespace

from openpilot.selfdrive.ui.sunnypilot.onroad.blind_spot_indicators import BlindSpotIndicators
from openpilot.selfdrive.ui.sunnypilot.onroad.developer_ui.elements import BlindSpotStateElement, RearSonarStateElement
from openpilot.selfdrive.ui.sunnypilot.onroad.rear_sonar import RearSonarIndicators


class TestSubaruSurroundingsUi(unittest.TestCase):
  def test_blindspot_colors_distinguish_state(self):
    invalid = BlindSpotIndicators._indicator_color(False, False, False, 255)
    adjacent = BlindSpotIndicators._indicator_color(True, False, True, 255)
    approaching = BlindSpotIndicators._indicator_color(False, True, True, 255)
    held = BlindSpotIndicators._indicator_color(False, False, True, 255)

    self.assertEqual((invalid.r, invalid.g, invalid.b), (160, 160, 160))
    self.assertEqual((adjacent.r, adjacent.g, adjacent.b), (255, 190, 32))
    self.assertEqual((approaching.r, approaching.g, approaching.b), (255, 96, 32))
    self.assertEqual((held.r, held.g, held.b), (255, 150, 32))

  def test_rear_sonar_detected_requires_valid_nonzero_state(self):
    sonar = RearSonarIndicators.__new__(RearSonarIndicators)
    sonar._valid = True
    sonar._left = 0
    sonar._center = 0
    sonar._right = 0
    sonar._rab_alert = 0
    self.assertFalse(sonar.detected)

    sonar._center = 1
    self.assertTrue(sonar.detected)

    sonar._valid = False
    self.assertFalse(sonar.detected)

  def test_developer_elements_preserve_detailed_states(self):
    state = SimpleNamespace(
      blindSpotMonitorValid=True,
      blindSpotLeftAdjacent=False,
      blindSpotLeftApproaching=True,
      blindSpotRightAdjacent=True,
      blindSpotRightApproaching=False,
      rearSonarSystemFaulted=False,
      rearSonarSystemHalted=False,
      rearSonarValid=True,
      rearSonarLeft=1,
      rearSonarCenter=4,
      rearSonarRight=2,
    )
    sm = {"carStateSP": state}
    self.assertEqual(BlindSpotStateElement().update(sm, True).value, "P/A")
    self.assertEqual(RearSonarStateElement().update(sm, True).value, "1/4/2")


if __name__ == "__main__":
  unittest.main()
