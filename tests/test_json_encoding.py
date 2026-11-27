"""The JSON a data answer's rows are written as (outputs.encode_json, which
FastJSON uses): pinned value by value, and the same bytes whether the rows
went through ``jsonable`` first or stay as DuckDB fetched them (``raw``)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import duckdb
import pytest

from exuvia.outputs import encode_json, jsonable, query_json


def before(content):
    """What a data answer was before encode_json: jsonable, then json.dumps."""
    return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=str).encode()


# (DuckDB expression, the JSON it is written as)
PINNED = [
    ("null", "null"),
    ("true", "true"),
    ("42::bigint", "42"),
    ("(-9223372036854775808)::bigint", "-9223372036854775808"),
    ("18446744073709551615::ubigint", "18446744073709551615"),
    ("170141183460469231731687303715884105727::hugeint", "170141183460469231731687303715884105727"),
    ("340282366920938463463374607431768211455::uhugeint", "340282366920938463463374607431768211455"),
    ("1.5::double", "1.5"),
    ("1.5::float", "1.5"),
    ("-0.0::double", "-0.0"),
    ("1e16::double", "1e+16"),
    ("1e300::double", "1e+300"),
    ("'nan'::double", "null"),
    ("'inf'::double", "null"),
    ("'-inf'::float", "null"),
    ("123.450::decimal(10,3)", "123.45"),
    ("12345678901234567890123456789.123::decimal(38,3)", "1.2345678901234568e+28"),
    ("'héllo ☃ \"q\" \\n'", '"héllo ☃ \\"q\\" \\\\n"'),
    ("chr(1) || chr(10) || chr(127)", '"\\u0001\\n\x7f"'),
    ("'a'::enum('a','b')", '"a"'),
    ("date '2024-01-02'", '"2024-01-02"'),
    ("'-infinity'::date", '"0001-01-01"'),
    ("timestamp '2024-01-02 03:04:05'", '"2024-01-02T03:04:05"'),
    ("timestamp '2024-01-02 03:04:05.123456'", '"2024-01-02T03:04:05.123456"'),
    ("timestamp_ms '2024-01-02 03:04:05.120'", '"2024-01-02T03:04:05.120000"'),
    ("timestamp_ns '2024-01-02 03:04:05.123456789'", '"2024-01-02T03:04:05.123456"'),
    ("timestamp_s '2024-01-02 03:04:05'", '"2024-01-02T03:04:05"'),
    ("'infinity'::timestamp", '"9999-12-31T23:59:59.999999"'),
    ("timestamptz '2024-07-02 03:04:05.5+00'", '"2024-07-01T23:04:05.500000-04:00"'),
    ("timestamptz '2024-01-02 03:04:05+00'", '"2024-01-01T22:04:05-05:00"'),
    # local mean time: an offset with seconds, which orjson would cut to -04:56
    ("timestamptz '1850-01-01 00:00:00+00'", '"1849-12-31T19:03:58-04:56:02"'),
    ("time '03:04:05.25'", '"03:04:05.250000"'),
    ("'03:04:05+02'::timetz", '"03:04:05+02:00"'),
    ("interval '1 day 2 hours 3.5 seconds'", '"1 day, 2:00:03.500000"'),
    ("interval '14 months'", '"420 days, 0:00:00"'),
    ("'\\xAA\\x00ab'::blob", "\"b'\\\\xaa\\\\x00ab'\""),
    ("'550e8400-e29b-41d4-a716-446655440000'::uuid", '"550e8400-e29b-41d4-a716-446655440000"'),
    ("'0101'::bit", '"0101"'),
    ("'123456789012345678901234567890'::varint", '"123456789012345678901234567890"'),
    ("'{\"a\":1}'::json", '"{\\"a\\":1}"'),
    ("union_value(num := 2)", "2"),
    ("[1, 2, null]", "[1,2,null]"),
    ("[[1.5, 'nan'::double]]", "[[1.5,null]]"),
    ("[1, 2, 3]::int[3]", "[1,2,3]"),
    ("{'a': 1, 'b': [timestamp '2024-01-01'], 'c': 'x'}", '{"a":1,"b":["2024-01-01T00:00:00"],"c":"x"}'),
    ("[timestamptz '2024-01-01 00:00:00+05']", '["2023-12-31T14:00:00-05:00"]'),
    ("{'t': timestamptz '1850-01-01 00:00:00+00'}", '{"t":"1849-12-31T19:03:58-04:56:02"}'),
    ("map(['k'], [date '2024-01-01'])", '{"k":"2024-01-01"}'),
    ("map([1, 2], ['a', 'b'])", '{"1":"a","2":"b"}'),
    ("map([timestamp '2024-01-01 01:02:03'], [1])", '{"2024-01-01 01:02:03":1}'),
    ("map([1.5], [1])", '{"1.5":1}'),
]


@pytest.fixture(scope="module")
def con():
    c = duckdb.connect()
    c.execute("set timezone = 'America/New_York'")
    yield c
    c.close()


@pytest.mark.parametrize(("expr", "want"), PINNED, ids=[e for e, _ in PINNED])
def test_a_value_is_written_as_before(con, expr, want):
    raw = query_json(con, f"select {expr} as v", 10, raw=True)
    ready = query_json(con, f"select {expr} as v", 10)
    assert before(ready["rows"]) == f"[[{want}]]".encode()
    assert encode_json(raw) == before(ready)


def test_an_answer_of_every_type_at_once(con):
    """One answer with every pinned type as a column: one value that
    orjson cannot write (a MAP's int key, a HUGEINT) takes the whole answer
    down the slow path, and that writes the rest the same."""
    sql = "select " + ", ".join(f"{e} as c{i}" for i, (e, _) in enumerate(PINNED))
    raw = query_json(con, sql, 10, raw=True)
    want = "[[" + ",".join(w for _, w in PINNED) + "]]"
    assert encode_json(raw) == before(query_json(con, sql, 10))
    assert json.loads(encode_json(raw))["rows"] == json.loads(want)


def test_python_values_as_before():
    """Values a DuckDB fetch can hold, given directly, as dict keys too."""
    tz = timezone(timedelta(hours=5, minutes=30))
    rows = [[
        Decimal("1.10"), datetime(2024, 1, 2, 3, 4, 5, 6, tzinfo=UTC), datetime(2024, 1, 2, 3, 4, 5, tzinfo=tz),
        date(1, 1, 1), time(1, 2, 3), time(1, 2, 3, tzinfo=tz), timedelta(seconds=-1), b"\x00\xff",
        UUID(int=1), (1, "a"), {"n": float("nan")}, {1: "a"}, {None: 1}, {True: 1}, 2**64, -(2**63) - 1,
        float("inf"), " \U0001f600",
    ]]
    assert encode_json(rows) == before(jsonable(rows))
    assert encode_json({"columns": [{"name": "t", "type": "TIMESTAMP WITH TIME ZONE"}], "rows": rows}) == before(
        {"columns": [{"name": "t", "type": "TIMESTAMP WITH TIME ZONE"}], "rows": jsonable(rows)}
    )


@pytest.mark.parametrize("v", [1e-9, 1.5e-9, 3.14159e-7, 1e-5, 9.999999e-5])
def test_a_tiny_float_is_the_same_number_spelled_differently(v):
    """The one difference: orjson writes a float between 1e-9 and 1e-4
    without the exponent's leading zero (1e-7, 1e-07 before) or without an
    exponent (0.00001, 1e-05 before). Any JSON reader reads the same float."""
    new, old = encode_json([v]), before([v])
    assert json.loads(new) == json.loads(old) == [v]


@pytest.mark.parametrize("v", [1e-4, 1.5e-3, 0.1, 123.456, 1e16, 1.7976931348623157e308, 5e-324, 2.2250738585072014e-308])
def test_other_floats_are_the_same_bytes(v):
    assert encode_json([v, -v]) == before([v, -v])


def test_a_wide_random_answer_is_the_same_bytes(con):
    sql = """select i::bigint id, sin(i) * 1000 x, 'name_' || (i % 997) s,
        timestamp '2024-01-01' + to_seconds(i * 37) ts, date '2020-01-01' + (i % 3000)::int d,
        (i / 7)::decimal(18,3) amount, [i, null] l, {'k': i::varchar} st
        from range(5000) r(i)"""
    raw = query_json(con, sql, 5000, raw=True)
    assert len(raw["rows"]) == 5000
    assert encode_json(raw) == before(query_json(con, sql, 5000))
