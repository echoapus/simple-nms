"""Shared runner for the phase validation scripts."""

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  -- {detail}")


def run_suite(title, tests):
    print("=" * 60)
    print(title)
    print("=" * 60)
    for test in tests:
        test()
    print("\n" + "=" * 60)
    print(f"Results:  {passed} passed,  {failed} failed")
    print("=" * 60)
    return failed
