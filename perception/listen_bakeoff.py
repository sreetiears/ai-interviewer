"""离线听感横评。一个录音文件一条横评记录，不进入面试状态图。"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

_WAIT_SECONDS = 120.0
_AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".ogg"}


class ListenCallError(Exception):
    """接口失败。这一次是未判断，延迟仍记下。"""


@dataclass(frozen=True)
class ListenReply:
    """模型一次回包。用量缺席时为 None，不填价格。"""

    text: str
    usage: dict | None = None


class ListenClient(Protocol):
    def listen(self, audio: bytes, filename: str) -> ListenReply:
        """听一段音频并返回文字。超时或接口失败由调用方抛出。"""


@dataclass(frozen=True)
class ListenRun:
    """一次跑。犹豫和说完同时为 None 时，这一次是未判断。"""

    hesitant: bool | None
    finished: bool | None
    latency_seconds: float | None
    usage: dict | None


@dataclass(frozen=True)
class ListenRecord:
    """一个录音文件的横评记录。comparison 为 stable、unstable 或 incomplete。"""

    source_name: str
    runs: tuple[ListenRun, ListenRun]
    comparison: str


@dataclass(frozen=True)
class ListenConclusion:
    """满三十个录音文件后的汇总。它不决定能否接入直播。"""

    file_count: int
    stable_count: int
    unstable_count: int
    incomplete_count: int
    latency_seconds: tuple[float, ...]
    usages: tuple[dict | None, ...]


@dataclass(frozen=True)
class ListenBatch:
    records: tuple[ListenRecord, ...]
    below_threshold: bool
    conclusion: ListenConclusion | None


def run_listen_bakeoff(
    audio_dir: Path,
    record_dir: Path,
    listener: ListenClient,
    clock: Callable[[], float] = time.monotonic,
) -> ListenBatch:
    record_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(p for p in Path(audio_dir).iterdir() if p.is_file()):
        if path.suffix.lower() not in _AUDIO_SUFFIXES:
            _keep(records, record_dir, _unopened(path.name))
            continue
        try:
            audio = path.read_bytes()
        except OSError:
            _keep(records, record_dir, _unopened(path.name))
            continue
        runs: list[ListenRun] = []
        for _ in range(2):
            started = clock()
            try:
                reply = listener.listen(audio, path.name)
            except TimeoutError:
                runs.append(ListenRun(None, None, _WAIT_SECONDS, None))
                continue
            except ListenCallError:
                runs.append(ListenRun(None, None, clock() - started, None))
                continue
            latency = clock() - started
            hesitant, finished = _judgment(reply.text)
            runs.append(ListenRun(hesitant, finished, latency, reply.usage))
        pair = (runs[0], runs[1])
        _keep(records, record_dir, ListenRecord(path.name, pair, _comparison(pair)))
    return _batch(records)


def _keep(records: list[ListenRecord], record_dir: Path, record: ListenRecord) -> None:
    records.append(record)
    _write_record(record_dir, record)


def read_listen_bakeoff(record_dir: Path) -> ListenBatch:
    records = []
    for path in sorted(Path(record_dir).glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        runs = tuple(
            ListenRun(
                run["hesitant"],
                run["finished"],
                run["latency_seconds"],
                run["usage"],
            )
            for run in payload["runs"]
        )
        records.append(ListenRecord(payload["source_name"], runs, payload["comparison"]))
    return _batch(records)


def _batch(records: list[ListenRecord]) -> ListenBatch:
    conclusion = None
    if len(records) >= 30:
        latencies: list[float] = []
        usages: list[dict | None] = []
        for record in records:
            for run in record.runs:
                if run.latency_seconds is not None:
                    latencies.append(run.latency_seconds)
                usages.append(run.usage)
        conclusion = ListenConclusion(
            file_count=len(records),
            stable_count=sum(record.comparison == "stable" for record in records),
            unstable_count=sum(record.comparison == "unstable" for record in records),
            incomplete_count=sum(record.comparison == "incomplete" for record in records),
            latency_seconds=tuple(latencies),
            usages=tuple(usages),
        )
    return ListenBatch(tuple(records), len(records) < 30, conclusion)


def _write_record(record_dir: Path, record: ListenRecord) -> None:
    payload = {
        "source_name": record.source_name,
        "comparison": record.comparison,
        "runs": [
            {
                "hesitant": run.hesitant,
                "finished": run.finished,
                "latency_seconds": run.latency_seconds,
                "usage": run.usage,
            }
            for run in record.runs
        ],
    }
    (record_dir / f"{record.source_name}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _unopened(source_name: str) -> ListenRecord:
    blank = ListenRun(None, None, None, None)
    return ListenRecord(source_name, (blank, blank), "incomplete")


def main(argv: list[str] | None = None) -> int:
    """读本机目录，串行跑横评。目录没配好时不猜测路径。"""
    del argv
    from config import config, listen_bakeoff_dirs
    from perception.listen_client import SiliconFlowListenClient

    audio_dir, record_dir = listen_bakeoff_dirs()
    if not audio_dir or not record_dir:
        sys.stderr.write(
            "需要在本机配置 LISTEN_BAKEOFF_AUDIO_DIR 和 LISTEN_BAKEOFF_RECORD_DIR，目录放在仓库外。\n"
        )
        return 2
    if not config.siliconflow_api_key:
        sys.stderr.write("需要 SILICONFLOW_API_KEY。\n")
        return 2
    batch = run_listen_bakeoff(
        Path(audio_dir),
        Path(record_dir),
        SiliconFlowListenClient(config.siliconflow_api_key, config.siliconflow_base_url),
    )
    if batch.conclusion is None:
        sys.stdout.write(f"未达门槛：{len(batch.records)} 个文件。\n")
        return 0
    conclusion = batch.conclusion
    sys.stdout.write(
        "横评结论："
        f"{conclusion.file_count} 个文件，"
        f"稳定 {conclusion.stable_count}，"
        f"不稳定 {conclusion.unstable_count}，"
        f"未完成 {conclusion.incomplete_count}。\n"
    )
    return 0


def _judgment(text: str) -> tuple[bool | None, bool | None]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    hesitant = _yes_no(payload.get("犹豫"))
    finished = _yes_no(payload.get("说完"))
    if hesitant is None or finished is None:
        return None, None
    return hesitant, finished


def _yes_no(value: object) -> bool | None:
    if value == "是":
        return True
    if value == "否":
        return False
    return None


def _comparison(runs: tuple[ListenRun, ListenRun]) -> str:
    if any(run.hesitant is None or run.finished is None for run in runs):
        return "incomplete"
    if (runs[0].hesitant, runs[0].finished) == (runs[1].hesitant, runs[1].finished):
        return "stable"
    return "unstable"


if __name__ == "__main__":
    raise SystemExit(main())
