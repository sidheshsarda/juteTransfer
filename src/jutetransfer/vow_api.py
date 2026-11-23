"""Client for VoW's jute-payments integration API (server to server).

The Payment Tracker never writes accounting rows itself: a payment voucher is
created by VoW, through the same function its own Pay Bills screen uses, and
this app only sends the request (contract: vowerp3be
docs/integration/jute_payments_api.md).

.env:
  JT_VOW_API_URL   base URL of the VoW backend, e.g. http://127.0.0.1:8000
  JT_VOW_API_KEY   the integration key (VoW's VOW_INTEGRATION_KEY)
  JT_VOW_TENANT    tenant name, default 'sls'
Without URL and key the page stays read-only. No Streamlit import.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

BASE_PATH = "/api/integration/jute_payments"
TIMEOUT = 30  # seconds


class VowApiError(Exception):
    """VoW refused the request or could not be reached; str() is for the user."""


def _setting(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def configured() -> bool:
    return bool(_setting("JT_VOW_API_URL") and _setting("JT_VOW_API_KEY"))


def _error_text(status: int, raw: bytes) -> str:
    try:
        detail = json.loads(raw.decode("utf-8", "replace")).get("detail")
    except (ValueError, AttributeError):
        detail = None
    if isinstance(detail, list):                      # FastAPI validation errors
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    if isinstance(detail, dict):
        detail = detail.get("message") or json.dumps(detail)
    if detail:
        return str(detail)
    return {401: "VoW did not accept this app's integration key.",
            503: "The payments link is not switched on in VoW."}.get(
        status, f"VoW answered with an error ({status}).")


def _call(method: str, path: str, params: Optional[dict] = None,
          body: Optional[dict] = None, opener=urllib.request.urlopen) -> dict:
    if not configured():
        raise VowApiError("The link to VoW accounts is not set up on this server "
                          "(JT_VOW_API_URL / JT_VOW_API_KEY).")
    url = _setting("JT_VOW_API_URL").rstrip("/") + BASE_PATH + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body, default=str).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "X-Integration-Key": _setting("JT_VOW_API_KEY"),
        "X-Tenant": _setting("JT_VOW_TENANT", "sls") or "sls",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with opener(request, timeout=TIMEOUT) as response:
            answer = json.loads(response.read().decode("utf-8") or "{}")
            # VoW wraps every successful answer as {"data": ...}
            return answer["data"] if isinstance(answer, dict) and "data" in answer else answer
    except urllib.error.HTTPError as e:
        raise VowApiError(_error_text(e.code, e.read())) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VowApiError(f"Could not reach VoW accounts: {getattr(e, 'reason', e)}") from e
    except ValueError as e:
        raise VowApiError("VoW sent back something this app could not read.") from e


def payment_options(co_id: int) -> dict:
    """Branches, bank / cash ledgers and pay modes of the paying company."""
    return _call("GET", "/options", {"co_id": co_id})


def open_bills(co_id: int, party_id: int) -> list:
    """Every open bill of the supplier (BILLPASS and OPENING) as accounts sees
    it: acc_bill_ref_id, jute_mr_id (may be null), allocatable_amount,
    on_hold, hold_note, source."""
    out = _call("GET", "/bills", {"co_id": co_id, "party_id": party_id})
    return out.get("bills", []) if isinstance(out, dict) else out


def preview_payment(payload: dict) -> dict:
    return _call("POST", "/preview", body=payload)


def create_payment(payload: dict) -> dict:
    return _call("POST", "", body=payload)
