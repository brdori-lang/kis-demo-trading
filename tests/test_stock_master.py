import sqlite3

import pytest

from lab_repository import SQLiteLabRepository
from stock_master import normalize_stock_code, parse_kosdaq_master, parse_kospi_master


def master_row(code: str, standard_code: str, name: str, tail_length: int):
    part1 = f"{code:<9}{standard_code:<12}{name}"
    return part1 + ("X" * (tail_length - 1)) + "\n"


def test_kospi_parser_uses_official_228_character_tail_rule():
    row = master_row("005930", "KR7005930003", "삼성전자", 228)

    assert parse_kospi_master([row]) == [
        {"stock_code": "005930", "stock_name": "삼성전자", "market": "KOSPI"}
    ]


def test_kosdaq_parser_uses_official_222_character_tail_rule():
    row = master_row("035720", "KR7035720002", "카카오", 222)

    assert parse_kosdaq_master([row]) == [
        {"stock_code": "035720", "stock_name": "카카오", "market": "KOSDAQ"}
    ]


@pytest.mark.parametrize("value", ["5930", "A05930", "0059300", "00593A"])
def test_stock_code_must_be_six_digits(value):
    with pytest.raises(ValueError):
        normalize_stock_code(value)


def test_parser_skips_non_six_digit_short_codes():
    invalid = master_row("A05930", "KR7005930003", "잘못된종목", 228)
    valid = master_row("005930", "KR7005930003", "삼성전자", 228)

    assert parse_kospi_master([invalid, valid]) == [
        {"stock_code": "005930", "stock_name": "삼성전자", "market": "KOSPI"}
    ]


def test_sql_search_order_is_exact_code_prefix_then_name_relevance(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "search.db")
    repository.replace_stock_master(
        [
            {"stock_code": "005930", "stock_name": "삼성전자", "market": "KOSPI"},
            {"stock_code": "005931", "stock_name": "테스트우", "market": "KOSPI"},
            {"stock_code": "111111", "stock_name": "00593", "market": "KOSPI"},
            {"stock_code": "222222", "stock_name": "00593테스트", "market": "KOSPI"},
            {"stock_code": "333333", "stock_name": "테스트00593", "market": "KOSPI"},
        ]
    )

    results = repository.search_stocks("00593")

    assert [item["stock_code"] for item in results] == [
        "005930",
        "005931",
        "111111",
        "222222",
        "333333",
    ]
    assert repository.search_stocks("005930")[0]["stock_code"] == "005930"


def test_master_replace_rolls_back_on_insert_failure(tmp_path):
    repository = SQLiteLabRepository(tmp_path / "atomic.db")
    original = [{"stock_code": "005930", "stock_name": "삼성전자", "market": "KOSPI"}]
    repository.replace_stock_master(original)

    duplicate_rows = [
        {"stock_code": "000660", "stock_name": "SK하이닉스", "market": "KOSPI"},
        {"stock_code": "000660", "stock_name": "중복", "market": "KOSDAQ"},
    ]
    with pytest.raises(sqlite3.IntegrityError):
        repository.replace_stock_master(duplicate_rows)

    assert repository.count_stocks() == 1
    assert repository.get_stock("005930")["stock_name"] == "삼성전자"
