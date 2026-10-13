import importlib.util
import sys


def load_median():
    spec = importlib.util.spec_from_file_location("answer", "/app/answer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.median


import json


def main():
    median = load_median()
    cases = json.load(open("/tests/cases.json"))
    if len(cases) < 5:
        raise SystemExit("grading cases are missing")
    for case in cases:
        got = median(list(case["input"]))
        if got != case["expected"]:
            print(f"median({case['input']}) = {got!r}, expected {case['expected']!r}")
            raise SystemExit(1)


main()
