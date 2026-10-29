"""Analyzer terms and offsets remain stable across scripts and case folding."""

import pytest

from powercontext.builtin.artifacts.search import analyze_text_with_spans


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", ()),
        (
            "REVENUE FY2022 1,234.50\nnet_income",
            (
                ("revenue", 0, 7),
                ("fy2022", 8, 14),
                ("1", 15, 16),
                ("234", 17, 20),
                ("50", 21, 23),
                ("net_income", 24, 34),
            ),
        ),
        ("Café—\uff11\uff12\uff13 £A_B", (("café", 0, 4), ("\uff11\uff12\uff13", 5, 8), ("a_b", 10, 13))),
        ("\u212a STRAẞE", (("k", 0, 1), ("strasse", 2, 9))),
        (
            "München 中文 A_1",
            (("münchen", 0, 7), ("u_4e2d", 8, 9), ("u_6587", 9, 10), ("b_4e2d_6587", 8, 10), ("a_1", 11, 14)),
        ),
        ("𠀀中", (("u_20000", 0, 1), ("u_4e2d", 1, 2), ("b_20000_4e2d", 0, 2))),
    ],
)
def test_terms_and_normalized_character_offsets(text, expected):
    assert analyze_text_with_spans(text) == expected
