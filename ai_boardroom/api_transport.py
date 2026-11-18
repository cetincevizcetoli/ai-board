"""One isolated SDK request at a time, with a wall-clock deadline and cancellation."""
import multiprocessing
import time
from types import SimpleNamespace


class ResponseDeadlineExceeded(TimeoutError):
    pass


class ModelSkipped(RuntimeError):
    pass


class RequestCancelled(RuntimeError):
    pass


class TransportCleanupError(RuntimeError):
    pass


class RemoteCallError(RuntimeError):
    def __init__(self, error):
        super().__init__("Sağlayıcı isteği başarısız: " + error.get("kind", "failure"))
        self.routing_kind = error.get("kind", "failure")
        self.remote_error_type = error.get("type", "RemoteCallError")
        self.remote_limit_source = error.get("limit_source")
        self.status_code = error.get("http_status")
        self.response = SimpleNamespace(headers={"Retry-After": str(error.get("retry_after", 0))})


def call_provider(provider, model, prompt, key, system_prompt=None, mode="free", **unused):
    """Pure SDK adapter. Credentials never enter logs/files or the return value."""
    client = None
    try:
        if provider == "gemini":
            from google import genai
            client = genai.Client(api_key=key, http_options={"timeout": 60000})
            response = client.models.generate_content(model=model, contents=prompt,
                config={"system_instruction": system_prompt} if system_prompt else {})
            usage = getattr(response, "usage_metadata", None)
            return {"text": response.text, "actual_model": getattr(response, "model_version", None),
                    "prompt_tokens": getattr(usage, "prompt_token_count", None),
                    "completion_tokens": getattr(usage, "candidates_token_count", None), "cost": None}
        if provider == "anthropic":
            from anthropic import Anthropic
            client = Anthropic(api_key=key, timeout=60.0, max_retries=0)
            options = {"model": model, "max_tokens": 1024,
                       "messages": [{"role": "user", "content": prompt}]}
            if system_prompt:
                options["system"] = system_prompt
            response = client.messages.create(**options)
            usage = getattr(response, "usage", None)
            return {"text": response.content[0].text, "actual_model": getattr(response, "model", None),
                    "prompt_tokens": getattr(usage, "input_tokens", None),
                    "completion_tokens": getattr(usage, "output_tokens", None), "cost": None}
        if provider in ("openai", "groq", "openrouter"):
            from openai import OpenAI
            urls = {"openai": None, "groq": "https://api.groq.com/openai/v1", "openrouter": "https://openrouter.ai/api/v1"}
            client = OpenAI(api_key=key, base_url=urls[provider], timeout=60.0, max_retries=0)
            messages = ([{"role": "system", "content": system_prompt}] if system_prompt else [])
            messages.append({"role": "user", "content": prompt})
            options = {}
            if provider == "openrouter" and mode == "free":
                options["extra_body"] = {"provider": {"max_price": {"prompt": 0, "completion": 0}}}
            response = client.chat.completions.create(model=model, messages=messages, **options)
            usage = getattr(response, "usage", None)
            return {"text": response.choices[0].message.content, "actual_model": getattr(response, "model", None),
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "cost": getattr(usage, "cost", None),
                    "finish_reason": getattr(response.choices[0], "finish_reason", None)}
        raise ValueError("Bilinmeyen sağlayıcı")
    finally:
        close = getattr(client, "close", None)
        if close:
            close()


def _child_entry(connection, payload):
    try:
        result = call_provider(**payload)
        message = {"ok": True, "result": result}
    except Exception as exc:
        from .routing import error_kind, limit_source, retry_after_seconds
        # No raw error body/message or original request is sent to the parent.
        message = {"ok": False, "error": {"kind": error_kind(exc), "type": type(exc).__name__,
                   "http_status": getattr(exc, "status_code", None), "limit_source": limit_source(exc),
                   "retry_after": retry_after_seconds(exc)}}
    try:
        connection.send(message)
    except (OSError, EOFError):
        pass  # The parent may have timed out or skipped this attempt.
    finally:
        connection.close()


def _stop_child(process):
    if process.pid is None:
        return
    if process.is_alive():
        process.terminate()
    process.join(1.0)
    if process.is_alive():
        process.kill()
        process.join(1.0)
    if process.is_alive():
        raise TransportCleanupError("İstek işlemi sonlandırılamadı; yeni istek gönderilmedi.")
    process.close()


def run_request(provider, model, prompt, key, system_prompt=None, mode="free", timeout_seconds=60,
                cancel_event=None, skip_event=None, _target=None):
    """Deadline includes startup/import/connect/read, even while a server sends keepalive bytes.

    A child is joined/killed before any response, timeout or cancellation reaches the caller.
    No detached worker can deliver a late response or accumulate behind later attempts.
    """
    def cancelled():
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled()
        if skip_event is not None and skip_event.is_set():
            raise ModelSkipped("Kullanıcı diğer modeli seçti")

    cancelled()
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    payload = {"provider": provider, "model": model, "prompt": prompt, "key": key,
               "system_prompt": system_prompt, "mode": mode}
    process = context.Process(target=_target or _child_entry, args=(sender, payload), daemon=True)
    deadline = time.monotonic() + float(timeout_seconds)
    message = None
    try:
        process.start()
        sender.close()
        while True:
            cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ResponseDeadlineExceeded("Modelin toplam yanıt süresi doldu")
            if receiver.poll(min(0.05, remaining)):
                try:
                    message = receiver.recv()
                except EOFError:
                    raise RemoteCallError({"kind": "failure", "type": "ChildProcessError"}) from None
                cancelled()
                if time.monotonic() >= deadline:
                    raise ResponseDeadlineExceeded("Modelin toplam yanıt süresi doldu")
                break
            if not process.is_alive():
                raise RemoteCallError({"kind": "failure", "type": "ChildProcessError"})
    finally:
        receiver.close()
        sender.close()
        _stop_child(process)
    if not isinstance(message, dict) or "ok" not in message:
        raise RemoteCallError({"kind": "failure", "type": "InvalidTransportReply"})
    if not message["ok"]:
        raise RemoteCallError(message["error"])
    return message["result"]
