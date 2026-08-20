"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import pyray as rl

from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import gui_app
from openpilot.common.filter_simple import FirstOrderFilter


class BlindSpotIndicators:
  def __init__(self):
    self._txt_blind_spot_left: rl.Texture = gui_app.texture('icons_mici/onroad/blind_spot_left.png', 108, 128)
    self._txt_blind_spot_right: rl.Texture = gui_app.texture('icons_mici/onroad/blind_spot_left.png', 108, 128, flip_x=True)

    self._blind_spot_left_alpha_filter = FirstOrderFilter(0, 0.15, 1 / gui_app.target_fps)
    self._blind_spot_right_alpha_filter = FirstOrderFilter(0, 0.15, 1 / gui_app.target_fps)
    self._left_adjacent = False
    self._left_approaching = False
    self._right_adjacent = False
    self._right_approaching = False
    self._bsm_valid = True

  def update(self) -> None:
    sm = ui_state.sm
    CS = sm['carState']
    CS_SP = sm['carStateSP']

    self._blind_spot_left_alpha_filter.update(1.0 if CS.leftBlindspot else 0.0)
    self._blind_spot_right_alpha_filter.update(1.0 if CS.rightBlindspot else 0.0)
    self._left_adjacent = CS_SP.blindSpotLeftAdjacent
    self._left_approaching = CS_SP.blindSpotLeftApproaching
    self._right_adjacent = CS_SP.blindSpotRightAdjacent
    self._right_approaching = CS_SP.blindSpotRightApproaching
    self._bsm_valid = CS_SP.blindSpotMonitorValid

  @staticmethod
  def _indicator_color(adjacent: bool, approaching: bool, valid: bool, alpha: int) -> rl.Color:
    if not valid:
      return rl.Color(160, 160, 160, alpha)
    if approaching:
      return rl.Color(255, 96, 32, alpha)
    if adjacent:
      return rl.Color(255, 190, 32, alpha)
    # A recently-cleared approaching vehicle remains held as a conservative veto.
    return rl.Color(255, 150, 32, alpha)

  @property
  def detected(self) -> bool:
    return ui_state.blindspot and (self._blind_spot_left_alpha_filter.x > 0.01 or self._blind_spot_right_alpha_filter.x > 0.01)

  def render(self, rect: rl.Rectangle) -> None:
    if not ui_state.blindspot:
      return

    BLIND_SPOT_MARGIN_X = 20  # Distance from edge of screen
    BLIND_SPOT_Y_OFFSET = 100  # Distance from top of screen

    if self._blind_spot_left_alpha_filter.x > 0.01:
      pos_x = int(rect.x + BLIND_SPOT_MARGIN_X)
      pos_y = int(rect.y + BLIND_SPOT_Y_OFFSET)
      alpha = int(255 * self._blind_spot_left_alpha_filter.x)
      color = self._indicator_color(self._left_adjacent, self._left_approaching, self._bsm_valid, alpha)
      rl.draw_texture_ex(self._txt_blind_spot_left, rl.Vector2(pos_x, pos_y), 0.0, 1.0, color)

    if self._blind_spot_right_alpha_filter.x > 0.01:
      pos_x = int(rect.x + rect.width - BLIND_SPOT_MARGIN_X - self._txt_blind_spot_right.width)
      pos_y = int(rect.y + BLIND_SPOT_Y_OFFSET)
      alpha = int(255 * self._blind_spot_right_alpha_filter.x)
      color = self._indicator_color(self._right_adjacent, self._right_approaching, self._bsm_valid, alpha)
      rl.draw_texture_ex(self._txt_blind_spot_right, rl.Vector2(pos_x, pos_y), 0.0, 1.0, color)
