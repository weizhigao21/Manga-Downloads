"""src/net.py 重试逻辑测试（不依赖真实网络）。

覆盖：可重试异常判定（超时/连接错误/限流状态码）、连接错误重试成功、
不可重试异常（4xx）不重试、重试耗尽抛 RuntimeError。
"""

import pytest
import requests

from src.net import _should_retry, fetch_page_with_cookies, fetch_with_retry


@pytest.fixture
def fast_net(monkeypatch):
    """禁用真实等待（节流与退避睡眠），加速重试测试。"""
    monkeypatch.setattr("src.net.time.sleep", lambda _: None)
    monkeypatch.setattr("src.net._throttle", lambda: None)


def _ok_response(text: str) -> requests.Response:
    resp = requests.Response()
    resp.status_code = 200
    resp._content = text.encode("utf-8")
    return resp


def _status_response(status: int) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status
    resp._content = b"error"
    return resp


class TestShouldRetry:
    def test_timeout_is_retryable(self):
        assert _should_retry(requests.Timeout("slow"))

    def test_connection_error_is_retryable(self):
        assert _should_retry(requests.ConnectionError("refused"))

    def test_ratelimit_statuses_retryable(self):
        for status in (429, 403, 500, 502, 503):
            assert _should_retry(requests.HTTPError(response=_status_response(status)))

    def test_client_error_not_retryable(self):
        for status in (400, 404, 405):
            assert not _should_retry(requests.HTTPError(response=_status_response(status)))

    def test_non_request_exception_not_retryable(self):
        assert not _should_retry(ValueError("boom"))


class TestFetchWithRetry:
    def test_retries_connection_error_then_succeeds(self, monkeypatch, fast_net):
        calls: list[str] = []

        def fake_get(url, headers, timeout):
            calls.append(url)
            if len(calls) < 3:
                raise requests.ConnectionError("boom")
            return _ok_response("<html>ok</html>")

        monkeypatch.setattr("src.net._SESSION.get", fake_get)
        result = fetch_with_retry("http://x.example/", {}, retries=3)
        assert "ok" in result
        assert len(calls) == 3  # 失败 2 次 + 成功 1 次

    def test_retries_timeout_then_succeeds(self, monkeypatch, fast_net):
        calls: list[str] = []

        def fake_get(url, headers, timeout):
            calls.append(url)
            if len(calls) < 2:
                raise requests.Timeout("slow")
            return _ok_response("<html>ok</html>")

        monkeypatch.setattr("src.net._SESSION.get", fake_get)
        assert "ok" in fetch_with_retry("http://x.example/", {}, retries=3)
        assert len(calls) == 2

    def test_no_retry_on_404(self, monkeypatch, fast_net):
        calls: list[str] = []

        def fake_get(url, headers, timeout):
            calls.append(url)
            return _status_response(404)

        monkeypatch.setattr("src.net._SESSION.get", fake_get)
        with pytest.raises(requests.HTTPError):
            fetch_with_retry("http://x.example/", {}, retries=3)
        assert len(calls) == 1  # 4xx 不重试

    def test_raises_after_retries_exhausted(self, monkeypatch, fast_net):
        def fake_get(url, headers, timeout):
            raise requests.ConnectionError("boom")

        monkeypatch.setattr("src.net._SESSION.get", fake_get)
        with pytest.raises(RuntimeError, match="重试"):
            fetch_with_retry("http://x.example/", {}, retries=2)

    def test_ratelimit_429_retried(self, monkeypatch, fast_net):
        calls: list[str] = []

        def fake_get(url, headers, timeout):
            calls.append(url)
            if len(calls) < 2:
                return _status_response(429)
            return _ok_response("<html>ok</html>")

        monkeypatch.setattr("src.net._SESSION.get", fake_get)
        assert "ok" in fetch_with_retry("http://x.example/", {}, retries=3)
        assert len(calls) == 2


class TestFetchPageWithCookies:
    def test_retries_connection_error(self, monkeypatch, fast_net):
        calls: list[str] = []

        def fake_get(url, headers, timeout):
            calls.append(url)
            if len(calls) < 2:
                raise requests.ConnectionError("boom")
            return _ok_response("<html>c</html>")

        monkeypatch.setattr("src.net._SESSION.get", fake_get)
        html_text, cookies = fetch_page_with_cookies("http://x.example/", {}, retries=3)
        assert "c" in html_text
        assert cookies == ""  # 空 cookie
        assert len(calls) == 2
