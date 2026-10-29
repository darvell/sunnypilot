import struct
import unittest

from scripts.subaru_research.vcpu_can_inventory import DIAGNOSTIC_FILTERS, MASK, PREFIX, RX_PREFIX, bank_inventory, inventory, words


def payload():
  """Synthetic copies of the recovered format, not a Subaru executable or flashing artifact."""
  data = bytearray(0x4000)
  identifiers = PREFIX + (0x6BB,) + RX_PREFIX + (0x241, 0x321, 0x325)
  tx_count = len(PREFIX) + 1
  count = len(identifiers)
  for bank in (0, 0x2000):
    app, filt = bank + 0x1000, bank + 0x1100
    data[app:app + count * 4] = words(identifiers)
    rx = identifiers[tx_count:] + DIAGNOSTIC_FILTERS
    for index, identifier in enumerate(rx):
      struct.pack_into("<4I", data, filt + 16 * index, identifier, MASK, index << 16, 0x100 if index < 4 else 0x800)
    cfg_base = app - 14 * count - 4
    offsets = cfg_base + 11 * count + 2
    for index in range(count):
      cfg = bytearray(10)
      cfg[2], cfg[3], cfg[4] = 0x10, 8, 0x80 if index >= tx_count else 0
      data[cfg_base + 10 * index:cfg_base + 10 * (index + 1)] = cfg
      struct.pack_into("<H", data, offsets + 2 * index, 8 * index)
    data[bank + 0x80:bank + 0x80 + 13] = b"FK1WHV010-880"
  return data


class TestVcpuInventory(unittest.TestCase):
  def test_separates_transmit_receive_and_diagnostic_filters(self):
    report = inventory(payload())
    self.assertEqual(report["banks"][0]["applicationCount"], 18)
    self.assertEqual(report["targets"]["0x6bb"]["direction"], "transmit")
    self.assertEqual(report["targets"]["0x241"]["direction"], "receive")
    self.assertIsNone(report["targets"]["0x221"])
    self.assertFalse(report["consumerTraceMatchesExactPayload"])
    # A matching version string is not enough to apply FK1's exact metadata profile.
    self.assertIsNone(report["banks"][0]["configurationFileOffset"])

  def test_raw_numeric_hit_does_not_create_a_message(self):
    data = payload()
    struct.pack_into("<I", data, 0x200, 0x220)
    self.assertIsNone(inventory(data)["targets"]["0x220"])

  def test_bank_disagreement_and_incomplete_filters_are_rejected(self):
    data = payload()
    struct.pack_into("<I", data, 0x1000 + len(PREFIX) * 4, 0x6BC)
    with self.assertRaisesRegex(ValueError, "bank inventories disagree"):
      inventory(data)
    data = payload()
    struct.pack_into("<I", data, 0x1100 + 16 * 6 + 4, 0)
    with self.assertRaisesRegex(ValueError, "incomplete or unsupported"):
      inventory(data)

  def test_fk1_config_uses_payload_offsets_and_notification_flags(self):
    report = bank_inventory(payload(), 0x1000, 0x1100, decode_fk1_config=True)
    rows = report["messages"]
    self.assertFalse(rows[10]["receiveNotification"])
    self.assertTrue(rows[11]["receiveNotification"])
    self.assertEqual(rows[11]["ramPayloadOffset"], 88)
    self.assertEqual(rows[11]["vcpuChannel"], 1)
    data = payload()
    offsets = int(report["configurationFileOffset"], 16) + 11 * 18 + 2
    struct.pack_into("<H", data, offsets + 2 * 11, 0)
    with self.assertRaisesRegex(ValueError, "payload offsets"):
      bank_inventory(data, 0x1000, 0x1100, decode_fk1_config=True)


if __name__ == "__main__":
  unittest.main()
