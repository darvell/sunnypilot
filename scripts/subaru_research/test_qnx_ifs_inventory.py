import struct
import unittest
from unittest.mock import patch

from scripts.subaru_research.qnx_ifs_inventory import parse_directory, unpack_image


def imagefs(name="usr/bin/MeterCore", payload=b"example"):
  image = bytearray(256)
  image[:7] = b"imagefs"
  encoded = name.encode() + b"\x00"
  length = (32 + len(encoded) + 3) & ~3
  directory_end = 92 + length + 4
  struct.pack_into("<3I", image, 8, len(image), directory_end, 92)
  struct.pack_into("<HHIIIII", image, 92, length, 0, 1, 0x8000 | 0o644, 0, 0, 0)
  struct.pack_into("<II", image, 116, 200, len(payload))
  image[124:124 + len(encoded)] = encoded
  image[200:200 + len(payload)] = payload
  checksum = -sum(value[0] for value in struct.iter_unpack("<I", image)) & 0xFFFFFFFF
  struct.pack_into("<I", image, len(image) - 4, checksum)
  return image


def startup(image):
  raw = bytearray(256)
  raw[:4] = bytes.fromhex("eb7eff00")
  raw[6] = 8
  stream = b"\x00\x01X\x00\x00"
  struct.pack_into("<4I", raw, 32, 256, 256 + len(stream), 0, len(image))
  return raw + stream


class TestQnxImageInventory(unittest.TestCase):
  def test_regular_file_data_and_directory_extent(self):
    image = imagefs()
    files = parse_directory(image)
    self.assertEqual([(f.path, f.offset, f.size) for f in files], [("usr/bin/MeterCore", 200, 7)])
    self.assertEqual(image[files[0].offset:files[0].offset + files[0].size], b"example")

  def test_checksum_failure_is_not_a_valid_inventory(self):
    image = imagefs()
    image[201] ^= 1
    with self.assertRaisesRegex(ValueError, "checksum"):
      parse_directory(image)

  def test_image_paths_cannot_escape_extraction_root(self):
    with self.assertRaisesRegex(ValueError, "unsafe"):
      parse_directory(imagefs("../MeterCore"))
    with self.assertRaisesRegex(ValueError, "unsafe"):
      parse_directory(imagefs("/MeterCore"))

  def test_file_extent_cannot_exceed_image(self):
    image = imagefs()
    struct.pack_into("<I", image, 120, 100)
    struct.pack_into("<I", image, 252, 0)
    struct.pack_into("<I", image, 252, -sum(value[0] for value in struct.iter_unpack("<I", image)) & 0xFFFFFFFF)
    with self.assertRaisesRegex(ValueError, "outside"):
      parse_directory(image)

  def test_embedded_startup_header_requires_explicit_validated_offset(self):
    image = imagefs()
    embedded = bytes(0x224) + startup(image)
    with patch("scripts.subaru_research.qnx_ifs_inventory.lzo_block", return_value=image):
      with self.assertRaisesRegex(ValueError, "IFS header"):
        unpack_image(embedded)
      result, files, metadata = unpack_image(embedded, startup_offset=0x224)
      self.assertEqual(result, image)
      self.assertEqual(files[0].path, "usr/bin/MeterCore")
      self.assertEqual(metadata["startupFileOffset"], 0x224)
      for offset in (-1, len(embedded), 0x225):
        with self.subTest(offset=offset), self.assertRaises(ValueError):
          unpack_image(embedded, startup_offset=offset)
      with self.assertRaisesRegex(ValueError, "startup extents"):
        unpack_image(embedded[:-1], startup_offset=0x224)

  def test_complete_lzo_extent_is_required(self):
    image = imagefs()
    raw = startup(image)
    with patch("scripts.subaru_research.qnx_ifs_inventory.lzo_block", return_value=image):
      result, files, metadata = unpack_image(raw)
      self.assertEqual(result, image)
      self.assertEqual(files[0].path, "usr/bin/MeterCore")
      self.assertTrue(metadata["formatIntegrityPassed"])
      self.assertFalse(metadata["manufacturerAuthenticityEstablished"])
      with self.assertRaisesRegex(ValueError, "startup extents"):
        unpack_image(raw[:-1])


if __name__ == "__main__":
  unittest.main()
