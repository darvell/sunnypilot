#!/usr/bin/env python3
import argparse
import json
import struct
from pathlib import Path

STC_CAN_TABLE_OFFSET = 0x22B8A0
STC_CAN_TABLE_COUNT = 17
STC_CAN_ENTRY = struct.Struct("<IIIIQII")
STC_SIGNAL_ENTRY_SIZE = 60

DISPMET_RCV_TABLE_OFFSET = 0x3AE0
DISPMET_RCV_TABLE_COUNT = 54
DISPMET_RCV_ENTRY = struct.Struct("<IHH")


class ElfImage:
  def __init__(self, data: bytes):
    if data[:6] != b"\x7fELF\x02\x01":
      raise ValueError("expected a 64-bit little-endian ELF image")
    self.data = data
    phoff = struct.unpack_from("<Q", data, 0x20)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 0x36)
    self.loads = []
    for idx in range(phnum):
      off = phoff + idx * phentsize
      p_type, _flags, p_offset, p_vaddr, _paddr, p_filesz, _memsz, _align = struct.unpack_from("<IIQQQQQQ", data, off)
      if p_type == 1:
        self.loads.append((p_vaddr, p_vaddr + p_filesz, p_offset))

  def file_offset(self, virtual_address: int) -> int:
    for start, end, file_start in self.loads:
      if start <= virtual_address < end:
        return file_start + virtual_address - start
    raise ValueError(f"virtual address {virtual_address:#x} is not backed by a LOAD segment")

  def unpack_from(self, layout: struct.Struct, virtual_address: int):
    return layout.unpack_from(self.data, self.file_offset(virtual_address))

  def slice(self, virtual_address: int, size: int) -> bytes:
    offset = self.file_offset(virtual_address)
    return self.data[offset:offset + size]


def c_string(data: bytes) -> str:
  return data.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def extract_can_signals(stc: ElfImage) -> list[dict]:
  messages = []
  for idx in range(STC_CAN_TABLE_COUNT):
    address = STC_CAN_TABLE_OFFSET + idx * STC_CAN_ENTRY.size
    can_id, enabled, signal_count, kind, signal_ptr, table_index, reserved = stc.unpack_from(STC_CAN_ENTRY, address)

    signals = []
    for signal_idx in range(signal_count):
      signal_address = signal_ptr + signal_idx * STC_SIGNAL_ENTRY_SIZE
      signal_data = stc.slice(signal_address, STC_SIGNAL_ENTRY_SIZE)
      output_index = struct.unpack_from("<I", signal_data)[0]
      name = c_string(signal_data[4:36])
      byte, bit, value_mask = struct.unpack_from("<BBB", signal_data, 36)
      signals.append({
        "output_index": output_index,
        "name": name,
        "byte": byte,
        "bit": bit,
        "value_mask": value_mask,
        "width": value_mask.bit_length(),
      })

    messages.append({
      "can_id": can_id,
      "enabled": enabled,
      "kind": kind,
      "table_index": table_index,
      "reserved": reserved,
      "signals": signals,
    })
  return messages


def extract_meter_map(libdispmet: ElfImage) -> list[dict]:
  mappings = []
  for idx in range(DISPMET_RCV_TABLE_COUNT):
    address = DISPMET_RCV_TABLE_OFFSET + idx * DISPMET_RCV_ENTRY.size
    signal_id, byte_position, clear_mask = libdispmet.unpack_from(DISPMET_RCV_ENTRY, address)
    mappings.append({
      "signal_id": signal_id,
      "byte_position": byte_position,
      "clear_mask": clear_mask,
      "value_mask": (~clear_mask) & 0xFF,
    })
  return mappings


def main() -> None:
  parser = argparse.ArgumentParser(description="Extract leaked CAN signal metadata from the Subaru cockpit QNX firmware")
  parser.add_argument("--stc", type=Path, required=True, help="path to the QNX usr/bin/stc binary")
  parser.add_argument("--libdispmet", type=Path, required=True, help="path to the QNX usr/lib/libdispmet.so binary")
  parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
  args = parser.parse_args()

  messages = extract_can_signals(ElfImage(args.stc.read_bytes()))
  meter_map = extract_meter_map(ElfImage(args.libdispmet.read_bytes()))
  result = {
    "can_messages": messages,
    "meter_receive_map": meter_map,
    "known_high_level_chain": {
      "property": "Param_Current_Meter.iacc",
      "struct_offset": 0x84,
      "param_meter_byte": 37,
      "param_meter_bit": 1,
      "meter_signal_id": 0x5D,
      "meter_middle_byte": next(m["byte_position"] for m in meter_map if m["signal_id"] == 0x5D),
    },
  }

  if args.json:
    print(json.dumps(result, indent=2))
    return

  for message in messages:
    print(f"CAN {message['can_id']:#05x}")
    for signal in message["signals"]:
      print(f"  byte {signal['byte']} bit {signal['bit']} width {signal['width']}: {signal['name']}")

  iacc = result["known_high_level_chain"]
  print("\nMeter leak")
  print(f"  {iacc['property']} <- Param_Meter byte {iacc['param_meter_byte']} bit {iacc['param_meter_bit']}")
  print(f"  MeterMiddle signal {iacc['meter_signal_id']:#x} <- byte {iacc['meter_middle_byte']:#x}")


if __name__ == "__main__":
  main()
