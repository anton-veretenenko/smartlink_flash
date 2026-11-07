#!/usr/bin/env python3
"""
Запись нескольких диапазонов из одного полного дампа, по чанкам.

Основано на restore_chunks.py: тот же write_flash, повторы, USB reset,
физический replug, пауза после стирания (внутри инструмента) и пауза
между чанками.

Пример:
  cd smartlink_flash
  sudo python3 flash_ranges.py --dump dump.bin \
      0xD000:0xF000 0x1F0000:0x1F1000

  # другой размер чанка/таймаут
  sudo python3 flash_ranges.py --dump ../build/lcd_test/dump_hook.bin \
      --chunk 0x4000 --timeout 180 \
      0xD000:0xF000 0x1F0000:0x1F1000

  # запись + проверка каждого записанного блока по CRC
  sudo python3 flash_ranges.py --verify --dump dump.bin \
      0xD000:0xF000 0x1F0000:0x1F1000

Диапазоны задаются как START:END (END не включается) или START-END.
Адреса — hex (0x...) или decimal. START должен быть кратен 0x1000;
END может быть любым (последний чанк будет неполным).

По умолчанию:
  --chunk   0x1000
  --timeout 120
  --delay   1
"""
import argparse
import os
import subprocess
import sys
import time

from restore_chunks import (TOOL, USB_ID, tool_base, reset_usb, wait_usb,
                            wait_for_replug)


def pct(done, total):
    return (100.0 * done / total) if total else 100.0


def crc16(data):
    """CRC16, тот же алгоритм, что в verify_flash.py / smtlink_dump."""
    c = 0xFFFF
    for b in data:
        t = (c >> 8) ^ b
        t ^= t >> 4
        c = ((c << 8) ^ t ^ (t << 5) ^ (t << 12)) & 0xFFFF
    return c


def parse_range(text):
    sep = ":" if ":" in text else ("-" if "-" in text else None)
    if not sep:
        raise ValueError(f"диапазон {text!r}: нужен START:END")
    a, b = text.split(sep, 1)
    start, end = int(a, 0), int(b, 0)
    return start, end


def check_tool():
    if not os.path.exists(TOOL):
        print(f"нет {TOOL}, сначала make", file=sys.stderr)
        return False
    try:
        marker = subprocess.check_output(["strings", TOOL], text=True,
                                         stderr=subprocess.DEVNULL)
        if "write_flash_fixed" not in marker:
            print("ВНИМАНИЕ: smtlink_dump старый (нет write_flash_fixed). "
                  "Сделайте make clean && make", file=sys.stderr)
            return False
    except FileNotFoundError:
        pass
    return True


def write_chunk(addr, size, args, log, done, total):
    print(f"  [{pct(done, total):5.1f}%] 0x{addr:08X}..0x{addr+size:08X}",
          file=sys.stderr)
    for attempt in range(1, args.retries + 1):
        if not wait_usb(args.wait):
            print("USB 301a:2800 не появился", file=sys.stderr)
            time.sleep(2)
            continue
        # Udev-правило (см. README) даёт MODE="0666" для 301a:2800,
        # тогда запускайте с --no-sudo. Иначе инструмент идёт через sudo.
        base = tool_base(args.no_sudo)
        cmd = base + [
            "--verbose", "1", "--id", USB_ID,
            # flash_id не нужен: write_flash сам проверяет CRC.
            "timeout", str(args.usb_timeout),
            "init", "write_flash",
            f"0x{addr:X}", f"0x{addr:X}", f"0x{size:X}", args.dump,
        ]
        print(f"  попытка {attempt}: " + " ".join(cmd[1:]), file=log)
        try:
            r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                               timeout=args.timeout)
            rc = r.returncode
        except subprocess.TimeoutExpired:
            print(f"  попытка {attempt}: таймаут {args.timeout}s",
                  file=sys.stderr)
            rc = -999
        if rc == 0:
            print(f"  OK 0x{addr:08X}..0x{addr+size:08X} "
                  f"({done+size}/{total} = {pct(done+size, total):5.1f}%)",
                  file=sys.stderr)
            return True
        print(f"  попытка {attempt} не удалась (rc={rc}), USB reset...",
              file=sys.stderr)
        if not reset_usb():
            print("  USB reset не удался — нужен физический replug",
                  file=sys.stderr)
            if not wait_for_replug():
                print("  устройство не вернулось; остановка", file=sys.stderr)
                return False
    return False


def verify_chunk(addr, size, dump_data, args, log):
    """Прочитать записанный блок и сверить CRC с образом."""
    expected = crc16(dump_data[addr:addr + size])
    base = tool_base(args.no_sudo)
    cmd = base + [
        "--verbose", "1", "--id", USB_ID,
        "timeout", str(args.usb_timeout),
        "init", "flash_id", "check_flash", f"0x{addr:X}", f"0x{size:X}",
    ]
    for attempt in range(1, args.verify_retries + 1):
        if not wait_usb(args.wait):
            print("  verify: USB 301a:2800 не появился", file=sys.stderr)
            time.sleep(2)
            continue
        print(f"  verify {attempt}: " + " ".join(cmd[1:]), file=log)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=args.verify_proc_timeout)
        except subprocess.TimeoutExpired:
            print(f"  verify {attempt}: таймаут "
                  f"{args.verify_proc_timeout}s", file=sys.stderr)
            if reset_usb():
                wait_for_replug()
            continue
        out = (r.stdout or "") + (r.stderr or "")
        print(out, file=log)
        crc = None
        for line in out.splitlines():
            if "flash_crc:" in line:
                try:
                    crc = int(line.split("0x")[-1].strip(), 16)
                except ValueError:
                    pass
        if crc is None:
            print(f"  verify {attempt}: нет flash_crc (rc={r.returncode})",
                  file=sys.stderr)
        elif crc == expected:
            print(f"  VERIFY OK 0x{addr:08X}..0x{addr+size:08X} "
                  f"crc=0x{crc:04X}", file=sys.stderr)
            return True
        else:
            print(f"  VERIFY FAIL 0x{addr:08X}..0x{addr+size:08X} "
                  f"crc=0x{crc:04X} expected=0x{expected:04X}",
                  file=sys.stderr)
        if attempt < args.verify_retries:
            if not reset_usb():
                if not wait_for_replug():
                    return False
            else:
                wait_for_replug()
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True,
                    help="полный дамп (2 МиБ) или модифицированный образ")
    ap.add_argument("ranges", nargs="+", metavar="START:END",
                    help="диапазоны записи: 0xD000:0xF000 0x1F0000:0x1F1000")
    ap.add_argument("--chunk", default="0x1000",
                    help="размер чанка, кратный 0x1000 (по умолчанию 0x1000)")
    ap.add_argument("--timeout", type=int, default=120,
                    help="таймаут одной попытки smtlink_dump, сек (120)")
    ap.add_argument("--usb-timeout", type=int, default=5000,
                    help="USB-таймаут внутри инструмента, мс (5000)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="пауза между чанками, сек (1)")
    ap.add_argument("--retries", type=int, default=8,
                    help="попыток на чанк (8)")
    ap.add_argument("--wait", type=int, default=60,
                    help="ожидание USB, сек (60)")
    ap.add_argument("--no-sudo", action="store_true",
                    help="не использовать sudo (работает udev-правило)")
    ap.add_argument("--log", default="flash_ranges.log")
    ap.add_argument("--verify", action="store_true",
                    help="после каждого записанного блока прочитать его "
                         "и сверить CRC с образом")
    ap.add_argument("--verify-proc-timeout", type=int, default=30,
                    help="таймаут процесса проверки, сек (30)")
    ap.add_argument("--verify-retries", type=int, default=3,
                    help="попыток проверки блока (3)")
    ap.add_argument("--dry-run", action="store_true",
                    help="только показать план, ничего не писать")
    args = ap.parse_args()

    try:
        ranges = [parse_range(r) for r in args.ranges]
    except ValueError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 1

    chunk = int(args.chunk, 0)
    if chunk <= 0 or chunk % 0x1000:
        print("--chunk должен быть кратен 0x1000", file=sys.stderr)
        return 1

    args.dump = os.path.abspath(args.dump)
    if not os.path.exists(args.dump):
        print(f"нет файла {args.dump}", file=sys.stderr)
        return 1
    dump_size = os.path.getsize(args.dump)
    dump_data = open(args.dump, "rb").read() if args.verify else None

    plan = []
    total = 0
    prev_end = None
    for idx, (start, end) in enumerate(ranges, 1):
        if start % 0x1000:
            print(f"диапазон {idx}: start 0x{start:X} не кратен 0x1000",
                  file=sys.stderr)
            return 1
        if end <= start:
            print(f"диапазон {idx}: end <= start", file=sys.stderr)
            return 1
        if end > dump_size:
            print(f"диапазон {idx}: 0x{end:X} за пределами дампа "
                  f"(0x{dump_size:X})", file=sys.stderr)
            return 1
        chunks = []
        addr = start
        while addr < end:
            size = min(chunk, end - addr)
            chunks.append((addr, size))
            addr += size
        plan.append((start, end, chunks))
        total += end - start
        if prev_end is not None and start < prev_end:
            print(f"ВНИМАНИЕ: диапазон {idx} пересекается с предыдущим",
                  file=sys.stderr)
        prev_end = max(prev_end or 0, end)

    print(f"дамп: {args.dump} ({dump_size} байт)", file=sys.stderr)
    print(f"чанк: 0x{chunk:X}, timeout {args.timeout}s, delay {args.delay}s",
          file=sys.stderr)
    for idx, (start, end, chunks) in enumerate(plan, 1):
        print(f"  [{idx}] 0x{start:08X}..0x{end:08X}: "
              f"{len(chunks)} чанк(ов), {end - start} байт", file=sys.stderr)
    print(f"всего: {total} байт", file=sys.stderr)

    if args.dry_run:
        for idx, (start, end, chunks) in enumerate(plan, 1):
            for addr, size in chunks:
                print(f"  [{idx}] write_flash 0x{addr:X} 0x{size:X}",
                      file=sys.stderr)
        return 0

    if not check_tool():
        return 1

    if os.geteuid() != 0 and not args.no_sudo:
        print("sudo: запрашиваю права один раз...", file=sys.stderr)
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            print("sudo не получен (или используйте --no-sudo с udev-правилом)",
                  file=sys.stderr)
            return 1

    log = open(args.log, "a", buffering=1)
    print(f"лог: {args.log}", file=sys.stderr)

    done = 0
    try:
        for idx, (start, end, chunks) in enumerate(plan, 1):
            print(f"\n=== [{idx}/{len(plan)}] 0x{start:08X}.."
                  f"0x{end:08X}  (старт {pct(done, total):5.1f}%) ===",
                  file=sys.stderr)
            for addr, size in chunks:
                if not write_chunk(addr, size, args, log, done, total):
                    print(f"ЧАНК 0x{addr:08X} не записан после "
                          f"{args.retries} попыток", file=sys.stderr)
                    return 1
                if args.verify and not verify_chunk(addr, size, dump_data,
                                                    args, log):
                    print(f"ЧАНК 0x{addr:08X} не прошёл проверку CRC",
                          file=sys.stderr)
                    return 1
                done += size
                if args.delay > 0:
                    time.sleep(args.delay)
    finally:
        log.close()

    print(f"\nГОТОВО: {len(plan)} диапазон(ов), {total} байт, 100.0%",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
