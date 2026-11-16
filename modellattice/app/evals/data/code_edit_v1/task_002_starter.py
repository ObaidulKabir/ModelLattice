def is_palindrome(s):
    # Bug: doesn't normalize case or strip non-alphanumeric characters,
    # so "Race car" and "A man a plan a canal Panama" fail incorrectly.
    return s == s[::-1]
