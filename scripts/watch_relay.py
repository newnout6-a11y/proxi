"""Read plain relay.log with terminal colors, including WRITE alerts and balance."""
import argparse
import collections
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import console_log


def lines(path, *, tail=30, follow=True):
    """Follow appends and reopen after truncation/rotation without changing the file."""
    first = True
    while True:
        try:
            with path.open(encoding="utf-8-sig", errors="replace") as handle:
                signature = path.stat().st_ino
                if first:
                    yield from collections.deque(handle, maxlen=tail)
                    first = False
                while True:
                    line = handle.readline()
                    if line:
                        yield line
                        continue
                    if not follow:
                        return
                    time.sleep(0.2)
                    stat = path.stat()
                    if stat.st_ino != signature or stat.st_size < handle.tell():
                        break
        except FileNotFoundError:
            if not follow:
                raise
            time.sleep(0.2)


def demo():
    monitor = console_log.CacheMonitor()
    remaining = 1430
    samples = [("req-warm", 180_000, 128, 0.7744), ("req-read", 225_000, 0, 0.9),
               ("req-spike", 0, 190_000, 19.0)]
    for request, read, write, cost in samples:
        remaining -= cost
        console_log.write(monitor.line(request, {"inputTokens": 4, "promptTokensReadFromCache": read,
                          "promptTokensWrittenToCache": write, "creditsUsed": cost}))
        console_log.write(console_log.balance_line(request, {"available": remaining,
                          "monthly_allocation": 5000, "estimated": True}, cost))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "Proxi/state/relay.log"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path,
                        default=local if local.exists() else Path(__file__).resolve().parents[1] / "relay.log")
    parser.add_argument("--lines", type=int, default=30)
    parser.add_argument("--no-follow", action="store_true")
    parser.add_argument("--demo", action="store_true", help="Show synthetic samples without contacting a model")
    args = parser.parse_args()
    if args.lines < 0:
        parser.error("--lines must be non-negative")
    try:
        if args.demo:
            demo()
        else:
            for line in lines(args.path, tail=args.lines, follow=not args.no_follow):
                # Older Windows PowerShell appended UTF-16 lines to the same file.
                console_log.write(line.replace("\x00", "").rstrip("\r\n"))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
