import importlib.util
import sys


def load_median():
    spec = importlib.util.spec_from_file_location("answer", "/app/answer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.median


def main():
    median = load_median()
    if median([1, 2, 3]) != 2:
        print("median([1, 2, 3]) should be 2")
        raise SystemExit(1)


main()
