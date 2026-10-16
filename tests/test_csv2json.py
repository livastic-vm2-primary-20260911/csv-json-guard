from __future__ import annotations
import io
import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from csv2json import ConversionError, csv_to_json, sniff_file


def convert(text: str, **options):
    sink = io.StringIO()
    count = csv_to_json(io.StringIO(text, newline=""), sink, **options)
    return count, sink.getvalue()


def test_basic_header_preserves_all_strings():
    count, out = convert("name,age\nAda,37\nBob,02\n", indent=0)
    assert count == 2
    assert json.loads(out) == [{"name": "Ada", "age": "37"}, {"name": "Bob", "age": "02"}]


def test_unicode_and_csv_quotes_roundtrip():
    _, out = convert('name,note\n홍길동,"그는 ""안녕"" 이라고 말했다"\n', indent=0)
    assert json.loads(out) == [{"name": "홍길동", "note": '그는 "안녕" 이라고 말했다'}]


def test_quoted_embedded_commas_and_newlines():
    _, out = convert('id,description\n1,"Hello,\nWorld"\n', indent=2)
    assert json.loads(out)[0]["description"] == "Hello,\nWorld"


@pytest.mark.parametrize("delim", [";", "\t", "|"])
def test_explicit_delimiters(delim):
    count, result = convert(f"a{delim}b\nx{delim}y\n", delimiter=delim)
    assert count == 1
    assert json.loads(result) == [{"a": "x", "b": "y"}]


def test_jsonl_one_json_per_line():
    count, out = convert("a,b\n1,2\n3,4\n", output_format="jsonl")
    assert count == 2
    assert [json.loads(s) for s in out.splitlines()] == [
        {"a": "1", "b": "2"}, {"a": "3", "b": "4"}
    ]


def test_empty_file_writes_empty_array_or_no_lines():
    assert convert("", indent=0) == (0, "[]")
    assert convert("", output_format="jsonl") == (0, "")


def test_duplicate_column_names_are_rejected():
    with pytest.raises(ConversionError, match="Duplicate"):
        convert("a,a\nx,y\n")


def test_blank_header_name_rejected():
    with pytest.raises(ConversionError, match="Empty"):
        convert("a, \nx,y\n")


def test_ragged_rows_rejected_by_default():
    with pytest.raises(ConversionError, match="pad-missing"):
        convert("a,b,c\n1,2\n")


def test_explicit_padding_never_drops_data():
    _, out = convert("a,b,c\n1,2\n", pad_missing=True)
    assert json.loads(out) == [{"a": "1", "b": "2", "c": ""}]


def test_extra_field_rejected_even_if_padding_enabled():
    with pytest.raises(ConversionError, match="Extra data"):
        convert("a,b\n1,2,3\n", pad_missing=True)


def test_headerless_infers_column_names():
    _, out = convert("a,2\nb,3\n", no_header=True)
    assert json.loads(out)[0] == {"column_1": "a", "column_2": "2"}


def test_unterminated_quote_fails_closed():
    with pytest.raises(ConversionError, match="Malformed"):
        convert('name\n"unterminated\n')


def test_field_limit_is_validated():
    with pytest.raises(ConversionError, match="Max field"):
        convert("x\ny\n", max_field_chars=0)
    with pytest.raises(Exception):
        convert("x\n123456789\n", max_field_chars=4)


def test_json_without_indent_is_valid():
    _, out = convert("a,b\nx,y\n", indent=0)
    assert out == '[{"a":"x","b":"y"}]'


def test_auto_sniff(tmp_path):
    src = tmp_path / "sample.csv"
    src.write_text("one;two\na;b\n", encoding="utf-8")
    assert sniff_file(src, "utf-8-sig") == ";"


def test_cli_atomic_output_success(tmp_path):
    src = tmp_path / "sample.csv"
    dest = tmp_path / "result.json"
    src.write_text("name,city\nJi,서울\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "csv2json.py"), str(src),
                        "-o", str(dest)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(dest.read_text(encoding="utf-8")) == [{"name": "Ji", "city": "서울"}]


def test_cli_preserves_existing_output_on_failure(tmp_path):
    src = tmp_path / "bad.csv"
    dest = tmp_path / "preserved.json"
    src.write_text("a,b\n1,2,3\n", encoding="utf-8")
    dest.write_text("USER ORIGINAL", encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "csv2json.py"), str(src),
                        "-o", str(dest), "--delimiter", "comma"], capture_output=True, text=True)
    assert r.returncode == 2
    assert dest.read_text(encoding="utf-8") == "USER ORIGINAL"
    assert not any(tmp_path.glob(".csv-to-json-*.tmp"))


def test_cli_bom(tmp_path):
    src = tmp_path / "bom.csv"
    src.write_bytes(b"\xef\xbb\xbfa,b\n1,2\n")
    r = subprocess.run([sys.executable, str(ROOT / "csv2json.py"),
                        str(src), "--indent", "0"], capture_output=True, text=True)
    assert r.returncode == 0
    assert json.loads(r.stdout) == [{"a": "1", "b": "2"}]


def test_stdin_requires_delimiter(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "csv2json.py"), "-"],
                       input="a\n1\n", capture_output=True, text=True)
    assert r.returncode == 2
    assert "requires a seekable path" in r.stderr


def test_cli_refuses_same_source_and_destination(tmp_path):
    src = tmp_path / "keep.csv"
    original = "name,city\nJi,Seoul\n"
    src.write_text(original)
    r = subprocess.run([sys.executable, str(ROOT / "csv2json.py"), str(src),
                        "-o", str(src)], capture_output=True, text=True)
    assert r.returncode == 2
    assert "same file" in r.stderr
    assert src.read_text() == original

def test_cli_refuses_symlink_destination(tmp_path):
    src = tmp_path / "valid.csv"
    dst = tmp_path / "real.json"
    alias = tmp_path / "alias.json"
    src.write_text("a,b\nx,y\n")
    dst.write_text("PRESERVE")
    alias.symlink_to(dst)
    r = subprocess.run([sys.executable, str(ROOT / "csv2json.py"), str(src),
                        "-o", str(alias)], capture_output=True, text=True)
    assert r.returncode == 2
    assert dst.read_text() == "PRESERVE"
