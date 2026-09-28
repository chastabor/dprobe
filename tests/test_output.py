import io
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from dprobe.output import json_keys, json_value, text_value, write_result


@dataclass
class FakeResult:
    columns: list[str]
    data: list[tuple]
    fetched: int = 0

    def rows(self):
        for row in self.data:
            self.fetched += 1
            yield row


ROWS = [
    (1, "Ann", Decimal("12.50"), None),
    (2, "Bo\tb", Decimal("1E+2"), date(2024, 1, 2)),
]


def render(fmt, rows=ROWS, **kwargs):
    out = io.StringIO()
    count, more = write_result(FakeResult(["id", "name", "amount", "since"], rows), fmt, out, **kwargs)
    return out.getvalue(), count, more


def test_table():
    text, count, more = render("table", null="NULL")
    assert text == (
        "id  name   amount  since\n"
        "--  -----  ------  ----------\n"
        "1   Ann    12.50   NULL\n"
        "2   Bo\\tb  100     2024-01-02\n"
    )
    assert (count, more) == (2, False)


def test_table_truncates_wide_cells():
    text, _, _ = render("table", rows=[(1, "x" * 20, None, None)], max_width=8)
    assert "xxxxxxx…" in text


def test_csv_and_tsv():
    text, _, _ = render("csv")
    assert text == "id,name,amount,since\n1,Ann,12.50,\n2,Bo\tb,100,2024-01-02\n"
    text, _, _ = render("tsv")
    assert text.splitlines()[2] == '2\t"Bo\tb"\t100\t2024-01-02'


def test_json_keeps_decimal_digits():
    text, _, _ = render("json")
    assert json.loads(text, parse_float=Decimal) == [
        {"id": 1, "name": "Ann", "amount": Decimal("12.50"), "since": None},
        {"id": 2, "name": "Bo\tb", "amount": 100, "since": "2024-01-02"},
    ]
    assert '"amount": 12.50' in text


def test_json_empty_and_jsonl():
    assert render("json", rows=[])[0] == "[]\n"
    lines = render("jsonl")[0].splitlines()
    assert [json.loads(line)["id"] for line in lines] == [1, 2]


def test_max_rows_reports_more():
    result = FakeResult(["n"], [(i,) for i in range(10)])
    out = io.StringIO()
    assert write_result(result, "csv", out, max_rows=3) == (3, True)
    # Only one row beyond the limit is fetched.
    assert result.fetched == 4
    assert write_result(FakeResult(["n"], [(1,), (2,)]), "csv", io.StringIO(), max_rows=2) == (2, False)


@pytest.mark.parametrize(
    ("value", "text", "encoded"),
    [
        (None, "", "null"),
        (True, "True", "true"),
        (b"\x00\xff", "0x00ff", '"0x00ff"'),
        (bytearray(b"\x01"), "0x01", '"0x01"'),
        (Decimal("-0.001"), "-0.001", "-0.001"),
        (Decimal("NaN"), "NaN", '"NaN"'),
        (float("inf"), "inf", '"inf"'),
        (1.5, "1.5", "1.5"),
        (datetime(2024, 1, 2, 3, 4, 5), "2024-01-02 03:04:05", '"2024-01-02T03:04:05"'),
        (timedelta(hours=2), "2:00:00", '"2:00:00"'),
        (timedelta(days=1, hours=1, microseconds=5), "25:00:00.000005", '"25:00:00.000005"'),
        (timedelta(seconds=-1), "-0:00:01", '"-0:00:01"'),
        ({"b", "a"}, "a,b", '["a", "b"]'),
        ({"k": [1, 2]}, '{"k": [1, 2]}', '{"k": [1, 2]}'),
        (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001", '"00000000-0000-0000-0000-000000000001"'),
    ],
)
def test_value_conversions(value, text, encoded):
    assert text_value(value) == text
    assert json_value(value) == encoded


def test_json_keeps_non_ascii():
    assert json_value("naïve ✓") == '"naïve ✓"'


def test_json_keys_unique():
    assert json_keys(["ID", "ID", "", "ID_2", ""]) == ["ID", "ID_2", "column3", "ID_2_2", "column5"]
