"""成绩单前端：缺测分项显示为「缺测」，不当成 0 分，也不由分项推算总分。

在 Node 的 vm 沙箱里运行真实的 frontend/app.js（见 frontend_report_harness.js），
本机没有 Node 时跳过。
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "frontend" / "app.js"
HARNESS = ROOT / "tests" / "frontend_report_harness.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None,
    reason="需要 Node.js 在沙箱中运行 frontend/app.js",
)

_TEXT = {"core_weakness": "例子少", "improvement_tips": "补充例子", "closing": "辛苦了"}


def _report(report: dict, text: str = "") -> dict:
    return {"type": "report", "report": {**report, **_TEXT}, "text": text}


def _run(scenarios: dict) -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS), str(APP_JS)],
        input=json.dumps(scenarios, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_complete_report_shows_every_score_and_no_missing_mark():
    out = _run({
        "complete": _report({
            "total_score": 86,
            "scores": {"profession_know": 88, "exp_logic": 84, "comper_quality": 87},
        }),
    })["complete"]

    assert out["score"] == "86"
    assert out["dims"] == {"profession_know": "88", "exp_logic": "84", "comper_quality": "87"}
    assert out["structured"] is True
    assert "不完整" not in out["summary"]


def test_missing_subscore_shows_missing_mark_and_empty_bar_not_zero():
    out = _run({
        "null_item": _report({
            "total_score": 86,
            "scores": {"profession_know": 88, "exp_logic": None, "comper_quality": 87},
        }),
    })["null_item"]

    assert out["score"] == "86"  # 有效总分保留
    assert out["dims"] == {"profession_know": "88", "exp_logic": "缺测", "comper_quality": "87"}
    assert out["fills"]["exp_logic"] == "0"  # 缺测的分项不画条
    assert "不完整" in out["summary"]


def test_absent_or_unusable_subscores_all_show_missing():
    out = _run({
        "absent": _report({"total_score": 80}),
        "unusable": _report({
            "total_score": 80,
            "scores": {"profession_know": "", "exp_logic": "高", "comper_quality": True},
        }),
    })

    for name in ("absent", "unusable"):
        assert out[name]["score"] == "80", name
        assert out[name]["dims"] == {
            "profession_know": "缺测",
            "exp_logic": "缺测",
            "comper_quality": "缺测",
        }, name
        assert "不完整" in out[name]["summary"], name


def test_real_zero_and_numeric_strings_are_scores_not_missing():
    out = _run({
        "zeros": _report({
            "total_score": 0,
            "scores": {"profession_know": 0, "exp_logic": 0, "comper_quality": 0},
        }),
        "strings": _report({
            "total_score": "86",
            "scores": {"profession_know": "88", "exp_logic": " 84 ", "comper_quality": 150},
        }),
    })

    assert out["zeros"]["score"] == "0"
    assert out["zeros"]["dims"] == {"profession_know": "0", "exp_logic": "0", "comper_quality": "0"}
    assert out["strings"]["score"] == "86"
    assert out["strings"]["dims"] == {"profession_know": "88", "exp_logic": "84", "comper_quality": "100"}


@pytest.mark.parametrize(
    "report",
    [
        {"scores": {"profession_know": 80, "exp_logic": 80, "comper_quality": 80}},  # 没有总分
        {"total_score": None, "scores": {"profession_know": 80, "exp_logic": 80, "comper_quality": 80}},
        {"total_score": "高", "scores": {"profession_know": 80, "exp_logic": 80, "comper_quality": 80}},
    ],
)
def test_report_without_total_falls_back_to_raw_text_and_invents_no_total(report):
    raw_text = "面试结束。原始成绩单文本"
    out = _run({"no_total": {"type": "report", "report": {**report, **_TEXT}, "text": raw_text}})["no_total"]

    assert out["structured"] is False
    assert out["rawVisible"] is True
    assert out["raw"] == raw_text
    assert out["score"] == "--"  # 不由分项平均推算总分
