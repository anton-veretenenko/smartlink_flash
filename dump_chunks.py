#!/usr/bin/env python3
"""
Полный (или частичный) дамп flash чанками, с докачкой и проверкой.

Зачем: загрузчик перестаёт отвечать после ~30 команд подряд, а read_flash
на 2 МиБ — это 512+ команд. Поэтому читаем чанками по 0x10000 (16 блоков
по 4 КиБ), между чанками пауза, при сбое — USB reset и повтор.

Пример полного дампа:
  cd smartlink_flash
  sudo python3 dump_chunks.py --out verify_dump.bin \
      --start 0 --end 0x200000 --compare dump.bin

Только FIRM:
  sudo python3 dump_chunks.py --out verify_firm.bin \
      --start 0xE000 --end 0x1A30A0 --compare dump.bin

Повторный запуск продолжает с недокачанных чанков (progress-файл рядом
с выходным файлом). Чтобы перекачать всё заново: --restart.

ВАЖНО: скрипт только читает flash, ничего не пишет в устройство.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

from restore_chunks import (TOOL, USB_ID, tool_base, reset_usb, wait_usb,
                            wait_for_replug)

# Разделы из `fwhelper dump.bin scan` (для отчёта о сравнении).
PARTITIONS = [
    ("boot", 0x000000, 0x00E000),
    ("FIRM", 0x00E000, 0x1A30A0),
    ("gap1", 0x1A30A0, 0x1A4000),
    ("TONE", 0x1A4000, 0x1A6038),
    ("gap2", 0x1A6038, 0x1FC000),
    ("PSMP", 0x1FC000, 0x200000),
]
# PSMP — пользовательские настройки, может отличаться законно.
USER_DATA = {"PSMP"}


def crc16(data):
    c = 0xFFFF
    for b in data:
        t = (c >> 8) ^ b
        t ^= t >> 4
        c = ((c << 8) ^ t ^ (t << 5) ^ (t << 12)) & 0xFFFF
    return c


def partition_of(addr):
    for name, s, e in PARTITIONS:
        if s <= addr < e:
            return name
    return "unknown"


def load_progress(path, expect):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    for k in ("out", "start", "end", "chunk"):
        if d.get(k) != expect.get(k):
            return {}
    return d.get("chunks", {})


def save_progress(path, expect, chunks):
    d = dict(expect)
    d["version"] = 1
    d["chunks"] = chunks
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, path)


def run_chunk(addr, size, args, log):
    """Прочитать один чанк. Вернуть байты или None."""
    tmp = args.out + ".chunk"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass

    # Последний чанк может быть не кратен 4 КиБ. Читаем целыми блоками
    # (загрузчик так надёжнее), лишнее отрезаем.
    read_size = (size + 0xFFF) & ~0xFFF
    read_size = min(read_size, 0x200000 - addr)

    base = tool_base(args.no_sudo)
    cmd = base + [
        "--verbose", "1", "--id", USB_ID,
        "timeout", str(args.usb_timeout),
        "init", "flash_id",
        "blk_size", "0x1000",
        "read_flash", f"0x{addr:X}", f"0x{read_size:X}", tmp,
    ]
    print(f"  попытка: " + " ".join(cmd[1:]), file=log)
    try:
        r = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                           timeout=args.timeout)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        print(f"  таймаут процесса {args.timeout}s", file=sys.stderr)
        rc = -999

    if rc != 0:
        print(f"  rc={rc}", file=sys.stderr)
        return None
    try:
        with open(tmp, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"  не могу прочитать {tmp}: {e}", file=sys.stderr)
        return None
    if len(data) != read_size:
        print(f"  короткий чанк: {len(data)} / {read_size}", file=sys.stderr)
        return None
    return data[:size]


def read_chunk(addr, size, args, log):
    """Читать чанк с повторами. Вернуть байты или None."""
    for attempt in range(1, args.retries + 1):
        if not wait_usb(args.wait):
            print("USB 301a:2800 не появился", file=sys.stderr)
            time.sleep(2)
            continue
        data = run_chunk(addr, size, args, log)
        if data is not None:
            return data
        print(f"  попытка {attempt} не удалась, USB reset...",
              file=sys.stderr)
        if not reset_usb():
            print("  USB reset не удался — нужен физический replug",
                  file=sys.stderr)
            if not wait_for_replug():
                print("  устройство не вернулось; остановка", file=sys.stderr)
                return None
    return None


def write_at(path, offset, data):
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def read_at(path, offset, size):
    with open(path, "rb") as f:
        f.seek(offset)
        return f.read(size)


def diff_ranges(a, b, max_ranges=40):
    """Непрерывные диапазоны отличий (склеиваем разрывы <= 16 байт)."""
    n = min(len(a), len(b))
    ranges = []
    start = last = None
    for i in range(n):
        if a[i] != b[i]:
            if start is None:
                start = i
            elif i - last > 16:
                ranges.append((start, last + 1))
                start = i
            last = i
    if start is not None:
        ranges.append((start, last + 1))
    return ranges[:max_ranges]


def parse_ignore(specs):
    out = []
    for sp in specs or []:
        a, _, n = sp.partition(":")
        out.append((int(a, 0), int(a, 0) + int(n, 0)))
    return out


def in_ignore(addr, ignore):
    return any(s <= addr < e for s, e in ignore)


def compare(out_path, ref_path, base_addr, ignore=()):
    """Сравнить выходной файл с эталоном. Вернуть (ok, important_diffs).

    out_path содержит диапазон flash, начинающийся с base_addr, поэтому
    сравниваем с ref[base_addr:base_addr+len(out)].
    Диапазоны ignore считаются известными и не влияют на important.
    """
    out = open(out_path, "rb").read()
    ref_all = open(ref_path, "rb").read()
    ref = ref_all[base_addr:base_addr + len(out)]
    if len(ref) < len(out):
        print(f"ЭТАЛОН КОРОЧЕ: нужно 0x{base_addr + len(out):X}, "
              f"есть 0x{len(ref_all):X}", file=sys.stderr)
    size = min(len(out), len(ref))

    per = {}
    diff_bytes = 0
    ignored = 0
    for i in range(size):
        if out[i] != ref[i]:
            diff_bytes += 1
            addr = base_addr + i
            if in_ignore(addr, ignore):
                ignored += 1
                continue
            name = partition_of(addr)
            per[name] = per.get(name, 0) + 1

    print("\n=== Сравнение с эталоном ===")
    print(f"  {ref_path}: {len(ref_all)} байт, "
          f"sha256 {hashlib.sha256(ref_all).hexdigest()}")
    print(f"  {out_path}: {len(out)} байт, sha256 {hashlib.sha256(out).hexdigest()}")
    print(f"  всего отличий: {diff_bytes} байт")
    if ignored:
        print(f"  из них в известных/игнорируемых блоках: {ignored} байт")

    important = []
    for name, s, e in PARTITIONS:
        if e <= base_addr or s >= base_addr + size:
            continue
        n = per.get(name, 0)
        status = "OK" if n == 0 else f"РАСХОЖДЕНИЕ {n} байт"
        if name in USER_DATA and n:
            status += " (пользовательские данные, допустимо)"
        print(f"  {name:6s} 0x{s:06X}..0x{e:06X}  {status}")
        if n and name not in USER_DATA:
            important.append(name)

    if diff_bytes:
        print("\n  диапазоны отличий (адреса flash):")
        for s, e in diff_ranges(out, ref):
            addr = base_addr + s
            note = " (игнорируется)" if in_ignore(addr, ignore) else ""
            print(f"    0x{addr:06X}..0x{base_addr + e:06X} "
                  f"({e - s} байт, {partition_of(addr)}){note}")

    return diff_bytes == ignored, important


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="выходной файл дампа")
    ap.add_argument("--start", default="0", help="начало, по умолчанию 0")
    ap.add_argument("--end", default="0x200000",
                    help="конец (не включая), по умолчанию 0x200000")
    ap.add_argument("--chunk", default="0x10000",
                    help="размер чанка, кратный 0x1000 (по умолчанию 0x10000)")
    ap.add_argument("--compare", help="эталонный дамп для сравнения")
    ap.add_argument("--retries", type=int, default=8, help="попыток на чанк")
    ap.add_argument("--wait", type=int, default=60, help="ожидание USB, сек")
    ap.add_argument("--timeout", type=int, default=180,
                    help="таймаут одной попытки smtlink_dump, сек")
    ap.add_argument("--usb-timeout", type=int, default=5000,
                    help="USB-таймаут внутри инструмента, мс")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="пауза между чанками, сек")
    ap.add_argument("--compare-passes", type=int, default=1,
                    help="сколько раз перечитывать отличающиеся чанки")
    ap.add_argument("--restart", action="store_true",
                    help="игнорировать progress и читать всё заново")
    ap.add_argument("--ignore", action="append", default=[],
                    metavar="ADDR:SIZE",
                    help="известный блок, не считать расхождение критичным "
                         "(можно несколько раз)")
    ap.add_argument("--compare-only", action="store_true",
                    help="не читать flash, только сравнить готовый файл")
    ap.add_argument("--no-sudo", action="store_true",
                    help="не использовать sudo (работает udev-правило)")
    ap.add_argument("--log", default="dump_chunks.log")
    args = ap.parse_args()

    start = int(args.start, 0)
    end = int(args.end, 0)
    chunk = int(args.chunk, 0)
    ignore = parse_ignore(args.ignore)
    if chunk <= 0 or chunk % 0x1000 or start % 0x1000:
        print("start/chunk должны быть кратны 0x1000", file=sys.stderr)
        return 1
    if end <= start:
        print("некорректный диапазон", file=sys.stderr)
        return 1
    if not os.path.exists(TOOL):
        print(f"нет {TOOL}, сначала make", file=sys.stderr)
        return 1

    total = end - start
    if args.compare_only:
        if not args.compare:
            print("--compare-only требует --compare", file=sys.stderr)
            return 1
        if not os.path.exists(args.out) or os.path.getsize(args.out) != total:
            print("нет готового файла нужного размера", file=sys.stderr)
            return 1
        ok, important = compare(args.out, args.compare, start, ignore)
        if ok:
            print("\nВСЁ СОВПАЛО (с учётом --ignore)", file=sys.stderr)
            return 0
        if important:
            print("\nЕСТЬ РАСХОЖДЕНИЯ В ВАЖНЫХ РАЗДЕЛАХ", file=sys.stderr)
            return 1
        print("\nОтличаются только PSMP/игнорируемые блоки", file=sys.stderr)
        return 2

    if os.geteuid() != 0 and not args.no_sudo:
        print("sudo: запрашиваю права один раз...", file=sys.stderr)
        if subprocess.run(["sudo", "-v"]).returncode != 0:
            print("sudo не получен", file=sys.stderr)
            return 1

    expect = {"out": os.path.abspath(args.out), "start": start,
              "end": end, "chunk": chunk}
    progress_path = args.out + ".progress.json"

    if args.restart:
        for p in (args.out, progress_path):
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass
    # Создаём/выравниваем выходной файл.
    if not os.path.exists(args.out) or os.path.getsize(args.out) != total:
        with open(args.out, "wb") as f:
            f.truncate(total)
        try:
            os.unlink(progress_path)
        except FileNotFoundError:
            pass

    chunks = load_progress(progress_path, expect)
    log = open(args.log, "a", buffering=1)
    print(f"лог: {args.log}", file=sys.stderr)

    addresses = list(range(start, end, chunk))
    done = 0

    for idx, addr in enumerate(addresses, 1):
        size = min(chunk, end - addr)
        key = f"0x{addr:X}"
        rec = chunks.get(key)
        off = addr - start

        if rec and rec.get("size") == size:
            data = read_at(args.out, off, size)
            if len(data) == size and crc16(data) == rec.get("crc"):
                print(f"[{idx}/{len(addresses)}] 0x{addr:06X}.."
                      f"0x{addr+size:06X} уже есть (crc 0x{rec['crc']:04X})",
                      file=sys.stderr)
                done += size
                continue

        print(f"\n=== [{idx}/{len(addresses)}] 0x{addr:06X}.."
              f"0x{addr+size:06X} ({done*100//total}%) ===",
              file=sys.stderr)
        data = read_chunk(addr, size, args, log)
        if data is None:
            print(f"ЧАНК 0x{addr:08X} не прочитан после {args.retries} "
                  f"попыток", file=sys.stderr)
            log.close()
            return 1
        crc = crc16(data)
        write_at(args.out, off, data)
        back = read_at(args.out, off, size)
        if crc16(back) != crc:
            print(f"ОШИБКА записи файла на 0x{addr:08X}", file=sys.stderr)
            log.close()
            return 1
        chunks[key] = {"size": size, "crc": crc}
        save_progress(progress_path, expect, chunks)
        done += size
        print(f"  OK, crc 0x{crc:04X}", file=sys.stderr)
        if args.delay > 0:
            time.sleep(args.delay)

    print(f"\nДАМП ГОТОВ: 0x{start:06X}..0x{end:06X}, {total} байт",
          file=sys.stderr)

    if not args.compare:
        log.close()
        return 0

    ok, important = compare(args.out, args.compare, start, ignore)

    # Если важные разделы отличаются — перечитать отличающиеся чанки.
    for p in range(args.compare_passes):
        if not important:
            break
        out = open(args.out, "rb").read()
        ref = open(args.compare, "rb").read()
        targets = []
        for addr in addresses:
            size = min(chunk, end - addr)
            s = addr - start
            e = s + size
            chunk = out[s:e]
            refc = ref[addr:addr + size]
            if chunk != refc:
                unexpected = False
                for i in range(size):
                    if chunk[i] != refc[i] and not in_ignore(addr + i, ignore):
                        unexpected = True
                        break
                if unexpected:
                    targets.append(addr)
        if not targets:
            break
        print(f"\n=== Перечитываю {len(targets)} отличающихся чанков "
              f"(проход {p+1}) ===", file=sys.stderr)
        for addr in targets:
            size = min(chunk, end - addr)
            print(f"  0x{addr:06X}..0x{addr+size:06X}", file=sys.stderr)
            # Старая запись могла быть битой — снимаем отметку, чтобы
            # при следующем запуске чанк не пропустился.
            chunks.pop(f"0x{addr:X}", None)
            save_progress(progress_path, expect, chunks)
            data = read_chunk(addr, size, args, log)
            if data is None:
                log.close()
                return 1
            write_at(args.out, addr - start, data)
            chunks[f"0x{addr:X}"] = {"size": size, "crc": crc16(data)}
            save_progress(progress_path, expect, chunks)
        ok, important = compare(args.out, args.compare, start, ignore)

    log.close()
    if ok:
        print("\nВСЁ СОВПАЛО", file=sys.stderr)
        return 0
    if important:
        print("\nЕСТЬ РАСХОЖДЕНИЯ В ВАЖНЫХ РАЗДЕЛАХ", file=sys.stderr)
        return 1
    print("\nОтличается только PSMP (пользовательские данные)", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
