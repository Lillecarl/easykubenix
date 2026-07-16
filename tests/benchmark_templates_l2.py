from __future__ import annotations

import argparse
from pathlib import Path
from time import perf_counter

import nanopynix
import nanopynix_expr

NIX_BENCHMARK = Path(__file__).with_name("benchmark_templates.nix")


def measure(function: nanopynix.Value, state: nanopynix.EvalState, count: int, warmup: int) -> float:
    for index in range(warmup):
        function.call(state.value_from_python(index)).force()

    started = perf_counter()
    for index in range(count):
        function.call(state.value_from_python(index)).force()
    return perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark typed Nix templates through nanopynix L2.")
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument("--warmup", type=int, default=100)
    args = parser.parse_args()

    if args.count <= 0:
        raise ValueError("--count must be positive")
    if args.warmup < 0:
        raise ValueError("--warmup cannot be negative")

    nanopynix.init_libstore(load_config=False)
    nanopynix.enable_experimental_feature("flakes")
    nanopynix.enable_experimental_feature("nix-command")
    nanopynix.init_libexpr()
    state = nanopynix.EvalState(nanopynix.open_store(), nanopynix_expr.parse_nix_path())

    for name in ("evalModules", "adiosCalls", "adiosBuilds"):
        preload_started = perf_counter()
        function = state.eval_file(str(NIX_BENCHMARK)).attr_get("l2").attr_get(name)
        for index in range(args.warmup):
            function.call(state.value_from_python(index)).force()
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
