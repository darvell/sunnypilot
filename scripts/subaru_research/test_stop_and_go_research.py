from pathlib import Path
import tempfile
import unittest

from opendbc.can import CANDefine, CANPacker
from opendbc.car.can_definitions import CanData
from openpilot.cereal import messaging
from openpilot.selfdrive.car.subaru_eyesight import DBC, FACTORY_STREAMS, RETAINED_MESSAGES
from openpilot.tools.lib.logreader import save_log
from scripts.subaru_research.stop_and_go_research import EyeSightOffResearch, STOCK_MESSAGES, StockResumeResearch, audit


class StockCan:
  def __init__(self):
    self.packer = CANPacker(DBC)
    self.counter = 0
    gears = CANDefine(DBC).dv["Transmission"]["Gear"]
    self.park = next(value for value, label in gears.items() if label == "P")
    self.drive = next(value for value, label in gears.items() if label == "D")
    self.values = {
      "Throttle": {"Engine_RPM": 700}, "Transmission": {"Gear": self.drive}, "Steering_Torque": {},
      "Wheel_Speeds": {}, "Brake_Status": {},
      "ES_Distance": {"Car_Follow": 1, "Close_Distance": 1.2, "Cruise_Throttle": 808},
      "ES_Status": {"Cruise_Activated": 1}, "ES_DashStatus": {"Cruise_On": 1, "Cruise_State": 3},
      "CruiseControl": {}, "ES_LKAS_ANGLE": {}, "ES_Brake": {},
    }

  def frames(self, messages=STOCK_MESSAGES):
    result = [CanData(*self.packer.make_can_msg(name, bus, {"COUNTER": self.counter, **self.values.get(name, {})}))
              for (bus, _), name in messages.items()]
    self.counter = (self.counter + 1) % 16
    return result


class TestStockResumeResearch(unittest.TestCase):
  def run_hold(self, change=None):
    bus, analyzer = StockCan(), StockResumeResearch()
    for tick in range(100):
      t = 1 + tick * 0.02
      bus.values["ES_Distance"]["Close_Distance"] = 1.2 if tick < 40 else 3.5
      packets = [bus.frames()]
      if change:
        packets = change(bus, packets, tick)
      analyzer.update(t, packets)
    return analyzer

  def test_fresh_hold_and_lead_motion_emits_one_annotation_only(self):
    result = self.run_hold().result()
    self.assertEqual(len(result["resumeCandidates"]), 1)
    candidate = result["resumeCandidates"][0]
    self.assertGreaterEqual(candidate["timeSeconds"], 1.95)
    self.assertEqual(candidate["action"], "annotation_only_no_transmission")
    self.assertFalse(result["actuatorValidated"])
    self.assertFalse(result["physicalResumeButtonDecoded"])

  def test_long_stop_without_lead_or_without_hold_never_requests_resume(self):
    for name, field, value in (("ES_Distance", "Car_Follow", 0), ("ES_DashStatus", "Cruise_State", 0),
                               ("ES_DashStatus", "Conventional_Cruise", 1), ("ES_Status", "Cruise_Activated", 0)):
      def change(bus, packets, tick, name=name, field=field, value=value):
        bus.values[name][field] = value
        return packets
      with self.subTest(name=name, field=field):
        self.assertEqual(self.run_hold(change).candidates, [])

  def test_pedals_and_camera_button_announcement_veto_the_entire_hold(self):
    for name, field in (("Throttle", "Throttle_Pedal"), ("Brake_Status", "Brake"), ("ES_Distance", "Cruise_Resume")):
      def pulse(bus, packets, tick, name=name, field=field):
        # A single positive sample, followed by release, must not re-arm inside the same HOLD.
        bus.values[name][field] = 1 if tick == 30 else 0
        return packets
      with self.subTest(field=field):
        self.assertEqual(self.run_hold(pulse).candidates, [])

  def test_same_batch_brake_press_and_release_is_not_lost(self):
    def brake(bus, packets, tick):
      if tick == 30:
        packets[0].append(CanData(*bus.packer.make_can_msg("Brake_Status", 1, {"COUNTER": bus.counter, "Brake": 1})))
        packets[0].append(CanData(*bus.packer.make_can_msg("Brake_Status", 1, {"COUNTER": (bus.counter + 1) % 16, "Brake": 0})))
      return packets
    self.assertEqual(self.run_hold(brake).candidates, [])

  def test_echoes_stale_or_missing_inputs_cannot_supply_observation(self):
    for missing in STOCK_MESSAGES:
      def hide(bus, packets, tick, missing=missing):
        return [[CanData(msg.address, msg.dat, msg.src + 128) if (msg.src, msg.address) == missing else msg for msg in packets[0]]]
      with self.subTest(missing=missing):
        self.assertEqual(self.run_hold(hide).candidates, [])

  def test_noisy_distance_and_saturated_distance_do_not_trigger(self):
    for distance in (1.3, 1.4, 5.0):
      def noise(bus, packets, tick, distance=distance):
        if tick >= 40:
          for index, msg in enumerate(packets[0]):
            if (msg.src, msg.address) == (1, 0x221):
              packets[0][index] = CanData(*bus.packer.make_can_msg("ES_Distance", 1, {
                **bus.values["ES_Distance"], "COUNTER": msg.dat[1] & 15, "Close_Distance": distance,
              }))
        return packets
      with self.subTest(distance=distance):
        self.assertEqual(self.run_hold(noise).candidates, [])

  def test_absent_can_events_do_not_complete_a_lead_motion_interval(self):
    bus, analyzer = StockCan(), StockResumeResearch()
    for tick in range(41):
      if tick == 40:
        bus.values["ES_Distance"]["Close_Distance"] = 3.5
      analyzer.update(1 + tick * 0.02, [bus.frames()])
    # No update calls during the blackout. Fresh counters afterward do not repair the missing interval.
    analyzer.update(20.0, [bus.frames()])
    self.assertEqual(analyzer.candidates, [])
    self.assertGreater(analyzer.vetoes["receive_gap"], 0)

  def test_fault_then_clear_in_one_batch_still_resets_qualification(self):
    for name, bus_number, address in (("ES_Status", 1, 0x222), ("ES_DashStatus", 2, 0x321)):
      with self.subTest(message=name):
        bus, analyzer = StockCan(), StockResumeResearch()
        for tick in range(49):
          if tick == 40:
            bus.values["ES_Distance"]["Close_Distance"] = 3.5
          frames = bus.frames()
          if tick == 48:  # the batch that would otherwise emit a candidate
            counter = next(msg.dat[1] & 15 for msg in frames if (msg.src, msg.address) == (bus_number, address))
            for offset, fault in ((1, 1), (2, 0)):
              frames.append(CanData(*bus.packer.make_can_msg(name, bus_number, {
                **bus.values[name], "COUNTER": (counter + offset) % 16, "Cruise_Fault": fault,
              })))
          analyzer.update(1 + tick * 0.02, [frames])
        self.assertEqual(analyzer.candidates, [])

  def test_real_compressed_log_intake_is_always_locked(self):
    bus = StockCan()
    events = []
    for tick in range(100):
      if tick == 40:
        bus.values["ES_Distance"]["Close_Distance"] = 3.5
      frames = bus.frames()
      event = messaging.new_message("can", len(frames), valid=True, logMonoTime=1_000_000_000 + tick * 20_000_000)
      for output, frame in zip(event.can, frames, strict=True):
        output.address, output.dat, output.src = frame
      events.append(event)
    with tempfile.TemporaryDirectory() as directory:
      path = Path(directory) / "rlog.zst"
      save_log(str(path), [event.as_reader() for event in events])
      result = audit(path, "eyesight-on")
    self.assertEqual(result["canEvents"], 100)
    self.assertEqual(len(result["analysis"]["resumeCandidates"]), 1)
    self.assertTrue(result["modeLocked"])
    self.assertFalse(result["transmitEnabled"])
    self.assertFalse(result["roadReadinessEstablished"])


class TestEyeSightOffResearch(unittest.TestCase):
  def sequence(self, response=b'\x68\x03', return_stock=False, drop_rx=False, blackout=False):
    bus, analyzer = StockCan(), EyeSightOffResearch()
    bus.values["Throttle"]["Engine_RPM"] = 0
    bus.values["Transmission"]["Gear"] = bus.park
    names = {0x124: "ES_LKAS_ANGLE", 0x220: "ES_Brake", 0x221: "ES_Distance", 0x222: "ES_Status"}
    factory = {key: names[key[1]] for key in FACTORY_STREAMS}
    for tick in range(150):
      t = 1 + tick * 0.02 + (7.4 if blackout and tick >= 80 else 0)
      if tick == 40:
        analyzer.diagnostic(t, 0x787, bytes.fromhex("0328030100000000"), 2, outgoing=True)
      if tick == 41:
        payload = bytes([len(response)]) + response + bytes(7 - len(response))
        analyzer.diagnostic(t, 0x78F, payload, 2)
      if tick == 80:
        bus.values["Throttle"]["Engine_RPM"] = 300
      messages = dict(RETAINED_MESSAGES)
      if tick < 41 or (return_stock and tick >= 120):
        messages.update(factory)
      frames = bus.frames(messages)
      if drop_rx and 100 <= tick < 110:
        frames = []
      analyzer.update(t, [frames])
    return analyzer

  def test_positive_ack_silence_then_crank_is_observation_not_unlock(self):
    result = self.sequence().result()
    self.assertTrue(result["engineOffBeforeRequestObserved"])
    self.assertTrue(result["silenceBeforeCrankObserved"])
    self.assertTrue(result["silenceThroughCrankObserved"], result)
    self.assertFalse(result["stockHandoverEstablished"])
    self.assertFalse(result["physicalButtonsVerifiedAfterSilence"])

  def test_real_negative_response_does_not_count_as_disable(self):
    result = self.sequence(response=b'\x7f\x28\x22').result()
    self.assertFalse(result["silenceThroughCrankObserved"])
    self.assertIn("communication_control_rejected_7f2822", result["failures"])

  def test_returned_factory_traffic_or_receive_loss_invalidates_crank_evidence(self):
    for kwargs in ({"return_stock": True}, {"drop_rx": True}):
      with self.subTest(**kwargs):
        result = self.sequence(**kwargs).result()
        self.assertFalse(result["silenceThroughCrankObserved"])
        self.assertFalse(result["stockHandoverEstablished"])

  def test_no_can_events_during_crank_interval_is_not_continuous_evidence(self):
    result = self.sequence(blackout=True).result()
    self.assertFalse(result["silenceThroughCrankObserved"])
    self.assertIn("receive_gap", result["failures"])

  def test_no_request_no_ack_and_echoes_cannot_qualify(self):
    analyzer = EyeSightOffResearch()
    analyzer.diagnostic(1.0, 0x787, bytes.fromhex("0328030100000000"), 130, outgoing=True)
    analyzer.diagnostic(1.1, 0x78F, bytes.fromhex("0268030000000000"), 130)
    result = analyzer.result()
    self.assertIn("no_disable_request", result["failures"])
    self.assertFalse(result["silenceThroughCrankObserved"])


if __name__ == "__main__":
  unittest.main()
