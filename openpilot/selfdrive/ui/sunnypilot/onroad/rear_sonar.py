"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.system.ui.lib.text_measure import measure_text_cached


class RearSonarIndicators:
  def __init__(self):
    self._font = gui_app.font(FontWeight.SEMI_BOLD)
    self._valid = False
    self._halted = False
    self._faulted = False
    self._left = 0
    self._center = 0
    self._right = 0
    self._rab_alert = 0

  def update(self) -> None:
    state = ui_state.sm['carStateSP']
    self._valid = state.rearSonarValid
    self._halted = state.rearSonarSystemHalted
    self._faulted = state.rearSonarSystemFaulted
    self._left = state.rearSonarLeft
    self._center = state.rearSonarCenter
    self._right = state.rearSonarRight
    self._rab_alert = state.rearAutomaticBrakingAlert

  @property
  def detected(self) -> bool:
    return self._valid and any((self._left, self._center, self._right, self._rab_alert))

  def render(self, rect: rl.Rectangle) -> None:
    if self._faulted or self._halted:
      text = "REAR SENSORS UNAVAILABLE"
      size = 24
      measured = measure_text_cached(self._font, text, size)
      rl.draw_text_ex(self._font, text,
                      rl.Vector2(rect.x + (rect.width - measured.x) / 2, rect.y + rect.height - 64),
                      size, 0, rl.Color(190, 190, 190, 220))
      return

    if not self.detected:
      return

    segment_width = 72
    segment_height = 12
    gap = 18
    total_width = segment_width * 3 + gap * 2
    start_x = rect.x + (rect.width - total_width) / 2
    y = rect.y + rect.height - 64
    warning = rl.Color(255, 170, 32, 235)
    braking = rl.Color(255, 55, 45, 245)
    color = braking if self._rab_alert else warning

    # The recovered values are categorical warning zones, not calibrated distances.
    # Draw presence only until the real car establishes the enum ordering.
    for index, active in enumerate((self._left, self._center, self._right)):
      if active:
        segment = rl.Rectangle(start_x + index * (segment_width + gap), y, segment_width, segment_height)
        rl.draw_rectangle_rounded(segment, 0.8, 8, color)
