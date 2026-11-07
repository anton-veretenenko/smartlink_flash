#!/usr/bin/env python3
"""
Восстановление flash через smtlink_dump write_flash чанками.

Зачем: bootloader/USB может отваливаться после нескольких блоков.
write_flash читает каждый 4 КиБ блок и пишет только отличающиеся,
поэтому повторный запуск того же чанка продолжает с места обрыва.

Пример:
  cd smartlink_flash
  python3 restore_chunks.py --dump dump.bin \
      --start 0xE000 --end 0x1A30A0 --chunk 0x10000

Полный дамп:
  python3 restore_chunks.py --dump dump.bin --start 0 --end 0x200000

Скрипт вызывает sudo (пароль спросит один раз на запуск).
"""
import argparse
import glob
import os
import subprocess
import sys
import time

TOOL = (os.environ.get("SMTLINK_DUMP")
        or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "smtlink_dump"))
USB_ID = "301a:2800"


def tool_base(no_sudo=False):
    """Команда запуска smtlink_dump: напрямую под root/udev или через sudo."""
    if os.geteuid() == 0 or no_sudo:
        return [TOOL]
    return ["sudo", TOOL]


def find_usb_sysfs():
    """Найти /sys/bus/usb/devices/... для 301a:2800."""
    for d in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(d, "idVendor")) as f:
                vid = f.read().strip()
            with open(os.path.join(d, "idProduct")) as f:
                pid = f.read().strip()
        except OSError:
            continue
        if vid.lower() == "301a" and pid.lower() == "2800":
            return d
    return None


def sysfs_write(path, value):
    base = ["sh"] if os.geteuid() == 0 else ["sudo", "sh"]
    return subprocess.run(base + ["-c", f"echo {value} > {path}"],
                          stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def wait_unplugged(timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = subprocess.run(["lsusb", "-d", USB_ID],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if r.returncode != 0:
                return True
        except FileNotFoundError:
            return True
        time.sleep(0.5)
    return False


def wait_for_replug():
    print("  ОТКЛЮЧИТЕ USB на 5-10 секунд и подключите снова...", file=sys.stderr)
    if not wait_unplugged(45):
        print("  устройство так и не исчезло из системы", file=sys.stderr)
        return False
    print("  ждём появления 301a:2800...", file=sys.stderr)
    return wait_usb(120)


def reset_usb():
    """Форсировать переподключение через authorized 0/1."""
    d = find_usb_sysfs()
    if not d:
        return False
    auth = os.path.join(d, "authorized")
    if not os.path.exists(auth):
        return False
    print(f"  USB reset через {auth}", file=sys.stderr)
    if not sysfs_write(auth, 0):
        print("  authorized=0 -> I/O error (порт завис)", file=sys.stderr)
        return False
    time.sleep(1)
    if not sysfs_write(auth, 1):
        print("  authorized=1 -> I/O error (порт завис)", file=sys.stderr)
        return False
    time.sleep(2)
    return wait_usb(15)


def wait_usb(timeout=60):
    """Ждём появления 301a:2800 (если lsusb доступен)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = subprocess.run(["lsusb", "-d", USB_ID],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
        except FileNotFoundError:
            return True
        time.sleep(1)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True, help="полный дамп (2 МиБ)")
    ap.add_argument("--start", default="0xE000", help="начало, по умолчанию FIRM 0xE000")
    ap.add_argument("--end", default="0x1A30A0", help="конец, по умолчанию конец FIRM")
    ap.add_argument("--chunk", default="0x4000", help="размер чанка, кратный 0x1000")
    ap.add_argument("--retries", type=int, default=8, help="попыток на чанк")
    ap.add_argument("--wait", type=int, default=60, help="ожидание USB, сек")
    ap.add_argument("--timeout", type=int, default=60,
                    help="таймаут одной попытки smtlink_dump, сек")
    ap.add_argument("--usb-timeout", type=int, default=5000,
                    help="USB-таймаут внутри инструмента, мс (5000)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="пауза между успешными чанками, сек")
    ap.add_argument("--no-sudo", action="store_true",
                    help="не использовать sudo (работает udev-правило)")
    ap.add_argument("--log", default="restore_chunks.log")
    args = ap.parse_args()

    start = int(args.start, 0)
    end = int(args.end, 0)
    chunk = int(args.chunk, 0)
    # start и chunk должны быть выровнены; end может быть не кратен 0x1000:
    # последний блок FIRM заканчивается на 0x1A30A0.
    if start % 0x1000 or chunk % 0x1000:
        print("start/chunk должны быть кратны 0x1000", file=sys.stderr)
        return 1
    if chunk <= 0 or end <= start:
        print("некорректный диапазон", file=sys.stderr)
        return 1
    if not os.path.exists(args.dump):
        print(f"нет файла {args.dump}", file=sys.stderr)
        return 1
    if not os.path.exists(TOOL):
        print(f"нет {TOOL}, сначала make", file=sys.stderr)
        return 1
    try:
        marker = subprocess.check_output(["strings", TOOL], text=True,
                                         stderr=subprocess.DEVNULL)
        if "write_flash_fixed" not in marker:
            print("ВНИМАНИЕ: smtlink_dump старый (нет write_flash_fixed). "
                  "Сделайте make clean && make", file=sys.stderr)
            return 1
    except FileNotFoundError:
        pass

    if os.geteuid() != 0 and not args.no_sudo:
        print("sudo: запрашиваю права один раз...", file=sys.stderr)
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            print("sudo не получен (или используйте --no-sudo с udev-правилом)",
                  file=sys.stderr)
            return 1

    log = open(args.log, "a", buffering=1)
    print(f"лог: {args.log}", file=sys.stderr)

    total = end - start
    done = 0
    addr = start
    while addr < end:
        size = min(chunk, end - addr)
        print(f"\n=== chunk 0x{addr:08X}..0x{addr+size:08X} "
              f"({done}/{total} = {done*100//total}%) ===", file=sys.stderr)
        ok = False
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
                # flash_id не обязателен: его отдельный статус иногда
                # теряется, а write_flash сам проверяет CRC каждого блока.
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
                print(f"  попытка {attempt}: таймаут {args.timeout}s, "
                      f"убиваю процесс", file=sys.stderr)
                rc = -999
            # write_flash сам проверяет CRC; код возврата 0 = успех
            if rc == 0:
                ok = True
                break
            print(f"  попытка {attempt} не удалась (rc={rc}), "
                  f"пробуем USB reset...", file=sys.stderr)
            if not reset_usb():
                print("  USB reset не удался — нужен физический replug",
                      file=sys.stderr)
                if not wait_for_replug():
                    print("  устройство не вернулось; остановка", file=sys.stderr)
                    log.close()
                    return 1
        if not ok:
            print(f"ЧАНК 0x{addr:08X} не записан после {args.retries} попыток",
                  file=sys.stderr)
            log.close()
            return 1
        done += size
        addr += size
        if args.delay > 0:
            time.sleep(args.delay)

    log.close()
    print(f"\nГОТОВО: 0x{start:08X}..0x{end:08X}, {total} байт", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
