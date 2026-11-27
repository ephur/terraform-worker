import pytest

from tfworker.util.duration import format_duration, parse_duration


class TestParseDuration:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (30, 30.0),
            (1.5, 1.5),
            ("45", 45.0),
            (" 90s ", 90.0),
            ("15m", 900.0),
            ("1h30m", 5400.0),
            ("2d", 172800.0),
            ("1.5h", 5400.0),
            ("1H", 3600.0),
        ],
    )
    def test_valid(self, value, expected):
        assert parse_duration(value) == expected

    @pytest.mark.parametrize("value", ["", "abc", "10x", "1h foo", True, -1, "-5"])
    def test_invalid(self, value):
        with pytest.raises(ValueError):
            parse_duration(value)


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [(0, "0s"), (42.4, "42s"), (252, "4m12s"), (3600, "1h00m"), (5430, "1h30m")],
    )
    def test_format(self, seconds, expected):
        assert format_duration(seconds) == expected
