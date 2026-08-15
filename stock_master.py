import io
import re
import tempfile
import urllib.request
import zipfile
from pathlib import Path


KOSPI_MASTER_URL = "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip"
KOSDAQ_MASTER_URL = "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip"
STOCK_CODE_PATTERN = re.compile(r"^\d{6}$")


def normalize_stock_code(value: str):
    stock_code = value.strip()
    if not STOCK_CODE_PATTERN.fullmatch(stock_code):
        raise ValueError("종목코드는 숫자 6자리여야 합니다.")
    return stock_code


def _parse_master_lines(lines, market: str, tail_length: int):
    stocks = []
    for row in lines:
        if not row.strip():
            continue
        if len(row) <= tail_length + 21:
            raise ValueError(f"{market} 마스터 레코드 길이가 올바르지 않습니다.")

        # KIS 공식 파서와 동일하게 레코드 끝의 고정폭 영역을 먼저 분리한다.
        part1 = row[0 : len(row) - tail_length]
        short_code = part1[0:9].rstrip()
        standard_code = part1[9:21].rstrip()
        stock_name = part1[21:].strip()

        try:
            stock_code = normalize_stock_code(short_code)
        except ValueError:
            continue
        if not standard_code or not stock_name:
            raise ValueError(f"{market} 마스터 필수 필드가 비어 있습니다.")

        stocks.append(
            {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "market": market,
            }
        )
    if not stocks:
        raise ValueError(f"{market} 마스터에서 유효한 종목을 찾지 못했습니다.")
    return stocks


def parse_kospi_master(lines):
    return _parse_master_lines(lines, market="KOSPI", tail_length=228)


def parse_kosdaq_master(lines):
    return _parse_master_lines(lines, market="KOSDAQ", tail_length=222)


def _download_and_parse(url: str, archive_name: str, member_name: str, parser):
    with tempfile.TemporaryDirectory(prefix="kis-stock-master-") as temp_dir:
        archive_path = Path(temp_dir) / archive_name
        urllib.request.urlretrieve(url, archive_path)
        with zipfile.ZipFile(archive_path) as archive:
            with archive.open(member_name) as raw_file:
                with io.TextIOWrapper(raw_file, encoding="cp949", newline=None) as text_file:
                    return parser(text_file)


def download_all_stock_masters():
    kospi = _download_and_parse(
        KOSPI_MASTER_URL,
        "kospi_code.mst.zip",
        "kospi_code.mst",
        parse_kospi_master,
    )
    kosdaq = _download_and_parse(
        KOSDAQ_MASTER_URL,
        "kosdaq_code.mst.zip",
        "kosdaq_code.mst",
        parse_kosdaq_master,
    )
    return kospi + kosdaq
