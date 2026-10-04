"""Shared HTTP layer: JSON slicing, backoff, token bucket, signing, client capture."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from cdx_http.backoff import Kind, backoff_delay, classify, parse_retry_after
from cdx_http.client import HttpClient, build_url, redact_headers
from cdx_http.jsonutil import loads_decimal, one_line, raw_array_elements, raw_object_members
from cdx_http.ratelimit import TokenBucket
from cdx_http.signed import Credentials, load_credentials, serialize, sign, signed_request

FIX = Path(__file__).parent / "fixtures"


# ---- jsonutil ------------------------------------------------------------------

def test_raw_members_are_verbatim():
    s = '{"a": 0.00007903, "b":{"x":"}{\\"","y":[1,2]} ,"c":"s","d":null}'
    assert raw_object_members(s) == {"a": "0.00007903", "b": '{"x":"}{\\"","y":[1,2]}', "c": '"s"', "d": "null"}


def test_raw_members_empty_and_nested_arrays():
    assert raw_object_members(" { } ") == {}
    assert raw_array_elements('[ {"m":"]"} , 1.50,"x" ]') == ['{"m":"]"}', "1.50", '"x"']
    assert raw_array_elements("[]") == []


def test_float_roundtrip_would_not_be_verbatim():
    # the reason raw slicing exists
    assert json.dumps(json.loads("0.00007903")) == "7.903e-05"
    assert raw_object_members('{"fr":0.00007903}')["fr"] == "0.00007903"


def test_real_futures_rt_row_slice():
    text = (FIX / "futures_rt_2026-09-18T1237Z.json").read_text(encoding="utf-8")
    prices = raw_object_members(raw_object_members(text)["prices"])
    btc = prices["B-BTC_USDT"]
    assert btc.startswith('{"fr":0.00007903,"h":78443,')
    assert btc.endswith('"cmRT":1789735045156}')
    assert btc in text
    # schema-incomplete, stale row survives untouched
    assert set(json.loads(prices["B-NKN_USDT"])) == {"fr", "mp", "efr", "bmST", "cmRT", "skw"}


def test_loads_decimal_and_one_line():
    assert loads_decimal('{"p":11570.67000000}')["p"] == Decimal("11570.67000000")
    assert one_line('{"a":\n 1}') == '{"a":  1}'


# ---- backoff ------------------------------------------------------------------

@pytest.mark.parametrize("kind,expected", [
    (Kind.RATE_LIMITED, [1, 2, 4, 8, 16, 32, 60, 60]),
    (Kind.MAINTENANCE, [30, 60, 120, 240, 480, 900, 900, 900]),
    (Kind.SERVER_ERROR, [5, 10, 20, 40, 80, 120, 120, 120]),
    (Kind.TRANSPORT, [2, 4, 8, 16, 32, 60, 60, 60]),
])
def test_backoff_schedules(kind, expected):
    assert [backoff_delay(kind, n) for n in range(1, 9)] == expected


def test_retry_after_only_lengthens():
    assert backoff_delay(Kind.RATE_LIMITED, 1, retry_after_s=10) == 10
    assert backoff_delay(Kind.RATE_LIMITED, 3, retry_after_s=1) == 4
    assert parse_retry_after({"retry-after": "7"}) == 7.0
    assert parse_retry_after({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) is None


def test_classify():
    assert [classify(s) for s in (200, 204, 429, 503, 401, 500, 502, 404, 422)] == [
        Kind.OK, Kind.OK, Kind.RATE_LIMITED, Kind.MAINTENANCE, Kind.UNAUTHORIZED,
        Kind.SERVER_ERROR, Kind.SERVER_ERROR, Kind.CLIENT_ERROR, Kind.CLIENT_ERROR]


# ---- token bucket ------------------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.t += d


def test_token_bucket_spacing_and_penalty():
    c = FakeClock()
    b = TokenBucket(rate=2.0, burst=1, recovery_s=60, clock=c, sleep=c.sleep)
    times = []
    for _ in range(3):
        b.acquire()
        times.append(c.t)
    assert times == [0.0, 0.5, 1.0]
    b.penalize()                       # 2.0 -> 1.0 req/s, bucket emptied
    b.acquire()
    assert c.t == pytest.approx(2.0)
    c.t = 2.0 + 60                      # one recovery period: 1.0 * 1.25
    b.acquire()
    assert b.rate == pytest.approx(1.25)


# ---- signing -------------------------------------------------------------------------

def test_hmac_matches_openssl_vector():
    # echo -n '{"timestamp":1789735000000}' | openssl dgst -sha256 -hmac 'test-secret-not-real'
    body = serialize({"timestamp": 1789735000000})
    assert body == b'{"timestamp":1789735000000}'
    assert sign("test-secret-not-real", body) == "c84c01769111d24bbbbd5ab6b7aa266516c9d7bc70a93058e724a866e227ed70"


def test_signed_request_units_and_headers():
    creds = Credentials("k-123", "s-456")
    body_ms, h = signed_request(creds, {"pair": "B-BTC_USDT"}, "ms")
    body_s, _ = signed_request(creds, None, "s")
    ts_ms = json.loads(body_ms)["timestamp"]
    ts_s = json.loads(body_s)["timestamp"]
    assert list(json.loads(body_ms)) == ["pair", "timestamp"]
    assert ts_ms > 10**12 and ts_s < 10**11
    assert h["X-AUTH-SIGNATURE"] == sign("s-456", body_ms)
    assert "s-456" not in repr(creds) and "k-123" not in str(creds)


def test_load_credentials_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("COINDCX_API_KEY", raising=False)
    monkeypatch.delenv("COINDCX_API_SECRET", raising=False)
    env = tmp_path / ".env"
    env.write_text('# c\nCOINDCX_API_KEY="abc"\nCOINDCX_API_SECRET=def\n')
    c = load_credentials(env)
    assert (c.key, c.secret) == ("abc", "def")
    env.write_text("COINDCX_API_KEY=abc\nCOINDCX_API_SECRET=\n")
    assert load_credentials(env) is None


# ---- client ----------------------------------------------------------------------------

class FakeResp:
    def __init__(self, status, content, headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append((method, url, headers, data))
        return self.responses.pop(0)


def test_build_url():
    assert build_url("https://h/p", {"pair": "B-BTC_USDT", "depth": 50}) == "https://h/p?pair=B-BTC_USDT&depth=50"
    assert build_url("https://h/p?a=1", {"b": [1, 2]}) == "https://h/p?a=1&b=1&b=2"


def test_client_capture_redacts_secrets(tmp_path):
    sess = FakeSession([FakeResp(200, b'{"ok":1}', {"Content-Type": "application/json"})])
    c = HttpClient(capture_dir=tmp_path, session=sess)
    r = c.request("GET", "https://api.coindcx.com/x", headers={"X-AUTH-APIKEY": "k3y-val", "X-AUTH-SIGNATURE": "s1g-val"},
                  body=b"{}")
    assert r.status == 200 and r.body == b'{"ok":1}' and r.ok
    assert sess.calls[0][2]["X-AUTH-APIKEY"] == "k3y-val"          # sent for real
    meta_files = list(tmp_path.glob("*.meta.json"))
    assert len(meta_files) == 1
    text = meta_files[0].read_text()
    assert "k3y-val" not in text and "s1g-val" not in text
    assert json.loads(text)["request_headers"] == {"X-AUTH-APIKEY": "<redacted>", "X-AUTH-SIGNATURE": "<redacted>"}
    assert list(tmp_path.glob("*.body"))[0].read_bytes() == b'{"ok":1}'


def test_request_with_retry_backs_off_then_succeeds():
    sess = FakeSession([FakeResp(503, b""), FakeResp(429, b"", {"Retry-After": "5"}), FakeResp(200, b"[]")])
    slept = []
    c = HttpClient(session=sess, sleep=slept.append, default_rate=1000)
    r = c.request_with_retry("GET", "https://public.coindcx.com/x")
    assert r.status == 200
    assert slept == [30.0, 5.0]    # 503 attempt 1 -> 30s; 429 attempt 2 -> max(2, Retry-After 5)


def test_request_with_retry_does_not_retry_4xx():
    sess = FakeSession([FakeResp(422, b'{"message":"Depth is not allowed"}')])
    c = HttpClient(session=sess, sleep=lambda d: pytest.fail("slept"), default_rate=1000)
    assert c.request_with_retry("GET", "https://api.coindcx.com/x").status == 422


def test_redact_headers_case_insensitive():
    assert redact_headers({"x-auth-apikey": "a", "Accept": "b"}) == {"x-auth-apikey": "<redacted>", "Accept": "b"}
