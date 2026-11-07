#!/usr/bin/env python3
"""
Проверка flash по CRC кусками, без записи.

Для каждого куска:
  smtlink_dump init flash_id timeout <ms> check_flash <addr> <size>
и сравнение с CRC16 из dump.bin.

Пример:
  cd smartlink_flash
  sudo python3 verify_flash.py --dump dump.bin \
      --start 0xE000 --end 0x1A30A0 --chunk 0x4000

Таймаут USB для check_flash: --usb-timeout (по умолчанию 5000 мс).
"""
import argparse
import os
import subprocess
import sys
import time

from restore_chunks import (TOOL, USB_ID, tool_base, reset_usb, wait_usb,
                            wait_for_replug)


def crc16(data: bytes) -> int:
    c = 0xFFFF
    for b in data:
        t = (c >> 8) ^ b
        t ^= t >> 4
        c = ((c << 8) ^ t ^ (t << 5) ^ (t << 12)) & 0xFFFF
    return c


def check_chunk(addr, size, usb_timeout, args):
    """Вернуть CRC из flash или None, если устройство не ответило."""
    cmd = tool_base(args.no_sudo) + [
        "--verbose", "1", "--id", USB_ID,
        "init", "flash_id", "timeout", str(usb_timeout),
        "check_flash", f"0x{addr:X}", f"0x{size:X}",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print(f"  таймаут процесса {args.timeout}s", file=sys.stderr)
        return None
    out = (r.stdout or "") + (r.stderr or "")
    crc = None
    for line in out.splitlines():
        if "flash_crc:" in line:
            try:
                crc = int(line.split("0x")[-1].strip(), 16)
            except ValueError:
                pass
    if crc is None:
        print(f"  нет ответа (rc={r.returncode})", file=sys.stderr)
        print("  " + out.strip()[-300:], file=sys.stderr)
    return crc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--start", default="0xE000")
    ap.add_argument("--end", default="0x1A30A0")
    ap.add_argument("--chunk", default="0x4000")
    ap.add_argument("--usb-timeout", "--timeout-ms", dest="usb_timeout",
                    type=int, default=5000,
                    help="USB-таймаут внутри инструмента, мс (5000)")
    ap.add_argument("--timeout", type=int, default=30,
                    help="таймаут процесса проверки, сек (30)")
    ap.add_argument("--retries", type=int, default=3,
                    help="попыток на кусок (3)")
    ap.add_argument("--wait", type=int, default=60,
                    help="ожидание USB, сек (60)")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="пауза между кусками, сек (0.5)")
    ap.add_argument("--no-sudo", action="store_true",
                    help="не использовать sudo (работает udev-правило)")
    args = ap.parse_args()

    start = int(args.start, 0)
    end = int(args.end, 0)
    chunk = int(args.chunk, 0)
    if not os.path.exists(TOOL):
        print(f"нет {TOOL}, сначала make", file=sys.stderr)
        return 1
    try:
        data = open(args.dump, "rb").read()
    except OSError as e:
        print(f"не могу прочитать {args.dump}: {e}", file=sys.stderr)
        return 1
    if end > len(data):
        print("end за пределами dump.bin", file=sys.stderr)
        return 1
    if chunk <= 0 or start % 0x1000 or chunk % 0x1000:
        print("start/chunk должны быть кратны 0x1000", file=sys.stderr)
        return 1

    if os.geteuid() != 0 and not args.no_sudo:
        print("sudo: запрашиваю права один раз...", file=sys.stderr)
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            print("sudo не получен (или используйте --no-sudo с udev-правилом)",
                  file=sys.stderr)
            return 1

    bad = 0
    addr = start
    while addr < end:
        size = min(chunk, end - addr)
        expected = crc16(data[addr:addr + size])
        crc = None
        for attempt in range(1, args.retries + 1):
            if not wait_usb(args.wait):
                print("USB 301a:2800 не появился", file=sys.stderr)
                time.sleep(2)
                continue
            crc = check_chunk(addr, size, args.usb_timeout, args)
            if crc == expected:
                break
            if crc is not None:
                print(f"  попытка {attempt}: crc=0x{crc:04X}, "
                      f"expected=0x{expected:04X}", file=sys.stderr)
            if attempt < args.retries:
                if not reset_usb():
                    print("  USB reset не удался — нужен физический replug",
                          file=sys.stderr)
                    if not wait_for_replug():
                        print("  устройство не вернулось; остановка",
                              file=sys.stderr)
                        return 1
                else:
                    wait_for_replug()
        if crc != expected:
            print(f"0x{addr:08X}..0x{addr+size:08X}  ОШИБКА/НЕТ ОТВЕТА "
                  f"(expected=0x{expected:04X})", file=sys.stderr)
            bad += 1
            break
        print(f"0x{addr:08X}..0x{addr+size:08X}  crc=0x{crc:04X}  OK")
        addr += size
        if args.delay > 0:
            time.sleep(args.delay)

    if bad:
        print(f"\nЕСТЬ РАСХОЖДЕНИЯ/ОШИБКИ: {bad}", file=sys.stderr)
        return 1
    print(f"\nВСЁ СОВПАЛО: 0x{start:X}..0x{end:X}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
