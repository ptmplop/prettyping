#!/usr/bin/env python3
"""Tests for prettyping, driven by the fake ping in this directory.

    tests/run_tests.py                  # with every awk found on this machine
    tests/run_tests.py --awk mawk       # with one awk
    tests/run_tests.py -k late          # only tests whose name contains "late"
    tests/run_tests.py --bash /bin/bash # with another bash (default: the first in PATH)

The accounting tests run prettyping with --noterminal and read the numbers
back from its statistics lines and its summary. The screen tests run it on a
pseudo-terminal, emulated with pyte (pip install pyte), and check the layout;
they are skipped when pyte is not installed.
"""
import argparse
import fcntl
import os
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PRETTYPING = os.path.join(os.path.dirname(HERE), "prettyping")
FAKEPING = os.path.join(HERE, "fakeping.py")


############################################################
# Running prettyping

def script_file(events):
    f = tempfile.NamedTemporaryFile("w", suffix=".events", delete=False)
    f.write("\n".join(str(e) for e in events) + "\n")
    f.close()
    return f.name


def child_signals():
    # Python ignores SIGPIPE; a real shell does not.
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)


def run_piped(cfg, events, args=(), ping_args=(), interrupt_after=None, columns=200, timeout=60):
    """Runs prettyping with --noterminal; returns (exit status, output)."""
    path = script_file(events)
    cmd = [cfg.bash, PRETTYPING, "--awkbin", cfg.awk, "--noterminal", "--nocolor",
           "--columns", str(columns), "--pingbin", FAKEPING, *args,
           "-i", "0", "--script", path, *ping_args, "test.example"]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             preexec_fn=child_signals)
        if interrupt_after is not None:
            time.sleep(interrupt_after)
            os.killpg(p.pid, signal.SIGINT)
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.decode("utf-8", "replace")
    finally:
        os.unlink(path)


def numbers(out):
    """The figures printed in the summary and the last session statistics line."""
    n = {}
    for key, pat in (("sent", r"(\d+) sent"), ("received", r"(\d+) received"),
                     ("lost", r"(\d+) lost \("), ("dup", r"(\d+) duplicated"),
                     ("late", r"(\d+) late")):
        m = re.search(pat, out)
        n[key] = int(m.group(1)) if m else 0
    lines = [l for l in out.splitlines() if l.lstrip().startswith("session ")]
    line = lines[-1] if lines else ""
    for key in ("min", "avg", "p95", "max", "jit"):
        m = re.search(key + r" +([0-9.]+)", line)
        if m:
            n[key] = float(m.group(1))
    m = re.search(r"loss +([<0-9.]+)%", line)
    if m:
        n["loss"] = float(m.group(1).lstrip("<"))
    return n


def close(a, b):
    """Equal to the three significant digits prettyping prints."""
    return abs(a - b) <= max(abs(b) * 0.006, 0.0006)


def compare(got, want):
    errors = []
    for key, value in want.items():
        if key not in got:
            errors.append(f"{key} missing")
        elif key in ("sent", "received", "lost", "dup", "late"):
            if got[key] != value:
                errors.append(f"{key}={got[key]}, want {value}")
        elif not close(got[key], value):
            errors.append(f"{key}={got[key]}, want {value}")
    return errors


############################################################
# Accounting tests: (name, style, events, extra ping args, expected figures)

MOS400 = [17.0] * 18 + ["lost"] * 2 + [17.0] * 18

ACCOUNTING = [
    ("steady", "macos", [10, 20, 30, 40], (),
     dict(sent=4, received=4, lost=0, min=10, max=40, avg=25, jit=10)),
    ("one reply", "macos", [12.5], (),
     dict(sent=1, received=1, lost=0, min=12.5, max=12.5, avg=12.5, jit=0)),
    ("sub-millisecond", "iputils", [0.05, 0.123, 0.08], (),
     dict(sent=3, received=3, min=0.05, max=0.123)),
    ("first lost, macos timeouts", "macos", ["lost", "lost", 10, 20], (),
     dict(sent=4, received=2, lost=2, min=10, max=20)),
    ("first lost, silent iputils", "iputils", ["lost", "lost", 10, 20], (),
     dict(sent=4, received=2, lost=2)),
    ("first lost, silent busybox", "busybox", ["lost", 10, 20], (),
     dict(sent=3, received=2, lost=1)),
    ("busybox seq=", "busybox", [10, 20, 30], (),
     dict(sent=3, received=3, lost=0, avg=20)),
    ("duplicate", "macos", [10, "dup 11", 20], (),
     dict(sent=2, received=2, lost=0, dup=1, max=20)),
    ("late reply", "iputils", [10, "lost", 20, "late 1 500", 30], (),
     dict(sent=4, received=4, lost=0, late=1, max=500)),
    ("trailing silent loss with -c", "iputils", [10, 20, "lost", "lost"], ("-c", "4"),
     dict(sent=4, received=2, lost=2)),
    ("all lost with -c", "iputils", ["lost", "lost", "lost"], ("-c", "3"),
     dict(sent=3, received=0, lost=3)),
    ("unreachable marks the loss", "iputils",
     [10, "lost", "msg From 10.0.0.1 icmp_seq=2 Destination Host Unreachable", 20], (),
     dict(sent=3, received=2, lost=1)),
    ("iputils IPv6 counts from 1", "iputils", [10, 20, 30], ("-6",),
     dict(sent=3, received=3, lost=0)),
    ("iputils IPv6 trailing loss", "iputils", [10, 20, "lost", "lost"], ("-6", "-c", "4"),
     dict(sent=4, received=2, lost=2)),
    ("ping6 trailing loss", "ping6", [10, 20, "lost", "lost"], ("-c", "4"),
     dict(sent=4, received=2, lost=2)),
    ("ping6 dead host", "ping6", ["lost", "lost", "lost"], ("-c", "3"),
     dict(sent=3, received=0, lost=3)),
    ("iputils -D", "iputils", [10, 20, "lost", 30], ("-D",),
     dict(sent=4, received=3, lost=1)),
    ("iputils -D -O", "iputils", [10, "lost", "lost", 20], ("-D", "-O"),
     dict(sent=4, received=2, lost=2)),
    ("macOS --apple-time", "macos", [10, 20, 30], ("--apple-time",),
     dict(sent=3, received=3, lost=0, max=30)),
    ("httping trailing failures", "httping", [10, 20, "lost", "lost"], ("-c", "4"),
     dict(sent=4, received=2, lost=2)),
    ("seq wraps at 65536", "iputils", [10, 11, 12, 13, 14, 15], ("--seq", "65533", "--no-header"),
     dict(sent=6, received=6, lost=0)),
    ("seq wrap with a loss", "iputils", [10, 11, "lost", 13, 14], ("--seq", "65534", "--no-header"),
     dict(sent=5, received=4, lost=1)),
    ("seq wrap with a late reply", "iputils", [10, "lost", 12, "late 1 90", 13], ("--seq", "65535", "--no-header"),
     dict(sent=4, received=4, lost=0, late=1)),
]


def test_accounting(cfg, style, events, ping_args, want):
    status, out = run_piped(cfg, events, ping_args=("--style", style, *ping_args))
    return compare(numbers(out), want), out


def test_messages_hidden(cfg):
    """ping's own statistics never show up as messages."""
    errors = []
    for style in ("macos", "ping6", "iputils", "httping"):
        _, out = run_piped(cfg, [10, 20], ping_args=("--style", style))
        if "statistics" in out or "transmitted" in out or "connects" in out:
            errors.append(f"{style}: ping statistics printed")
    return errors, out


def test_recent_window(cfg):
    _, out = run_piped(cfg, [10, 20, 30, 40, 50], args=("--last", "3"))
    line = next((l for l in out.splitlines()[::-1] if l.lstrip().startswith("last 3 ")), "")
    errors = []
    for key, value in (("min", 30), ("max", 50), ("avg", 40)):
        m = re.search(key + r" +([0-9.]+)", line)
        if not m or not close(float(m.group(1)), value):
            errors.append(f"last-3 {key}: {m.group(1) if m else 'missing'}, want {value}")
    return errors, out


def test_row_stats_cover_the_row(cfg):
    """The stats under a row describe that row, not the first packet of the next."""
    _, out = run_piped(cfg, ["lost"] * 4 + [500, 10], args=("--last", "4"),
                       ping_args=("--style", "iputils"))
    lines = out.splitlines()
    i = next(i for i, l in enumerate(lines) if "✕✕✕✕" in l)
    errors = []
    if "max     -" not in lines[i + 1] or "loss   100%" not in lines[i + 1]:
        errors.append(f"session line under the first row: {lines[i + 1].strip()}")
    return errors, out


def test_exact_percentiles(cfg):
    _, out = run_piped(cfg, [10, 20, 30, 40])
    errors = [] if re.search(r"p50 20\.0", out) and re.search(r"p95 40\.0", out) else ["p50/p95 not exact"]
    return errors, out


def test_jitter_rows_agree(cfg):
    """With a late reply, the session and last-N jitter use the same order."""
    _, out = run_piped(cfg, [10, 10, "lost", 20, "late 1 80", 30, 40],
                       ping_args=("--style", "iputils"))
    jit = re.findall(r"(?:session|last 60) .*jit +([0-9.]+)", out)
    return ([] if len(set(jit[-2:])) == 1 else [f"jitter differs: {jit[-2:]}"]), out


def test_tiny_bucket(cfg):
    _, out = run_piped(cfg, [0.008, 0.004, 0.045], ping_args=("--style", "iputils"))
    return ([] if "<0.02 ms" in out else ["no <0.02 ms bucket"]), out


def test_mos_word_matches(cfg):
    _, out = run_piped(cfg, MOS400)
    m = re.search(r"MOS (\d\.\d\d) (\S+) (\w+)", out)
    if not m:
        return ["no MOS"], out
    value, meter, word = float(m.group(1)), m.group(2), m.group(3)
    want = "excellent" if value >= 4.3 else "good" if value >= 4.0 else "fair" if value >= 3.6 \
        else "poor" if value >= 3.1 else "bad"
    lit = {"excellent": 5, "good": 4, "fair": 3, "poor": 2, "bad": 1}[want]
    errors = []
    if word != want:
        errors.append(f"MOS {value} says {word}")
    if meter.count("▰") != lit:
        errors.append(f"meter {meter} for {word}")
    return errors, out


def test_card_keeps_counts(cfg):
    """At 80 columns the duplicate and late counts still make it into the summary."""
    _, out = run_piped(cfg, [10, "dup 11", "lost", 20, "late 1 40", 30],
                       ping_args=("--style", "iputils"), columns=80)
    errors = [f"{w} missing" for w in ("1 duplicated", "1 late") if w not in out]
    return errors, out


def test_legend(cfg):
    _, out = run_piped(cfg, [10, 20])
    _, quiet = run_piped(cfg, [10, 20], args=("--nolegend",))
    errors = []
    if "▁▂▃▄▅▆▇█" not in out:
        errors.append("no legend in piped output")
    if "▁▂▃▄▅▆▇█" in quiet:
        errors.append("--nolegend did not remove it")
    return errors, out


def test_interrupt_in_flight(cfg):
    """^C while waiting: the packet in flight is not counted as lost."""
    status, out = run_piped(cfg, [10, 20, "wait"], ping_args=("--style", "iputils"),
                            interrupt_after=1.5)
    got = numbers(out)
    errors = compare(got, dict(sent=2, received=2, lost=0))
    if status != 0:
        errors.append(f"exit status {status}, want 0")
    return errors, out


def test_exit_status(cfg):
    """prettyping exits with the status of ping."""
    errors = []
    for style, want in (("iputils", 1), ("macos", 2)):
        status, out = run_piped(cfg, ["lost", "lost"], ping_args=("--style", style, "-c", "2"))
        if status != want:
            errors.append(f"{style}: exit status {status}, want {want}")
    return errors, out


def test_bad_options(cfg):
    errors = []
    for args in (["--last", "abc"], ["--rttmin", "x"], ["--rttmin", "5", "--rttmax", "2"],
                 ["--colors", "17"], ["--theme", "blue"], ["--awkbin", "no-such-awk"], ["--last"]):
        p = subprocess.run([cfg.bash, PRETTYPING, *args, "host"], capture_output=True, text=True)
        if p.returncode == 0 or "prettyping" not in p.stderr:
            errors.append(f"{' '.join(args)}: status {p.returncode}, stderr {p.stderr!r}")
    return errors, ""


def test_decimal_rtt_scale(cfg):
    status, out = run_piped(cfg, [10, 20], args=("--rttmin", "1.5", "--rttmax", "250"))
    errors = [] if status == 0 and "2 received" in out else [f"status {status}"]
    if "1.5 ms → 250 ms" not in out:
        errors.append("legend does not say 1.5 ms → 250 ms")
    return errors, out


############################################################
# Screen tests, on a pseudo-terminal emulated by pyte

def make_screen(pyte, cols, rows):
    class Screen(pyte.Screen):
        """Remembers every character written to the last column: a real
        terminal leaves the cursor on it, and the next erase eats it."""

        def __init__(self, *a):
            super().__init__(*a)
            self.last_column = []

        def draw(self, data):
            if self.cursor.x + len(data) >= self.columns:
                self.last_column.append((self.cursor.y, data))
            super().draw(data)

    return Screen(cols, rows)


def run_pty(pyte, cols, rows, cmd, interrupt_at=None, snap_at=(), resize=None, env=None, duration=30):
    screen = make_screen(pyte, cols, rows)
    stream = pyte.ByteStream(screen)
    pid, fd = os.forkpty()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        for sig in (signal.SIGINT, signal.SIGPIPE):
            signal.signal(sig, signal.SIG_DFL)
        e = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        e.pop("COLORFGBG", None)
        e.update(env or {})
        os.execvpe(cmd[0], cmd, e)
    raw = bytearray()
    snaps = []
    start = time.monotonic()
    pending = sorted(snap_at)
    while True:
        now = time.monotonic() - start
        while pending and pending[0] <= now:
            pending.pop(0)
            snaps.append([screen.display[y] for y in range(screen.lines)])
        if interrupt_at is not None and now >= interrupt_at:
            os.write(fd, b"\x03")
            interrupt_at = None
        if resize and now >= resize[0]:
            _, rcols, rrows = resize
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rrows, rcols, 0, 0))
            screen.resize(rrows, rcols)
            os.killpg(pid, signal.SIGWINCH)
            resize = None
        if now > duration:
            os.kill(pid, signal.SIGKILL)
        r, _, _ = select.select([fd], [], [], 0.02)
        if r:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            raw += data
            stream.feed(data)
    os.waitpid(pid, 0)
    return screen, snaps, bytes(raw)


def screen_cmd(cfg, events, *args):
    path = script_file(events)
    return path, [cfg.bash, PRETTYPING, "--awkbin", cfg.awk, "--pingbin", FAKEPING,
                  *args, "-i", "0.05", "--script", path, "test.example"]


SCREEN_EVENTS = [14, 15, 13, 16, 90, 14, "lost", "lost", 15, 14] * 12


def test_screen(cfg, pyte, cols, rows, *args):
    path, cmd = screen_cmd(cfg, SCREEN_EVENTS, *args)
    try:
        screen, snaps, raw = run_pty(pyte, cols, rows, cmd, snap_at=(3,))
    finally:
        os.unlink(path)
    errors = []
    if screen.last_column:
        errors.append(f"wrote in the last column: {screen.last_column[:3]}")
    final = "\n".join(screen.display)
    # The summary may be taller than the screen: read it from the output.
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw.decode("utf-8", "replace"))
    if not re.search(r"24 lost", text):
        errors.append("summary counts missing or wrong")
    if cols >= 64 and not re.search(r"120 sent\s+96 received\s+24 lost", text):
        errors.append("summary counts missing or wrong")
    if b"\x1b[?25h" not in raw[-300:]:
        errors.append("cursor not shown again at the end")
    for snap in snaps:
        tops = [y for y, line in enumerate(snap) if line.lstrip().startswith("╭─")]
        if len(tops) > 1:
            errors.append(f"{len(tops)} panels on screen at once")
    return errors, final


def test_resize_while_silent(cfg, pyte):
    """A resize is handled even while ping prints nothing."""
    path, cmd = screen_cmd(cfg, [14, 15, 16, "wait"])
    try:
        screen, snaps, raw = run_pty(pyte, 100, 30, cmd, resize=(2, 70, 30), snap_at=(4,),
                                     interrupt_at=5, duration=15)
    finally:
        os.unlink(path)
    errors = []
    if screen.last_column:
        errors.append(f"wrote in the last column: {screen.last_column[:3]}")
    top = [l for l in snaps[0] if l.lstrip().startswith("╭─")] if snaps else []
    if len(top) != 1:
        errors.append(f"{len(top)} panel tops after the resize")
    elif len(top[0].rstrip()) > 69:
        errors.append(f"panel is {len(top[0].rstrip())} wide in 70 columns")
    return errors, "\n".join(screen.display)


def test_dumb_terminal(cfg, pyte):
    """TERM=dumb gets the plain output: no cursor movement."""
    path, cmd = screen_cmd(cfg, [14, 15, 16])
    try:
        screen, snaps, raw = run_pty(pyte, 100, 30, cmd, env={"TERM": "dumb"})
    finally:
        os.unlink(path)
    errors = []
    if re.search(rb"\x1b\[[0-9]*[ABJ]", raw):
        errors.append("cursor movement sent to a dumb terminal")
    return errors, "\n".join(screen.display)


def test_late_repaint(cfg, pyte):
    """A late reply turns its cross back into a bar."""
    path, cmd = screen_cmd(cfg, [14, 15, "lost", 16, "late 1 20", 15, "wait"])
    try:
        screen, snaps, raw = run_pty(pyte, 100, 30, cmd, snap_at=(2.5,), interrupt_at=3, duration=10)
    finally:
        os.unlink(path)
    band = next((l for l in snaps[0] if "#0" in l), "") if snaps else ""
    errors = [] if band and "✕" not in band else [f"cross still drawn: {band.strip()}"]
    return errors, "\n".join(screen.display)


############################################################

def find_awks():
    found = []
    seen = set()
    for name in ("awk", "/usr/bin/awk", "gawk", "mawk", "nawk", "original-awk"):
        path = shutil.which(name)
        if not path:
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        found.append(path)
    return found


class Config:
    def __init__(self, awk, bash):
        self.awk = awk
        self.bash = bash


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--awk", action="append")
    ap.add_argument("--bash", default=shutil.which("bash") or "/bin/bash")
    ap.add_argument("-k", default="")
    ap.add_argument("-v", action="store_true", help="print the output of failed tests")
    a = ap.parse_args()

    try:
        import pyte
    except ImportError:
        pyte = None

    awks = a.awk or find_awks()
    failed = passed = skipped = 0
    for awk in awks:
        cfg = Config(awk, a.bash)
        tests = []
        for name, style, events, ping_args, want in ACCOUNTING:
            tests.append((name, lambda s=style, e=events, p=ping_args, w=want:
                          test_accounting(cfg, s, e, p, w)))
        tests += [
            ("ping statistics are not messages", lambda: test_messages_hidden(cfg)),
            ("recent window values", lambda: test_recent_window(cfg)),
            ("row stats cover their row", lambda: test_row_stats_cover_the_row(cfg)),
            ("exact percentiles", lambda: test_exact_percentiles(cfg)),
            ("jitter rows agree", lambda: test_jitter_rows_agree(cfg)),
            ("bucket under 0.01 ms", lambda: test_tiny_bucket(cfg)),
            ("MOS word and meter", lambda: test_mos_word_matches(cfg)),
            ("summary keeps dup and late", lambda: test_card_keeps_counts(cfg)),
            ("legend in piped output", lambda: test_legend(cfg)),
            ("interrupt with a packet in flight", lambda: test_interrupt_in_flight(cfg)),
            ("exit status", lambda: test_exit_status(cfg)),
            ("bad options", lambda: test_bad_options(cfg)),
            ("decimal --rttmin", lambda: test_decimal_rtt_scale(cfg)),
        ]
        for cols, rows, extra in ((100, 30, ()), (80, 24, ()), (64, 14, ()), (60, 20, ()),
                                  (40, 12, ()), (100, 5, ()), (100, 30, ("--compact",)),
                                  (100, 30, ("--nounicode",)), (100, 30, ("--colors", "16")),
                                  (100, 30, ("--theme", "light"))):
            name = f"screen {cols}x{rows} {' '.join(extra)}".strip()
            tests.append((name, lambda c=cols, r=rows, x=extra: test_screen(cfg, pyte, c, r, *x)
                          if pyte else None))
        tests += [
            ("screen resize while silent", lambda: test_resize_while_silent(cfg, pyte) if pyte else None),
            ("screen TERM=dumb", lambda: test_dumb_terminal(cfg, pyte) if pyte else None),
            ("screen late repaint", lambda: test_late_repaint(cfg, pyte) if pyte else None),
        ]
        for name, fn in tests:
            if a.k and a.k not in name:
                continue
            label = f"[{os.path.basename(awk)}] {name}"
            result = fn()
            if result is None:
                skipped += 1
                print(f"SKIP {label} (pyte is not installed)")
                continue
            errors, out = result
            if errors:
                failed += 1
                print(f"FAIL {label}: {'; '.join(errors)}")
                if a.v:
                    print(out)
            else:
                passed += 1
                print(f"ok   {label}")
            sys.stdout.flush()
    print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
