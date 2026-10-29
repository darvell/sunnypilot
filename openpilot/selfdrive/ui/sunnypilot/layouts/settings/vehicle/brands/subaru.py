"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
from openpilot.selfdrive.ui.sunnypilot.layouts.settings.vehicle.brands.base import BrandSettings
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.sunnypilot.widgets.list_view import toggle_item_sp
from opendbc.car.subaru.values import CAR, SubaruFlags
from opendbc.sunnypilot.car.subaru.comfort import comfort_supported


class SubaruSettings(BrandSettings):
  def __init__(self):
    super().__init__()
    self.has_stop_and_go = False
    self.has_comfort = False

    self.stop_and_go_toggle = toggle_item_sp(tr("Stop and Go (Beta)"), "", param="SubaruStopAndGo", callback=self._on_toggle_changed)

    self.stop_and_go_manual_parking_brake_toggle = toggle_item_sp(tr("Stop and Go for Manual Parking Brake (Beta)"), "",
                                                                  param="SubaruStopAndGoManualParkingBrake", callback=self._on_toggle_changed)

    self.auto_vehicle_hold_toggle = toggle_item_sp(tr("Auto Vehicle Hold at Start (Beta)"), "",
                                                   param="SubaruAutoVehicleHold", callback=self._on_toggle_changed)
    self.stop_start_off_toggle = toggle_item_sp(tr("Turn Off Auto Start-Stop at Start (Beta)"), "",
                                                param="SubaruStopStartOff", callback=self._on_toggle_changed)

    self.items = [self.stop_and_go_toggle, self.stop_and_go_manual_parking_brake_toggle,
                  self.auto_vehicle_hold_toggle, self.stop_start_off_toggle]

  def _on_toggle_changed(self, _):
    self.update_settings()

  @staticmethod
  def disabled_msg(available):
    if not available:
      return tr("This feature is currently not available on this platform.")
    elif not ui_state.is_offroad():
      return tr("Enable \"Always Offroad\" in Device panel, or turn vehicle off to toggle.")
    return ""

  def update_settings(self):
    bundle = ui_state.params.get("CarPlatformBundle")
    if bundle:
      platform = bundle.get("platform")
      config = CAR[platform].config
      self.has_stop_and_go = not (config.flags & (SubaruFlags.GLOBAL_GEN2 | SubaruFlags.HYBRID))
      self.has_comfort = comfort_supported(platform)
    elif ui_state.CP is not None:
      self.has_stop_and_go = not (ui_state.CP.flags & (SubaruFlags.GLOBAL_GEN2 | SubaruFlags.HYBRID))
      self.has_comfort = comfort_supported(ui_state.CP.carFingerprint)

    toggles = [
      (self.stop_and_go_toggle, self.has_stop_and_go,
       tr("Experimental feature to enable auto-resume during stop-and-go for certain supported Subaru platforms.")),
      (self.stop_and_go_manual_parking_brake_toggle, self.has_stop_and_go,
       tr("Experimental feature to enable stop and go for Subaru Global models with manual handbrake. " +
          "Models with electric parking brake should keep this disabled. Thanks to martinl for this implementation!")),
      (self.auto_vehicle_hold_toggle, self.has_comfort,
       tr("Switch Auto Vehicle Hold on once at the start of each drive, since the car forgets it every ignition cycle. " +
          "Turning it off on the touchscreen afterwards is respected for the rest of the drive.")),
      (self.stop_start_off_toggle, self.has_comfort,
       tr("Switch the auto start-stop engine shutoff off once at the start of each drive. " +
          "Turning it back on afterwards is respected for the rest of the drive.")),
    ]

    for toggle, available, desc in toggles:
      disabled_msg = self.disabled_msg(available)
      toggle.action_item.set_enabled(available and ui_state.is_offroad())
      toggle.set_description(f"<b>{disabled_msg}</b><br><br>{desc}" if disabled_msg else desc)
