import os
import time

# server import 전에 결정적 자격증명/키를 강제 설정한다.
os.environ["REPORTS_USER"] = "reader"
os.environ["REPORTS_PASS"] = "readerpass"
os.environ["REPORTS_UPLOAD_USER"] = "uploader"
os.environ["REPORTS_UPLOAD_PASS"] = "uploaderpass"
os.environ["REPORTS_SECRET_KEY"] = "test-secret-deadbeef-0123456789abcdef"

import jwt
import pytest
from fastapi.testclient import TestClient

import server
from server import app

client = TestClient(app)


def test_healthz_no_auth():
    assert client.get("/healthz").status_code == 200
