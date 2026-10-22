import pytest
from interview_graph import route_phase, create_interview_graph


def test_route_phase():
    assert route_phase({"current_phase": "self_intro"}) == "self_intro"
    assert route_phase({"current_phase": "pro_qa"}) == "pro_qa"
    assert route_phase({"current_phase": "compre_qa"}) == "compre_qa"
    assert route_phase({"current_phase": "report"}) == "report"
    assert route_phase({"current_phase": "completed"}) == "end"


def test_graph_compilation():
    graph = create_interview_graph()
    assert graph is not None
