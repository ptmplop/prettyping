#!/usr/bin/env python3
"""A fake ping, for developing, testing and demonstrating prettyping without
a network:

    ./prettyping --pingbin tests/fakeping.py [options] example.com

prettyping passes the options below through to it, like any ping option.

  -c N           stop after N packets, like ping
  -i SECONDS     interval between packets (default: 1)
  --style S      macos (default), iputils, busybox, ping6 (macOS) or
                 httping output
  -6             with --style iputils: the IPv6 header, which iputils
                 prints differently
  -D             with --style iputils: a [timestamp] before each line
  -O             with --style iputils: report each lost packet
  --apple-time   with --style macos: the time of day before each reply
  --seq N        the first icmp_seq (to test the wrap at 65536)
  --no-header    print no PING line, as if the run had started long ago
  --seed N       random seed (default: 1); a seed always gives the same run
  --base MS      baseline latency in milliseconds (default: 14)
  --jitter PCT   typical jitter, in percent of the baseline (default: 7)
  --script FILE  play these events instead of random ones, one per line:
                   <rtt>          a reply, e.g. 12.5
                   lost           no reply (macos style prints a timeout)
                   dup <rtt>      a duplicate reply for the previous packet
                   late <n> <rtt> a reply for the packet n packets back
                   msg <text>     a line on stderr, e.g. ping: sendto: ...
                   sleep <s>      a pause without sending anything
                   wait           stop sending, but keep running until ^C

Like ping, it prints the statistics and exits on SIGINT (^C), even when it
was started with SIGINT ignored, as prettyping does. It exits with status 0
when a reply came back, and 2 (macos, ping6) or 1 (the others) otherwise.
"""
import math
import random
import signal
import sys
import time

IP = "93.184.215.14"
IP6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"


class Ping:
    def __init__(self, host, style, count, ipv6=False, stamps=False, first=None,
                 outstanding=False, apple_time=False):
        self.outstanding = outstanding
        self.apple_time = apple_time
        self.host = host
        self.style = style
        self.count = count
        self.ipv6 = ipv6
        self.stamps = stamps
        self.first = first if first is not None else (1 if style == "iputils" else 0)
        self.sent = 0
        self.rtts = []
        self.start = time.monotonic()

    def out(self, line, err=False, stamp=False):
        stream = sys.stderr if err else sys.stdout
        if stamp and self.stamps:
            line = f"[{time.time():.6f}] " + line
        stream.write(line + "\n")
        stream.flush()

    def header(self):
        if self.style == "iputils" and self.ipv6:
            self.out(f"PING {self.host} ({IP6}) 56 data bytes")
        elif self.style == "iputils":
            self.out(f"PING {self.host} ({IP}) 56(84) bytes of data.")
        elif self.style == "ping6":
            self.out(f"PING6(56=40+8+8 bytes) ::1 --> {IP6}")
        elif self.style == "httping":
            self.out(f"PING {self.host}:80 (/):")
        else:
            self.out(f"PING {self.host} ({IP}): 56 data bytes")

    def reply_line(self, seq, rtt, dup=False):
        seq %= 65536
        if self.style == "httping":
            line = f"connected to {IP}:80 (273 bytes), seq={seq} time={rtt:.2f} ms"
        elif self.style == "ping6":
            line = f"16 bytes from {IP6}, icmp_seq={seq} hlim=57 time={rtt:.3f} ms"
        elif self.style == "iputils":
            t = f"{rtt:.3g}" if rtt < 100 else f"{rtt:.0f}"
            line = f"64 bytes from {IP}: icmp_seq={seq} ttl=57 time={t} ms"
        elif self.style == "busybox":
            line = f"64 bytes from {IP}: seq={seq} ttl=57 time={rtt:.3f} ms"
        else:
            line = f"64 bytes from {IP}: icmp_seq={seq} ttl=57 time={rtt:.3f} ms"
        if dup:
            line += " (DUP!)"
        if self.apple_time:
            line = time.strftime("%H:%M:%S") + f".{int(time.time() * 1e6) % 1000000:06d} " + line
        self.out(line, stamp=True)

    def send(self, rtt):
        """Sends the next packet; rtt is None for a lost one."""
        seq = self.first + self.sent
        self.sent += 1
        if rtt is None:
            if self.style == "macos":
                self.out(f"Request timeout for icmp_seq {seq % 65536}")
            elif self.style == "httping":
                self.out("could not connect (Connection refused)", err=True)
            elif self.style == "iputils" and self.outstanding:
                self.out(f"no answer yet for icmp_seq={seq % 65536}", stamp=True)
        else:
            self.rtts.append(rtt)
            self.reply_line(seq, rtt)
        return seq

    def summary(self):
        tx, rx = self.sent, len(self.rtts)
        loss = (tx - rx) * 100 / tx if tx else 0
        self.out("")
        if self.style == "httping":
            self.out(f"--- http://{self.host}/ ping statistics ---")
            ms = int((time.monotonic() - self.start) * 1000)
            self.out(f"{tx} connects, {rx} ok, {loss:.2f}% failed, time {ms}ms")
            return
        self.out(f"--- {self.host} {'ping6' if self.style == 'ping6' else 'ping'} statistics ---")
        if self.style == "iputils":
            ms = int((time.monotonic() - self.start) * 1000)
            self.out(f"{tx} packets transmitted, {rx} received, {loss:.0f}% packet loss, time {ms}ms")
        elif self.style == "busybox":
            self.out(f"{tx} packets transmitted, {rx} packets received, {loss:.0f}% packet loss")
        else:
            self.out(f"{tx} packets transmitted, {rx} packets received, {loss:.1f}% packet loss")
        if rx:
            mn, mx = min(self.rtts), max(self.rtts)
            avg = sum(self.rtts) / rx
            sd = math.sqrt(sum((r - avg) ** 2 for r in self.rtts) / rx)
            name = "rtt min/avg/max/mdev" if self.style == "iputils" else "round-trip min/avg/max/stddev"
            if self.style == "busybox":
                self.out(f"round-trip min/avg/max = {mn:.3f}/{avg:.3f}/{mx:.3f} ms")
            else:
                self.out(f"{name} = {mn:.3f}/{avg:.3f}/{mx:.3f}/{sd:.3f} ms")


def random_events(seed, base, jitter):
    """Plausible latency: a slow drift, jitter, spikes and bursts of loss."""
    rng = random.Random(seed)
    k = 0
    burst = slow = 0
    while True:
        k += 1
        if burst:
            burst -= 1
            yield ("lost",)
            continue
        if rng.random() < 0.010:
            burst = rng.randint(2, 6) - 1
            yield ("lost",)
            continue
        if rng.random() < 0.004:
            yield ("lost",)
            continue
        if slow:
            slow -= 1
        elif rng.random() < 0.012:
            slow = rng.randint(8, 20)
        rtt = base * (1 + 0.12 * math.sin(k / 23)) + abs(rng.gauss(0, base * jitter / 100))
        if slow:
            rtt += base * (0.8 + 0.4 * rng.random())
        if rng.random() < 0.035:
            rtt *= rng.uniform(2.2, 7.5)
        yield (round(rtt, 3),)


def script_events(path):
    with open(path) as f:
        for raw in f:
            parts = raw.split(None, 1)
            if not parts or parts[0].startswith("#"):
                continue
            word, rest = parts[0], (parts[1].strip() if len(parts) > 1 else "")
            if word in ("lost", "dup", "late", "msg", "sleep", "wait"):
                yield (word, rest)
            else:
                yield (float(word),)


def main():
    args = sys.argv[1:]
    count, interval, style, seed, base, script, host = 0, 1.0, "macos", 1, 14.0, None, "example.com"
    jitter = 7.0
    ipv6 = stamps = outstanding = apple_time = no_header = False
    first = None
    i = 0
    while i < len(args):
        a = args[i]
        val = args[i + 1] if i + 1 < len(args) else ""
        if a == "-c":
            count, i = int(val), i + 1
        elif a == "-i":
            interval, i = float(val), i + 1
        elif a == "--style":
            style, i = val, i + 1
        elif a == "--seed":
            seed, i = int(val), i + 1
        elif a == "--base":
            base, i = float(val), i + 1
        elif a == "--jitter":
            jitter, i = float(val), i + 1
        elif a == "--script":
            script, i = val, i + 1
        elif a == "--seq":
            first, i = int(val), i + 1
        elif a == "-6":
            ipv6 = True
        elif a == "-D":
            stamps = True
        elif a == "-O":
            outstanding = True
        elif a == "--apple-time":
            apple_time = True
        elif a == "--no-header":
            no_header = True
        elif not a.startswith("-"):
            host = a
        i += 1

    ping = Ping(host, style, count, ipv6, stamps, first, outstanding, apple_time)

    def status():
        if ping.rtts:
            return 0
        return 2 if style in ("macos", "ping6") else 1

    def on_sigint(signum, frame):
        ping.summary()
        sys.exit(status())

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)

    if not no_header:
        ping.header()
    events = script_events(script) if script else random_events(seed, base, jitter)
    last_seq = None
    for ev in events:
        if count and ping.sent >= count and ev[0] not in ("msg", "dup", "late"):
            break
        kind = ev[0]
        if kind == "sleep":
            time.sleep(float(ev[1]))
            continue
        if kind == "wait":
            while True:
                time.sleep(3600)
        if kind == "msg":
            ping.out(ev[1], err=True)
            continue
        if kind == "dup":
            if last_seq is not None:
                ping.reply_line(last_seq, float(ev[1]), dup=True)
            continue
        if kind == "late":
            back, rtt = ev[1].split()
            seq = ping.first + ping.sent - 1 - int(back)
            ping.rtts.append(float(rtt))
            ping.reply_line(seq, float(rtt))
            continue
        last_seq = ping.send(None if kind == "lost" else kind)
        time.sleep(interval)
    ping.summary()
    sys.exit(status())


if __name__ == "__main__":
    main()
