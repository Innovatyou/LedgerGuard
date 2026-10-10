from contextvars import ContextVar

_request_ip: ContextVar[str | None] = ContextVar("request_ip", default=None)
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_context(ip: str | None, request_id: str | None):
    return _request_ip.set(ip), _request_id.set(request_id)


def reset_request_context(tokens):
    _request_ip.reset(tokens[0])
    _request_id.reset(tokens[1])


def current_ip() -> str | None:
    return _request_ip.get()


def current_request_id() -> str | None:
    return _request_id.get()
