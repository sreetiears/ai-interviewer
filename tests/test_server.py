import pytest
import os
from server import create_token, InterviewHttpHandler
from http.server import HTTPServer


def test_create_token():
    token = create_token("test-room", "test-user")
    assert isinstance(token, str)
    assert len(token) > 20
    assert token.startswith("ey")  # JWT prefix
