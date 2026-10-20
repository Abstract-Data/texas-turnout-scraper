"""
Custom transport for Pyodide on Emscripten.

In sync mode it relies on use of the Javascript Promise Integration
feature which is currently experimental in webassembly and only works
in some places. Specifically in chrome JSPI is behind either a flag or an
origin trial, in node 20 or newer you need the --experimental-wasm-stack-switching
flag. Firefox is not currently supported.

See https://github.com/WebAssembly/js-promise-integration/

In async mode it uses the standard fetch api, which should work
anywhere that pyodide works.
"""

from __future__ import annotations

import email.parser
import typing
from contextlib import contextmanager
from types import TracebackType
from typing import Any, TypeVar

import js
from pyodide.ffi import JsException, JsProxy, can_run_sync, run_sync, to_js

if typing.TYPE_CHECKING:
    import ssl  # pragma: nocover

from .._config import DEFAULT_LIMITS, Limits
from .._exceptions import (
    ConnectError,
    ConnectTimeout,
    ReadError,
    ReadTimeout,
    RequestError,
)
from .._models import Request, Response
from .._types import AsyncByteStream, CertTypes, ProxyTypes, SyncByteStream
from .base import AsyncBaseTransport, BaseTransport

T = TypeVar("T", bound="JavascriptFetchTransport")
A = TypeVar("A", bound="AsyncJavascriptFetchTransport")

SOCKET_OPTION = (
    tuple[int, int, int]
    | tuple[int, int, typing.Union[bytes, bytearray]]
    | tuple[int, int, None, int]
)

__all__ = ["AsyncJavascriptFetchTransport", "JavascriptFetchTransport"]

"""
There are some headers that trigger unintended CORS preflight requests.
See also https://github.com/koenvo/pyodide-http/issues/22
"""
HEADERS_TO_IGNORE = ("user-agent",)


@contextmanager
def _timeout(
    timeout: float,
    abort_controller_js: JsProxy,
    TimeoutExceptionType: type[RequestError],
    ErrorExceptionType: type[RequestError],
):
    timer_id = None
    if timeout > 0:
        # It looks odd that we have to call bind() here since the JsProxy will
        # automatically remember the receiver. But when we pass it back to
        # JavaScript, we unwrap it and forget the receiver.
        abort = abort_controller_js.abort.bind(abort_controller_js)
        timer_id = js.setTimeout(abort, int(timeout * 1000))
    try:
        yield
    except JsException as err:
        if err.name == "AbortError":
            raise TimeoutExceptionType(message="Request timed out")
            timer_id = None
        else:
            raise ErrorExceptionType(message=err.message)
    finally:
        if timer_id is not None:
            js.clearTimeout(timer_id)


def _run_sync_with_timeout(
    promise: typing.Awaitable[JsProxy],
    timeout: float,
    abort_controller_js: JsProxy,
    TimeoutExceptionType: type[RequestError],
    ErrorExceptionType: type[RequestError],
) -> JsProxy:
    """await a javascript promise synchronously with a timeout set via the
       AbortController and return the resulting javascript proxy

    Args:
        promise (Awaitable): Javascript promise to await
        timeout (float): Timeout in seconds
        abort_controller_js (Any): A javascript AbortController object, used on timeout
        TimeoutExceptionType (type[Exception]): An exception type to raise on timeout
        ErrorExceptionType (type[Exception]): An exception type to raise on error

    Raises:
        TimeoutExceptionType: If the request times out
        ErrorExceptionType: If the request raises a Javascript exception

    Returns:
        _type_: The result of awaiting the promise.
    """
    with _timeout(
        timeout, abort_controller_js, TimeoutExceptionType, ErrorExceptionType
    ):
        # run_sync here uses WebAssembly Javascript Promise Integration to
        # suspend python until the Javascript promise resolves.
        return run_sync(promise)


async def _run_async_with_timeout(
    promise: typing.Awaitable[JsProxy],
    timeout: float,
    abort_controller_js: JsProxy,
    TimeoutExceptionType: type[RequestError],
    ErrorExceptionType: type[RequestError],
) -> JsProxy:
    """await a javascript promise asynchronously with a timeout set via the
       AbortController

    Args:
        promise (Awaitable): Javascript promise to await
        timeout (float): Timeout in seconds
        abort_controller_js (Any): A javascript AbortController object, used on timeout
        TimeoutExceptionType (type[Exception]): An exception type to raise on timeout
        ErrorExceptionType (type[Exception]): An exception type to raise on error

    Raises:
        TimeoutException: If the request times out
        NetworkError: If the request raises a Javascript exception

    Returns:
        _type_: The result of awaiting the promise.
    """
    with _timeout(
        timeout, abort_controller_js, TimeoutExceptionType, ErrorExceptionType
    ):
        return await promise


def _compute_timeouts(extensions: dict[str, Any]) -> tuple[float, float]:
    timeout_dict = extensions.get("timeout", {}) or {}
    conn_timeout = timeout_dict.get("connect", 0.0) or 0.0
    read_timeout = timeout_dict.get("read", 0.0) or 0.0
    return [conn_timeout, read_timeout]


def _do_fetch(request: Request, request_body: bytes, abort_controller_js: Any):
    headers = {k: v for k, v in request.headers.items() if k not in HEADERS_TO_IGNORE}
    fetch_data = {
        "headers": headers,
        "body": to_js(request_body),
        "method": request.method,
        "signal": abort_controller_js.signal,
    }

    return js.fetch(
        request.url,
        to_js(fetch_data, dict_converter=js.Object.fromEntries),
    )


def _js_response_to_python(
    Stream: "type[EmscriptenStream] | type[AsyncEmscriptenStream]",
    response_js: Any,
    read_timeout: float,
    abort_controller_js: Any,
) -> Response:
    headers = dict(response_js.headers.entries())
    # fix content-encoding headers because the javascript fetch handles that
    headers["content-encoding"] = "identity"
    status_code = response_js.status

    # get a reader from the fetch response
    body_stream_js = response_js.body.getReader()
    return Response(
        status_code=status_code,
        headers=headers,
        stream=Stream(body_stream_js, read_timeout, abort_controller_js),
    )


class EmscriptenStream(SyncByteStream):
    def __init__(
        self,
        response_stream_js: JsProxy,
        timeout: float,
        abort_controller_js: JsProxy,
    ) -> None:
        self._stream_js = response_stream_js
        self.timeout = timeout
        self.abort_controller_js = abort_controller_js

    def __iter__(self) -> typing.Iterator[bytes]:
        while True:
            result_js = _run_sync_with_timeout(
                self._stream_js.read(),
                self.timeout,
                self.abort_controller_js,
                ReadTimeout,
                ReadError,
            )
            if result_js.done:
                return
            else:
                this_buffer = result_js.value.to_py()
                yield this_buffer

    def close(self) -> None:
        self._stream_js = None


class JavascriptFetchTransport(BaseTransport):
    def __init__(
        self,
        verify: ssl.SSLContext | str | bool = True,
        cert: CertTypes | None = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: Limits = DEFAULT_LIMITS,
        proxy: ProxyTypes | None = None,
        uds: str | None = None,
        local_address: str | None = None,
        retries: int = 0,
        socket_options: typing.Iterable[SOCKET_OPTION] | None = None,
    ) -> None:
        pass

    def __enter__(self: T) -> T:  # Use generics for subclass support.
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        pass

    def handle_request(
        self,
        request: Request,
    ) -> Response:
        assert isinstance(request.stream, SyncByteStream)
        if not can_run_sync():
            return _no_jspi_fallback(request)
        request_body: bytes | None = b"".join(request.stream) or None

        conn_timeout, read_timeout = _compute_timeouts(request.extensions)
        abort_controller_js = js.AbortController.new()
        fetcher_promise_js = _do_fetch(request, request_body, abort_controller_js)
        response_js = _run_sync_with_timeout(
            fetcher_promise_js,
            conn_timeout,
            abort_controller_js,
            ConnectTimeout,
            ConnectError,
        )
        return _js_response_to_python(
            EmscriptenStream, response_js, read_timeout, abort_controller_js
        )

    def close(self) -> None:
        pass  # pragma: nocover


class AsyncEmscriptenStream(AsyncByteStream):
    def __init__(
        self,
        response_stream_js: JsProxy,
        timeout: float,
        abort_controller_js: JsProxy,
    ) -> None:
        self._stream_js = response_stream_js
        self.timeout = timeout
        self.abort_controller_js = abort_controller_js

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        while self._stream_js is not None:
            result_js = await _run_async_with_timeout(
                self._stream_js.read(),
                self.timeout,
                self.abort_controller_js,
                ReadTimeout,
                ReadError,
            )
            if result_js.done:
                return
            else:
                this_buffer = result_js.value.to_py()
                yield this_buffer

    async def aclose(self) -> None:
        self._stream_js = None


class AsyncJavascriptFetchTransport(AsyncBaseTransport):
    def __init__(
        self,
        verify: ssl.SSLContext | str | bool = True,
        cert: CertTypes | None = None,
        trust_env: bool = True,
        http1: bool = True,
        http2: bool = False,
        limits: Limits = DEFAULT_LIMITS,
        proxy: ProxyTypes | None = None,
        uds: str | None = None,
        local_address: str | None = None,
        retries: int = 0,
        socket_options: typing.Iterable[SOCKET_OPTION] | None = None,
    ) -> None:
        pass

    async def __aenter__(self: A) -> A:  # Use generics for subclass support.
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        pass

    async def handle_async_request(
        self,
        request: Request,
    ) -> Response:
        assert isinstance(request.stream, AsyncByteStream)
        request_body: bytes = b""
        async for x in request.stream:
            request_body += x
        if not request_body:
            request_body = None

        conn_timeout, read_timeout = _compute_timeouts(request.extensions)
        abort_controller_js = js.AbortController.new()
        fetcher_promise_js = _do_fetch(request, request_body, abort_controller_js)
        response_js = await _run_async_with_timeout(
            fetcher_promise_js,
            conn_timeout,
            abort_controller_js,
            ConnectTimeout,
            ConnectError,
        )
        return _js_response_to_python(
            AsyncEmscriptenStream, response_js, read_timeout, abort_controller_js
        )

    async def aclose(self) -> None:
        pass  # pragma: nocover


# Use XHR to do a sync request without jspi
def _is_in_browser_main_thread() -> bool:
    return hasattr(js, "window") and hasattr(js, "self") and js.self == js.window


def _no_jspi_fallback(request: Request) -> Response:
    assert isinstance(request.stream, SyncByteStream)
    try:
        js_xhr = js.XMLHttpRequest.new()

        req_body: bytes | None = b"".join(request.stream)
        if req_body is not None and len(req_body) == 0:
            req_body = None
        _, timeout = _compute_timeouts(request.extensions)

        # XHMLHttpRequest only supports timeouts and proper
        # binary file reading in web-workers
        if not _is_in_browser_main_thread():
            js_xhr.responseType = "arraybuffer"
            if timeout > 0.0:
                js_xhr.timeout = int(timeout * 1000)
        else:
            # this is a nasty hack to be able to read binary files on
            # main browser thread using xmlhttprequest
            js_xhr.overrideMimeType("text/plain; charset=ISO-8859-15")

        js_xhr.open(request.method, request.url, False)

        for name, value in request.headers.items():
            if name.lower() not in HEADERS_TO_IGNORE:
                js_xhr.setRequestHeader(name, value)

        js_xhr.send(to_js(req_body))

        headers = dict(email.parser.Parser().parsestr(js_xhr.getAllResponseHeaders()))

        if not _is_in_browser_main_thread():
            body = js_xhr.response.to_py().tobytes()
        else:
            body = js_xhr.response.encode("ISO-8859-15")

        return Response(status_code=js_xhr.status, headers=headers, content=body)
    except JsException as err:
        if err.name == "TimeoutError":
            raise ConnectTimeout(message="Request timed out")
        else:
            raise ConnectError(message=err.message)
