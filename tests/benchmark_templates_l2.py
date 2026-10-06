from __future__ import annotations

import argparse
from pathlib import Path
from time import perf_counter

from huggorm import EvalState, Store, Value, enable_experimental_feature, load_config

NIX_BENCHMARK = Path(__file__).with_name("benchmark_templates.nix")


def invoke(function: Value, state: EvalState, index: int) -> None:
    state.force(function(state.make_int(index)))


def measure(function: Value, state: EvalState, count: int, warmup: int) -> float:
    for index in range(warmup):
        invoke(function, state, index)

    started = perf_counter()
    for index in range(count):
        invoke(function, state, index)
    return perf_counter() - started


def select(state: EvalState, value: Value, *names: str) -> Value:
    for name in names:
        state.force(value)
        value = value.get(name)
    state.force(value)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark typed Nix templates through huggorm's sync API.")
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=100)
    args = parser.parse_args()

    if args.count <= 0:
        raise ValueError("--count must be positive")
    if args.warmup < 0:
        raise ValueError("--warmup cannot be negative")

    load_config()
    enable_experimental_feature("flakes")
    enable_experimental_feature("nix-command")
    state = EvalState(Store("auto"))

    for name in ("evalModules", "adiosCalls", "adiosBuilds"):
        preload_started = perf_counter()
        function = select(state, state.eval_file(str(NIX_BENCHMARK)), "l2", name)
        for index in range(args.warmup):
            invoke(function, state, index)
        preload_seconds = perf_counter() - preload_started

        seconds = measure(function, state, args.count, warmup=0)
        rate = args.count / seconds
        microseconds = seconds * 1_000_000 / args.count
        print(
            f"{name}: preloaded in {preload_seconds:.3f} s; "
            f"{args.count} invocations in {seconds:.3f} s "
            f"({rate:,.0f}/s, {microseconds:.1f} us/invocation)"
        )


if __name__ == "__main__":
    main()
