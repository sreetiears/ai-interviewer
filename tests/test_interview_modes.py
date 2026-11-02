"""The mode vocabulary must be the single source of truth for every layer."""
from interview_modes import DEFAULT_MODE, MODE_NAMES, is_valid_mode, normalize_mode


def test_mode_names_and_default():
    assert MODE_NAMES == ("quick", "real")
    assert DEFAULT_MODE == "quick"


def test_normalize_mode_handles_case_and_whitespace():
    assert normalize_mode(" REAL ") == "real"
    assert normalize_mode("Quick") == "quick"


def test_normalize_mode_falls_back_for_unknown_or_missing():
    assert normalize_mode("turbo") == "quick"
    assert normalize_mode("") == "quick"
    assert normalize_mode(None) == "quick"


def test_is_valid_mode_is_strict():
    assert is_valid_mode("real") is True
    assert is_valid_mode(" REAL ") is True
    assert is_valid_mode("turbo") is False
    assert is_valid_mode(None) is False


def test_server_and_graph_share_the_same_mode_names():
    import interview_graph
    import server

    assert server.INTERVIEW_MODES == MODE_NAMES
    assert set(interview_graph.INTERVIEW_MODES) == set(MODE_NAMES)
