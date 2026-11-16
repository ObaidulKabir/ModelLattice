from solution import fibonacci


def test_base_cases():
    assert fibonacci(0) == 0
    assert fibonacci(1) == 1


def test_known_terms():
    assert fibonacci(2) == 1
    assert fibonacci(5) == 5
    assert fibonacci(10) == 55
