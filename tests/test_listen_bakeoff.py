"""离线听感横评的公开结果。"""

from pathlib import Path

from perception.listen_bakeoff import (
    ListenCallError,
    ListenReply,
    main,
    read_listen_bakeoff,
    run_listen_bakeoff,
)


class ScriptedListener:
    def __init__(self, replies: list[ListenReply | Exception]):
        self._replies = list(replies)
        self.audio: list[bytes] = []
        self.names: list[str] = []

    def listen(self, audio: bytes, filename: str) -> ListenReply:
        self.audio.append(audio)
        self.names.append(filename)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _clock(*stamps: float):
    pending = iter(stamps)

    def clock() -> float:
        return next(pending)

    return clock


def test_two_matching_judgments_are_stable_and_below_threshold(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    record_dir = tmp_path / "records"
    audio_dir.mkdir()
    clip = b"RIFFfake-wav"
    (audio_dir / "turn.wav").write_bytes(clip)
    reply = ListenReply('{"犹豫": "是", "说完": "否"}', usage={"prompt_tokens": 13})
    listener = ScriptedListener([reply, reply])

    batch = run_listen_bakeoff(
        audio_dir,
        record_dir,
        listener,
        clock=_clock(0.0, 1.25, 1.25, 3.0),
    )

    assert listener.audio == [clip, clip]
    assert batch.below_threshold is True
    assert batch.conclusion is None
    assert len(batch.records) == 1
    record = batch.records[0]
    assert record.source_name == "turn.wav"
    assert record.comparison == "stable"
    assert len(record.runs) == 2
    assert record.runs[0].hesitant is True
    assert record.runs[0].finished is False
    assert record.runs[0].latency_seconds == 1.25
    assert record.runs[0].usage == {"prompt_tokens": 13}
    assert record.runs[1].hesitant is True
    assert record.runs[1].finished is False
    assert record.runs[1].latency_seconds == 1.75
    assert record.runs[1].usage == {"prompt_tokens": 13}


def test_different_judgments_are_unstable(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "turn.wav").write_bytes(b"RIFFfake-wav")
    listener = ScriptedListener(
        [
            ListenReply('{"犹豫": "否", "说完": "是"}'),
            ListenReply('{"犹豫": "是", "说完": "是"}'),
        ]
    )

    batch = run_listen_bakeoff(audio_dir, tmp_path / "records", listener, clock=_clock(0, 1, 1, 2))

    assert batch.records[0].comparison == "unstable"
    assert batch.records[0].runs[0].hesitant is False
    assert batch.records[0].runs[1].hesitant is True


def test_vague_answer_is_unjudged_and_incomplete(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "turn.wav").write_bytes(b"RIFFfake-wav")
    listener = ScriptedListener(
        [
            ListenReply('{"犹豫": "是", "说完": "是"}'),
            ListenReply('{"犹豫": "有点犹豫", "说完": "是", "情绪": "紧张"}'),
        ]
    )

    batch = run_listen_bakeoff(audio_dir, tmp_path / "records", listener, clock=_clock(0, 1, 1, 2))

    record = batch.records[0]
    assert record.comparison == "incomplete"
    assert record.runs[0].hesitant is True
    assert record.runs[1].hesitant is None
    assert record.runs[1].finished is None


def test_extra_transcript_is_not_kept(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "turn.wav").write_bytes(b"RIFFfake-wav")
    reply = ListenReply('{"犹豫": "否", "说完": "否", "转写": "我做过这个项目", "情绪": "平静"}')
    listener = ScriptedListener([reply, reply])

    batch = run_listen_bakeoff(audio_dir, tmp_path / "records", listener, clock=_clock(0, 1, 1, 2))

    run = batch.records[0].runs[0]
    assert run.hesitant is False
    assert run.finished is False
    assert not hasattr(run, "transcript")
    assert "转写" not in (run.usage or {})


def test_timeout_is_unjudged_with_the_full_wait(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "turn.wav").write_bytes(b"RIFFfake-wav")
    listener = ScriptedListener(
        [
            ListenReply('{"犹豫": "是", "说完": "是"}'),
            TimeoutError("hung"),
        ]
    )

    batch = run_listen_bakeoff(audio_dir, tmp_path / "records", listener, clock=_clock(0, 1, 10, 10.2))

    assert batch.records[0].comparison == "incomplete"
    assert batch.records[0].runs[0].hesitant is True
    assert batch.records[0].runs[0].latency_seconds == 1
    assert batch.records[0].runs[1].hesitant is None
    assert batch.records[0].runs[1].finished is None
    assert batch.records[0].runs[1].latency_seconds == 120
    assert batch.records[0].runs[1].usage is None


def test_call_error_keeps_latency_and_is_unjudged(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "turn.wav").write_bytes(b"RIFFfake-wav")
    listener = ScriptedListener(
        [
            ListenCallError("upstream"),
            ListenReply('{"犹豫": "否", "说完": "是"}'),
        ]
    )

    batch = run_listen_bakeoff(audio_dir, tmp_path / "records", listener, clock=_clock(5.0, 8.5, 8.5, 9.0))

    assert batch.records[0].comparison == "incomplete"
    assert batch.records[0].runs[0].hesitant is None
    assert batch.records[0].runs[0].latency_seconds == 3.5
    assert batch.records[0].runs[0].usage is None
    assert batch.records[0].runs[1].finished is True


def test_text_file_is_unjudged_without_calling_the_model(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "notes.txt").write_text("不是录音", encoding="utf-8")
    listener = ScriptedListener([])

    batch = run_listen_bakeoff(audio_dir, tmp_path / "records", listener, clock=_clock())

    assert listener.audio == []
    record = batch.records[0]
    assert record.source_name == "notes.txt"
    assert record.comparison == "incomplete"
    assert record.runs[0].latency_seconds is None
    assert record.runs[0].hesitant is None
    assert record.runs[1].latency_seconds is None


def test_unreadable_audio_is_unjudged_without_a_call(tmp_path: Path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "turn.wav").write_bytes(b"RIFFfake-wav")

    def deny(self: Path) -> bytes:
        raise OSError("denied")

    monkeypatch.setattr(Path, "read_bytes", deny)
    listener = ScriptedListener([])

    batch = run_listen_bakeoff(audio_dir, tmp_path / "records", listener, clock=_clock())

    assert listener.audio == []
    assert batch.records[0].comparison == "incomplete"
    assert batch.records[0].runs[0].latency_seconds is None
    assert batch.records[0].runs[1].hesitant is None


def test_files_and_their_two_runs_stay_in_order(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "b.wav").write_bytes(b"b")
    (audio_dir / "a.wav").write_bytes(b"a")
    reply = ListenReply('{"犹豫": "否", "说完": "是"}')
    listener = ScriptedListener([reply, reply, reply, reply])

    run_listen_bakeoff(audio_dir, tmp_path / "records", listener, clock=_clock(0, 1, 1, 2, 2, 3, 3, 4))

    assert listener.names == ["a.wav", "a.wav", "b.wav", "b.wav"]


def test_rerun_replaces_the_previous_pair(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    record_dir = tmp_path / "records"
    audio_dir.mkdir()
    (audio_dir / "turn.wav").write_bytes(b"RIFFfake-wav")
    first = ListenReply('{"犹豫": "是", "说完": "是"}')
    second = ListenReply('{"犹豫": "否", "说完": "否"}')

    run_listen_bakeoff(audio_dir, record_dir, ScriptedListener([first, first]), clock=_clock(0, 1, 1, 2))
    run_listen_bakeoff(audio_dir, record_dir, ScriptedListener([second, second]), clock=_clock(0, 1, 1, 2))

    stored = read_listen_bakeoff(record_dir)
    assert len(stored.records) == 1
    assert len(stored.records[0].runs) == 2
    assert stored.records[0].comparison == "stable"
    assert stored.records[0].runs[0].hesitant is False
    assert stored.records[0].runs[1].finished is False


def test_thirty_files_summarize_without_a_live_decision(tmp_path: Path):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    replies: list[ListenReply] = []
    yes = ListenReply('{"犹豫": "是", "说完": "是"}', usage={"prompt_tokens": 1})
    no = ListenReply('{"犹豫": "否", "说完": "是"}', usage={"prompt_tokens": 1})
    vague = ListenReply('{"犹豫": "有点"}', usage={"prompt_tokens": 1})
    for index in range(30):
        (audio_dir / f"{index:02d}.wav").write_bytes(b"wav")
        if index < 28:
            replies.extend([yes, yes])
        elif index == 28:
            replies.extend([yes, no])
        else:
            replies.extend([yes, vague])

    batch = run_listen_bakeoff(
        audio_dir,
        tmp_path / "records",
        ScriptedListener(replies),
        clock=_ticking(0.5),
    )

    assert batch.below_threshold is False
    conclusion = batch.conclusion
    assert conclusion is not None
    assert conclusion.file_count == 30
    assert conclusion.stable_count == 28
    assert conclusion.unstable_count == 1
    assert conclusion.incomplete_count == 1
    assert conclusion.latency_seconds == (0.5,) * 60
    assert conclusion.usages == ({"prompt_tokens": 1},) * 60
    assert not hasattr(conclusion, "go_live")


def test_main_refuses_when_local_directories_are_unset(monkeypatch):
    monkeypatch.delenv("LISTEN_BAKEOFF_AUDIO_DIR", raising=False)
    monkeypatch.delenv("LISTEN_BAKEOFF_RECORD_DIR", raising=False)

    assert main([]) == 2


def _ticking(step: float):
    now = 0.0

    def clock() -> float:
        nonlocal now
        now += step
        return now

    return clock
