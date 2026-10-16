#!/usr/bin/env python3
"""Stream CSV records to JSON arrays or JSON Lines with strict, documented validation.

No network, cloud API, external packages or unsafe deserialization.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import IO, Iterator

DELIMITERS = {
    "comma": ",",
    "semicolon": ";",
    "tab": "\t",
    "pipe": "|",
}


class ConversionError(ValueError):
    """An invalid or ambiguous CSV representation."""


def read_records(
    reader: csv.reader,
    *,
    no_header: bool,
    pad_missing: bool,
) -> Iterator[dict[str, str]]:
    try:
        first = next(reader, None)
    except csv.Error as exc:
        raise ConversionError(f"Failed to read the CSV header: {exc}") from exc
    if first is None:
        return
    if no_header:
        headers = [f"column_{n}" for n in range(1, len(first) + 1)]
        first_record = first
    else:
        headers = first
        first_record = None
    if not headers or any(not name.strip() for name in headers):
        raise ConversionError("Empty or whitespace-only header names are not supported.")
    if len(set(headers)) != len(headers):
        raise ConversionError("Duplicate header names would silently overwrite fields; rename them first.")

    def as_object(row: list[str], row_number: int) -> dict[str, str]:
        if len(row) > len(headers):
            raise ConversionError(
                f"CSV record {row_number} has {len(row)} fields, expected {len(headers)}. "
                "Extra data cannot be dropped silently."
            )
        if len(row) < len(headers):
            if not pad_missing:
                raise ConversionError(
                    f"CSV record {row_number} has {len(row)} fields, expected {len(headers)}. "
                    "Use --pad-missing to fill missing trailing fields with empty strings."
                )
            row = row + [""] * (len(headers) - len(row))
        return dict(zip(headers, row))

    if first_record is not None:
        yield as_object(first_record, 1)
    while True:
        try:
            row = next(reader, None)
        except csv.Error as exc:
            raise ConversionError(f"Malformed CSV near logical record {reader.line_num}: {exc}") from exc
        if row is None:
            break
        if not row:
            continue
        yield as_object(row, reader.line_num)


def csv_to_json(
    source: IO[str],
    sink: IO[str],
    *,
    delimiter: str = ",",
    no_header: bool = False,
    pad_missing: bool = False,
    output_format: str = "json",
    indent: int = 2,
    max_field_chars: int = 1_048_576,
) -> int:
    if delimiter not in DELIMITERS.values() or len(delimiter) != 1:
        raise ConversionError("Delimiter must be a comma, semicolon, tab or pipe.")
    if output_format not in ("json", "jsonl"):
        raise ConversionError("Format must be json or jsonl.")
    if indent not in (0, 2, 4):
        raise ConversionError("Indent must be 0, 2, or 4.")
    if not (1 <= max_field_chars <= 16_777_216):
        raise ConversionError("Max field characters out of supported range.")
    csv.field_size_limit(max_field_chars)
    reader = csv.reader(source, delimiter=delimiter, strict=True)
    count = 0
    first = True
    if output_format == "json":
        sink.write("[" if indent == 0 else "[\n")
    for obj in read_records(reader, no_header=no_header, pad_missing=pad_missing):
        if output_format == "jsonl":
            sink.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
        elif indent == 0:
            if not first:
                sink.write(",")
            sink.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
        else:
            rendered = json.dumps(obj, ensure_ascii=False, indent=indent)
            prefix = " " * indent
            rendered = "\n".join(prefix + line for line in rendered.splitlines())
            if not first:
                sink.write(",\n")
            sink.write(rendered)
        first = False
        count += 1
    if output_format == "json":
        sink.write("]" if indent == 0 else ("\n]\n" if count else "]\n"))
    return count


def sniff_file(path: Path, encoding: str) -> str:
    with path.open("r", encoding=encoding, newline="") as source:
        sample = source.read(8192)
    if not sample:
        return ","
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        print("Warning: delimiter is ambiguous; assuming comma. Override using --delimiter.", file=sys.stderr)
        return ","


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CSV -> JSON or JSONL converter. All field values remain strings; no network usage."
    )
    parser.add_argument("input", help="UTF-8 CSV path or '-' for stdin")
    parser.add_argument("-o", "--output", default="-", help="Destination path, or '-' for stdout")
    parser.add_argument("--delimiter", choices=["auto", *DELIMITERS], default="auto",
                        help="Guess from file sample or choose comma/semicolon/tab/pipe.")
    parser.add_argument("--format", dest="output_format", choices=["json", "jsonl"], default="json")
    parser.add_argument("--indent", type=int, choices=[0, 2, 4], default=2)
    parser.add_argument("--encoding", default="utf-8-sig",
                        help="Input codec, default utf-8-sig (supports UTF-8 BOM).")
    parser.add_argument("--no-header", action="store_true",
                        help="Treat the first CSV record as data and name fields column_1...")
    parser.add_argument("--pad-missing", action="store_true",
                        help="Fill missing trailing fields with empty strings (never drop extras).")
    parser.add_argument("--max-field-chars", type=int, default=1_048_576,
                        help="Maximum characters in any one field (1 to 16777216).")
    return parser


def main(argv: list[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    if options.input == "-" and options.delimiter == "auto":
        print("Error: --delimiter auto requires a seekable path; specify one for stdin.", file=sys.stderr)
        return 2
    source = None
    sink = None
    temp_path: Path | None = None
    try:
        input_path = None if options.input == "-" else Path(options.input)
        delimiter = sniff_file(input_path, options.encoding) if options.delimiter == "auto" else DELIMITERS[options.delimiter]
        source = io.TextIOWrapper(sys.stdin.buffer, encoding=options.encoding, newline="") if input_path is None else input_path.open("r", encoding=options.encoding, newline="")
        if options.output == "-":
            sink = sys.stdout
        else:
            dest = Path(options.output)
            if input_path is not None and input_path.resolve() == dest.resolve():
                raise ConversionError("Input and output cannot be the same file; refusing to overwrite source CSV.")
            if dest.is_symlink():
                raise ConversionError("Output destination must not be a symlink.")
            if not dest.parent.exists():
                raise ConversionError(f"Output directory does not exist: {dest.parent}")
            fd, name = tempfile.mkstemp(prefix=".csv-to-json-", suffix=".tmp", dir=dest.parent)
            temp_path = Path(name)
            sink = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        n = csv_to_json(source, sink, delimiter=delimiter, no_header=options.no_header,
                        pad_missing=options.pad_missing, output_format=options.output_format,
                        indent=options.indent, max_field_chars=options.max_field_chars)
        if options.output != "-":
            sink.flush()
            os.fsync(sink.fileno())
            sink.close()
            sink = None
            os.replace(temp_path, Path(options.output))
            temp_path = None
        print(f"Converted {n} CSV record(s) to {options.output_format}", file=sys.stderr)
        return 0
    except (ConversionError, csv.Error, UnicodeError, OSError, LookupError, ValueError) as exc:
        print(f"Conversion failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if sink is not None and sink is not sys.stdout:
            sink.close()
        if source is not None:
            # The default stdin text wrapper is a temporary view over the parent stream.
            source.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
