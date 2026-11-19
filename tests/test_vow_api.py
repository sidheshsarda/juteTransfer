"""The VoW payments client: headers, URL, and errors turned into words."""

import io
import json
import urllib.error

import pytest

from src.jutetransfer import vow_api


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def linked(monkeypatch):
    monkeypatch.setenv("JT_VOW_API_URL", "http://vow.test:8000/")
    monkeypatch.setenv("JT_VOW_API_KEY", "s3cret")
    monkeypatch.delenv("JT_VOW_TENANT", raising=False)


def test_not_configured_is_said_in_words(monkeypatch):
    monkeypatch.delenv("JT_VOW_API_URL", raising=False)
    monkeypatch.delenv("JT_VOW_API_KEY", raising=False)
    assert not vow_api.configured()
    with pytest.raises(vow_api.VowApiError, match="not set up on this server"):
        vow_api.payment_options(67)


def test_a_call_carries_the_key_the_tenant_and_the_body(linked):
    seen = {}

    def opener(request, timeout):
        seen.update(url=request.full_url, method=request.get_method(), body=request.data,
                    headers={k.lower(): v for k, v in request.header_items()})
        return FakeResponse(json.dumps({"data": {"voucher_no": "PAY-00002"}}).encode())
    out = vow_api._call("POST", "", body={"co_id": 67, "bills": [{"jute_mr_id": 1, "amount": 5.5}]},
                        opener=opener)
    assert out == {"voucher_no": "PAY-00002"}
    assert seen["url"] == "http://vow.test:8000/api/integration/jute_payments"
    assert seen["method"] == "POST" and json.loads(seen["body"])["bills"][0]["amount"] == 5.5
    assert seen["headers"]["x-integration-key"] == "s3cret" and seen["headers"]["x-tenant"] == "sls"

    vow_api._call("GET", "/options", {"co_id": 67}, opener=opener)
    assert seen["url"].endswith("/api/integration/jute_payments/options?co_id=67")


def test_the_bill_list_is_unwrapped(linked, monkeypatch):
    bills = [{"jute_mr_id": 1, "allocatable_amount": 5.0}]
    monkeypatch.setattr(vow_api, "_call", lambda *a, **k: bills)       # _call strips {"data": ...}
    assert vow_api.open_bills(67, 501) == bills


@pytest.mark.parametrize("status, body, message", [
    (400, b'{"detail": "Bill ACPL/JMR/26-27/00024 is not posted in accounts"}', "not posted in accounts"),
    (422, b'[{"x": 1}]', r"error \(422\)"),
    (422, b'{"detail": [{"msg": "field required"}]}', "field required"),
    (401, b"", "integration key"),
    (503, b"<html>", "not switched on in VoW"),
])
def test_vows_refusals_come_back_as_its_own_words(linked, status, body, message):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "x", {}, io.BytesIO(body))
    with pytest.raises(vow_api.VowApiError, match=message):
        vow_api._call("GET", "/options", opener=opener)


def test_an_unreachable_server_is_said_in_words(linked):
    def opener(request, timeout):
        raise urllib.error.URLError("connection refused")
    with pytest.raises(vow_api.VowApiError, match="Could not reach VoW accounts: connection refused"):
        vow_api._call("GET", "/options", opener=opener)
