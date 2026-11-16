from solution import is_palindrome


def test_simple_word():
    assert is_palindrome("level") is True


def test_mixed_case_with_space():
    assert is_palindrome("Race car") is True


def test_punctuation_and_case():
    assert is_palindrome("A man a plan a canal Panama") is True


def test_non_palindrome():
    assert is_palindrome("hello") is False
