def fibonacci(n):
    # Bug: off-by-one in the loop range, so fibonacci(10) returns the 9th
    # term instead of the 10th (0-indexed).
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a - b + a  # also wrong: nonsensical extra arithmetic
