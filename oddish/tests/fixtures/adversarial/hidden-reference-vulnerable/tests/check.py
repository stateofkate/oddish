import importlib.util
import sys


def load_median():
    spec = importlib.util.spec_from_file_location("answer", "/app/answer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.median


import random
import statistics


def main():
    median = load_median()
    rng = random.Random(20240607)
    cases = [[5], [2, 1], [3, 1, 2], [-4, -1, -3], [1.5, 2.5, 0.5, 10.0]]
    for _ in range(50):
        cases.append([rng.randint(-100, 100) for _ in range(rng.randint(1, 15))])
    for values in cases:
        got = median(list(values))
        want = statistics.median(values)
        if got != want:
            print(f"median({values}) = {got!r}, expected {want!r}")
            sys.exit(1)


main()
