import pytest
from solution import divide


def test_basic_division():
    assert divide(10, 2) == 5


def test_float_result():
    assert divide(7, 2) == 3.5


def test_zero_division_raises():
    with pytest.raises(ZeroDivisionError):
        divide(5, 0)
