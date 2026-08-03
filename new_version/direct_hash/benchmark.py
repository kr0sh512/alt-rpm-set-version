#!/usr/bin/env python3
import argparse
import ctypes
import gc
import os
import statistics
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
LIBC = ctypes.CDLL(None)
LIBC.free.argtypes = [ctypes.c_void_p]


class SetAPI:
    def __init__(self, path: Path):
        self.lib = ctypes.CDLL(str(path))
        self.lib.set_new.restype = ctypes.c_void_p
        self.lib.set_add.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self.lib.set_fini.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.set_fini.restype = ctypes.c_void_p
        self.lib.set_free.argtypes = [ctypes.c_void_p]
        self.lib.set_free.restype = ctypes.c_void_p
        self.lib.rpmsetcmp.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self.lib.rpmsetcmp.restype = ctypes.c_int

    def new_with_symbols(self, symbols):
        value = self.lib.set_new()
        if not value:
            raise RuntimeError("set_new returned NULL")
        for symbol in symbols:
            self.lib.set_add(value, symbol)
        return value

    def release(self, value, result):
        if result:
            LIBC.free(result)
        self.lib.set_free(value)

    def encode(self, symbols, bpp):
        value = self.new_with_symbols(symbols)
        result = self.lib.set_fini(value, bpp)
        if not result:
            raise RuntimeError("set_fini returned NULL")
        encoded = ctypes.string_at(result)
        self.release(value, result)
        return encoded


def median_fini(api, symbols, bpp, calls, rounds):
    samples = []
    for _ in range(rounds):
        sets = [api.new_with_symbols(symbols) for _ in range(calls)]
        results = []
        start = time.perf_counter_ns()
        for value in sets:
            results.append(api.lib.set_fini(value, bpp))
        samples.append((time.perf_counter_ns() - start) / calls)
        if not all(results):
            raise RuntimeError("set_fini returned NULL")
        encoded = [ctypes.string_at(result) for result in results]
        if len(set(encoded)) != 1:
            raise RuntimeError("set_fini is not deterministic")
        for value, result in zip(sets, results):
            api.release(value, result)
    return statistics.median(samples)


def median_build(api, symbols, bpp, calls, rounds):
    samples = []
    for _ in range(rounds):
        sets = []
        results = []
        start = time.perf_counter_ns()
        for _ in range(calls):
            value = api.new_with_symbols(symbols)
            result = api.lib.set_fini(value, bpp)
            sets.append(value)
            results.append(result)
        samples.append((time.perf_counter_ns() - start) / calls)
        if not all(results):
            raise RuntimeError("set_fini returned NULL")
        for value, result in zip(sets, results):
            api.release(value, result)
    return statistics.median(samples)


def median_cmp(api, provider, requirement, calls, rounds):
    expected = api.lib.rpmsetcmp(provider, requirement)
    if expected != 1 or api.lib.rpmsetcmp(provider, provider) != 0:
        raise RuntimeError(f"unexpected rpmsetcmp result: {expected}")
    for _ in range(100):
        api.lib.rpmsetcmp(provider, requirement)

    samples = []
    for _ in range(rounds):
        checksum = 0
        start = time.perf_counter_ns()
        for _ in range(calls):
            checksum += api.lib.rpmsetcmp(provider, requirement)
        samples.append((time.perf_counter_ns() - start) / calls)
        if checksum != calls:
            raise RuntimeError("rpmsetcmp result changed during benchmark")
    return statistics.median(samples)


def cold_cmp_once(api, provider, requirement):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        start = time.perf_counter_ns()
        result = api.lib.rpmsetcmp(provider, requirement)
        elapsed = time.perf_counter_ns() - start
        os.write(write_fd, f"{elapsed} {result}".encode())
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    payload = b""
    while chunk := os.read(read_fd, 128):
        payload += chunk
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    if status != 0:
        raise RuntimeError(f"cold rpmsetcmp child failed: status={status}")
    elapsed, result = map(int, payload.split())
    if result != 1:
        raise RuntimeError(f"unexpected cold rpmsetcmp result: {result}")
    return elapsed


def median_cmp_cold(api, provider, requirement, calls, rounds):
    samples = []
    for _ in range(rounds):
        total = sum(cold_cmp_once(api, provider, requirement) for _ in range(calls))
        samples.append(total / calls)
    return statistics.median(samples)


def format_time(ns):
    return f"{ns / 1000:.2f} us"


def main():
    parser = argparse.ArgumentParser(description="Compare set9 and direct-hash set APIs")
    parser.add_argument("--symbols", type=int, default=1000)
    parser.add_argument("--bpp", type=int, default=32)
    parser.add_argument("--rounds", type=int, default=7)
    parser.add_argument("--fini-calls", type=int, default=5)
    parser.add_argument("--cmp-calls", type=int, default=2000)
    parser.add_argument("--cold-calls", type=int, default=20)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()
    if args.symbols < 2 or not 10 <= args.bpp <= 32:
        parser.error("symbols must be >= 2 and bpp must be in 10..32")
    if min(args.rounds, args.fini_calls, args.cmp_calls, args.cold_calls) < 1:
        parser.error("rounds and call counts must be positive")

    if not args.skip_build:
        subprocess.run([str(HERE / "build.sh")], check=True)

    symbols = tuple(
        f"symbol_{i:08d}_version_ALT_{i % 97}".encode() for i in range(args.symbols)
    )
    required = symbols[::2]
    apis = {
        "set9": SetAPI(BUILD / "libset9.so"),
        "direct": SetAPI(BUILD / "libdirect-hash.so"),
    }

    gc.disable()
    try:
        timings = {}
        lengths = {}
        for name, api in apis.items():
            provider = api.encode(symbols, args.bpp)
            requirement = api.encode(required, args.bpp)
            wire_format = "D1/base64" if provider.startswith(b"D1") else "golomb/base62"
            lengths[name] = (len(provider), wire_format)
            timings[name] = (
                median_fini(api, symbols, args.bpp, args.fini_calls, args.rounds),
                median_build(api, symbols, args.bpp, args.fini_calls, args.rounds),
                median_cmp_cold(api, provider, requirement, args.cold_calls, args.rounds),
                median_cmp(api, provider, requirement, args.cmp_calls, args.rounds),
            )
    finally:
        gc.enable()

    print(f"symbols={args.symbols} required={len(required)} bpp={args.bpp}")
    print("implementation  set_chars  format")
    for name in apis:
        print(f"{name:<14} {lengths[name][0]:>9}  {lengths[name][1]}")
    print("\noperation                 set9       direct   direct/set9")
    labels = ("set_fini only", "new+add+fini", "rpmsetcmp cold", "rpmsetcmp warm")
    for index, label in enumerate(labels):
        old = timings["set9"][index]
        new = timings["direct"][index]
        print(f"{label:<22} {format_time(old):>10} {format_time(new):>10} {new / old:>12.2f}x")


if __name__ == "__main__":
    main()
