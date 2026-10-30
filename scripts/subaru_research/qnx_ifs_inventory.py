#!/usr/bin/env python3
"""Read-only inventory/selected extraction of the Subaru LZO QNX IFS filesystems.

Validates declared extents and the imagefs word-sum trailer before using the directory. This verifies
format integrity, not manufacturer authenticity. No firmware execution, mounting, or vehicle access.
Layouts follow QNX startup/image headers and were checked against both local CP1.5 IFS images.
"""
import argparse
import ctypes
import ctypes.util
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import struct

MAX_IMAGE_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class ImageFile:
  path: str
  offset: int
  size: int


def parse_directory(image):
  if len(image) < 92 or image[:7] != b"imagefs":
    raise ValueError("missing imagefs header")
  size, directory_end, directory_start = struct.unpack_from("<3I", image, 8)
  if size != len(image) or size % 4 or not 92 <= directory_start < directory_end < size:
    raise ValueError("invalid imagefs extents")
  if sum(value[0] for value in struct.iter_unpack("<I", image)) & 0xFFFFFFFF:
    raise ValueError("imagefs trailer checksum mismatch")
  result = []
  cursor = directory_start
  seen = set()
  while cursor + 24 <= directory_end:
    length = struct.unpack_from("<H", image, cursor)[0]
    if length == 0:
      break
    if length < 24 or cursor + length > directory_end:
      raise ValueError("invalid directory record extent")
    inode, mode = struct.unpack_from("<II", image, cursor + 4)
    record = image[cursor:cursor + length]
    cursor += length
    if not inode or mode & 0xF000 != 0x8000:
      continue
    if length < 33 or b"\x00" not in record[32:]:
      raise ValueError("invalid regular-file directory record")
    offset, count = struct.unpack_from("<II", record, 24)
    name = record[32:].split(b"\x00", 1)[0].decode("utf-8", errors="strict")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or name in seen:
      raise ValueError("unsafe or duplicate image path")
    if offset < directory_end or offset + count > size:
      raise ValueError("file extends outside image data")
    seen.add(name)
    result.append(ImageFile(name, offset, count))
  return result


def lzo_block(source):
  name = ctypes.util.find_library("lzo2")
  if name is None:
    raise RuntimeError("the system liblzo2 library is required")
  decompress = ctypes.CDLL(name).lzo1x_decompress_safe
  decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p]
  decompress.restype = ctypes.c_int
  src = ctypes.create_string_buffer(source)
  dst = ctypes.create_string_buffer(65536)
  output_length = ctypes.c_size_t(len(dst))
  status = decompress(src, len(source), dst, ctypes.byref(output_length), None)
  if status != 0 or output_length.value > len(dst):
    raise ValueError(f"invalid LZO block: {status}")
  return dst.raw[:output_length.value]


def unpack_image(raw, startup_offset=0):
  # Hypervisor images embed the same startup/imagefs format after an outer header. Require its known
  # offset explicitly rather than guessing magic-byte matches inside compressed or unrelated data.
  if not 0 <= startup_offset < len(raw):
    raise ValueError("startup offset outside input")
  raw = raw[startup_offset:]
  if len(raw) < 256 or raw[:4] != bytes.fromhex("eb7eff00") or raw[6] & 2 or raw[6] & 0x1C != 8:
    raise ValueError("expected a little-endian LZO QNX IFS header")
  start, stored, _, expected = struct.unpack_from("<4I", raw, 32)
  if not 256 <= start < stored <= len(raw) or not 0 < expected <= MAX_IMAGE_BYTES:
    raise ValueError("invalid startup extents")
  image = bytearray()
  cursor, blocks = start, 0
  terminated = False
  while cursor + 2 <= stored:
    length = struct.unpack_from(">H", raw, cursor)[0]
    cursor += 2
    if not length:
      terminated = True
      break
    if cursor + length > stored:
      raise ValueError("truncated compressed block")
    image.extend(lzo_block(raw[cursor:cursor + length]))
    if len(image) > expected:
      raise ValueError("decompression exceeds declared image extent")
    blocks += 1
    cursor += length
  if not terminated or len(image) != expected:
    raise ValueError("incomplete compressed image")
  files = parse_directory(image)
  return bytes(image), files, {"startupFileOffset": startup_offset, "storedBytes": stored,
                                "imageBytes": expected, "lzoBlocks": blocks,
                                "formatIntegrityPassed": True, "manufacturerAuthenticityEstablished": False}


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("image", type=Path)
  parser.add_argument("--startup-offset", type=lambda value: int(value, 0), default=0,
                      help="known startup-header offset in the input, decimal or 0x-prefixed; no auto-scan")
  parser.add_argument("--extract", nargs="*", default=[], help="exact paths from the image inventory")
  parser.add_argument("--output", type=Path, help="existing output directory; required only with --extract")
  args = parser.parse_args()
  if args.extract and (args.output is None or not args.output.is_dir()):
    parser.error("--extract requires an existing --output directory")
  image, files, metadata = unpack_image(args.image.read_bytes(), startup_offset=args.startup_offset)
  selected = set(args.extract)
  if selected - {entry.path for entry in files}:
    parser.error("requested path absent from image")
  reports = []
  for entry in files:
    row = {"path": entry.path, "offset": entry.offset, "size": entry.size}
    if entry.path in selected:
      data = image[entry.offset:entry.offset + entry.size]
      # Preserve hierarchy; refuse existing paths and symlink traversal instead of following image links.
      destination = args.output / entry.path
      if any(parent.is_symlink() for parent in destination.parents):
        raise ValueError("output path traverses a symlink")
      destination.parent.mkdir(parents=True, exist_ok=True)
      with destination.open("xb") as handle:
        handle.write(data)
      row["sha256"] = hashlib.sha256(data).hexdigest()
    reports.append(row)
  print(json.dumps({"image": args.image.name, "metadata": metadata, "files": reports}, indent=2))


if __name__ == "__main__":
  main()
