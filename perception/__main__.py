"""用本机录音和转写计算表达特征。

示例::

    python -m perception --audio turn.wav --transcript "考生回答"
    python -m perception --manifest turns.json

清单是 JSON 数组，每一项包含 ``audio`` 和 ``transcript``。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from perception.audio import measure_wav
from perception.report_text import delivery_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从本机录音和转写计算表达特征")
    parser.add_argument("--audio", help="WAV 路径")
    parser.add_argument("--transcript", default="", help="这一段录音对应的转写")
    parser.add_argument("--manifest", help="含 audio 与 transcript 的 JSON 清单")
    args = parser.parse_args(argv)

    pairs = _pairs(args)
    turns = [measure_wav(path, transcript) for path, transcript in pairs]
    json.dump(delivery_report(turns), sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


def _pairs(args) -> list[tuple[Path, str]]:
    if args.manifest:
        payload = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        return [(Path(item["audio"]), str(item.get("transcript") or "")) for item in payload]
    if not args.audio:
        raise SystemExit("需要 --audio 或 --manifest")
    return [(Path(args.audio), args.transcript)]


if __name__ == "__main__":
    raise SystemExit(main())
