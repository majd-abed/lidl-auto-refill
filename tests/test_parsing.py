import pytest

from lidl_client import AllowanceParseError, parse_data_allowance


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0,18 GB", 0.18),
        ("0.2 GB", 0.2),
        ("200 MB", 200 / 1024),
        ("199 MB", 199 / 1024),
        ("1 GB", 1),
        ("1,25 GB", 1.25),
        ("0.18 GB", 0.18),
        ("850 MB", 850 / 1024),
        ("1,4 GB", 1.4),
        ("1.25 GB", 1.25),
        ("  0,18\u00a0GB\n", 0.18),
        ("200\u202fmb", 200 / 1024),
        ("0 GB", 0),
    ],
)
def test_parse_allowance(text, expected):
    assert parse_data_allowance(text) == pytest.approx(expected)


@pytest.mark.parametrize(
    "text",
    ["unexpected text", "", "unlimited", "GB", "1", "-1 GB", "NaN GB", "inf GB",
     "1.2.3 GB", "1,2.3 GB", "remaining 0.2 GB", "0.2 GB / 10 GB", "1 TB", None,
     "9" * 400 + " GB"],
)
def test_invalid_allowance_fails_without_echoing_input(text):
    with pytest.raises(AllowanceParseError) as error:
        parse_data_allowance(text)
    if isinstance(text, str) and len(text) > 2:
        assert text not in str(error.value)
