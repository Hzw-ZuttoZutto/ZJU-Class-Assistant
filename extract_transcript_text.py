#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract all 'text' field values from a JSONL file into a sibling .txt file."
    )
    parser.add_argument("jsonl_path", help="Path to the source .jsonl file")
    return parser.parse_args()


def resolve_output_path(jsonl_path: Path) -> Path:
    if jsonl_path.suffix:
        return jsonl_path.with_suffix(".txt")
    return jsonl_path.with_name(f"{jsonl_path.name}.txt")


def collect_text_values(node: Any, results: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "text" and value is not None:
                if isinstance(value, str):
                    results.append(value)
                elif not isinstance(value, (dict, list)):
                    results.append(str(value))
            collect_text_values(value, results)
        return

    if isinstance(node, list):
        for item in node:
            collect_text_values(item, results)


def extract_texts(jsonl_path: Path) -> list[str]:
    texts: list[str] = []

    with jsonl_path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON on line {line_number}: {exc.msg}"
                ) from exc

            collect_text_values(payload, texts)

    return texts


def write_output(output_path: Path, texts: list[str]) -> None:
    content = "\n".join(texts)
    if texts:
        content += "\n"
    output_path.write_text(content, encoding="utf-8")


def main() -> int:
    args = parse_args()
    jsonl_path = Path(args.jsonl_path).expanduser().resolve()

    if not jsonl_path.is_file():
        print(f"Input file not found: {jsonl_path}", file=sys.stderr)
        return 1

    output_path = resolve_output_path(jsonl_path)

    try:
        texts = extract_texts(jsonl_path)
        write_output(output_path, texts)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"File error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(texts)} text entries to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
