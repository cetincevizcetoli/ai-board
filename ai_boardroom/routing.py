"""Layer policy, shared pacing and safe quota classification."""
from datetime import datetime, timezone
from copy import deepcopy
from email.utils import parsedate_to_datetime
import hashlib
import math
import threading
import time

from .config import OPENROUTER_MODELS
from .model_catalog import fetch_catalog, is_free

DEFAULT_ROUTING = {
    "mode": "free", "paid_enabled": False, "automatic_fallback": True,
    "free_interval_seconds": 6, "paid_interval_seconds": 3, "max_retries": 1,
    "backoff_seconds": 20, "max_fallbacks": 3,
    "free_fallbacks": list(OPENROUTER_MODELS), "paid_fallbacks": [],
    "free_catalog_fallback": True, "free_max_fallbacks": 0, "free_retry_same_model": False,
    "response_timeout_seconds": 60,
}


class PolicyError(RuntimeError):
    pass


class RoutesExhausted(RuntimeError):
    pass


class DailyQuotaExhausted(RuntimeError):
    pass


class EmptyResponseError(RuntimeError):
    pass


class MetaOnlyResponseError(EmptyResponseError):
    pass


def fetch_free_quota(api_key):
    """Read only counters; never return account labels, credentials or balances."""
    import requests
    response = requests.get("https://openrouter.ai/api/v1/key",
                            headers={"Authorization": "Bearer " + api_key}, timeout=15)
    response.raise_for_status()
    counters = response.json().get("data", {}).get("free_model_daily_requests")
    if not isinstance(counters, dict):
        return None
    return {name: counters[name] for name in ("used", "limit", "remaining")
            if isinstance(counters.get(name), (int, float)) and not isinstance(counters[name], bool)}


LIMIT_SOURCES = {
    "openrouter_free_tier_daily": "daily_quota",
    "openrouter_free_tier_per_minute": "account_rate_limit",
    "upstream_provider_shared_pool": "upstream_rate_limit",
    "openrouter_credits": "configuration",
    "openrouter_key_limit": "configuration",
}


def limit_source(error):
    source = getattr(error, "remote_limit_source", None)
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        payload = body.get("error", body)
        metadata = payload.get("metadata") if isinstance(payload, dict) else None
        if isinstance(metadata, dict):
            source = metadata.get("limit_source")
    return source if isinstance(source, str) and source in LIMIT_SOURCES else None


def error_kind(error):
    remote_kind = getattr(error, "routing_kind", None)
    if remote_kind in {"daily_quota", "account_rate_limit", "upstream_rate_limit", "rate_limit", "configuration", "model_unavailable", "temporary", "failure"}:
        return remote_kind
    source = limit_source(error)
    if source:
        return LIMIT_SOURCES[source]
    if type(error).__name__ == "ResponseDeadlineExceeded":
        return "response_timeout"
    if type(error).__name__ == "ModelSkipped":
        return "user_skip"
    if isinstance(error, EmptyResponseError):
        return "empty_response"
    status = getattr(error, "status_code", None)
    # Only classify internally; never store or display raw exception strings/bodies.
    text = str(error).lower()
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        text += " " + str(body).lower()
    if "free-models-per-day" in text or "free_models_per_day" in text or ("daily quota" in text and "provider returned error" not in text):
        return "daily_quota"
    if "free-models-per-min" in text or "free_models_per_min" in text:
        return "account_rate_limit"
    if status == 429 and "rate-limited upstream" in text:
        return "upstream_rate_limit"
    if status == 429 or any(word in text for word in ("429", "rate limit", "ratelimit", "quota", "resource_exhausted")):
        return "rate_limit"
    if status in (401, 402, 403):
        return "configuration"
    if status in (400, 404):
        return "model_unavailable"
    return "temporary" if status in (408, 500, 502, 503, 504) or "timeout" in type(error).__name__.lower() else "failure"


def retry_after_seconds(error, now=None):
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    current_time = time.time() if now is None else now
    delay = 0.0
    try:
        if value is not None and math.isfinite(float(value)):
            delay = max(0.0, float(value))
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            delay = max(0.0, parsed.timestamp() - current_time)
        except (ValueError, TypeError, OverflowError):
            pass
    if error_kind(error) == "account_rate_limit":
        reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
        try:
            reset_ms = float(reset)
            if math.isfinite(reset_ms):
                delay = max(delay, reset_ms / 1000 - current_time)
        except (ValueError, TypeError):
            pass
    return delay


class RequestPacer:
    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._last = {}
        self._cooldown = {}
        self._daily = {}

    @staticmethod
    def key(provider, api_key):
        return provider + ":" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    def acquire(self, key, interval, wait):
        while True:
            with self._lock:
                delay = max(self._last.get(key, float("-inf")) + interval, self._cooldown.get(key, 0)) - self.clock()
                if delay <= 0:
                    self._last[key] = self.clock()
                    return
            wait(delay)

    def cooldown(self, key, seconds):
        with self._lock:
            self._cooldown[key] = max(self._cooldown.get(key, 0), self.clock() + seconds)

    def block_daily(self, key):
        with self._lock:
            self._daily[key] = datetime.now(timezone.utc).date()

    def daily_blocked(self, key):
        with self._lock:
            return self._daily.get(key) == datetime.now(timezone.utc).date()


PACER = RequestPacer()


class RoutePolicy:
    def __init__(self, config=None, catalog_loader=None):
        self.config = deepcopy({**DEFAULT_ROUTING, **(config or {})})
        self.catalog_loader = catalog_loader
        self.catalog = None
        self.all_catalog = {}
        self.catalog_checked_at = ""

    def chat_catalog(self):
        if self.catalog is None:
            # Fresh public metadata verifies chat support and free prices.
            try:
                data = (self.catalog_loader or fetch_catalog)()
                self.all_catalog = {m["id"]: m for m in data["models"] if m.get("provider") == "openrouter"}
                self.catalog_checked_at = data.get("checked_at", "")
                self.catalog = {m["id"]: m for m in data["models"] if m.get("provider") == "openrouter" and is_free(m)}
            except Exception:
                raise PolicyError("Güncel OpenRouter sohbet kataloğu doğrulanamadı.") from None
        return self.all_catalog

    def free_catalog(self):
        self.chat_catalog()
        return self.catalog

    def candidates(self, provider, model, keys, excluded=()):
        primary = {"provider": provider, "model": model}
        mode = self.config["mode"]
        paid_chat_models = {}
        if mode == "free":
            if provider != "openrouter":
                raise PolicyError("Ücretsiz mod yalnızca fiyatı doğrulanmış OpenRouter modellerini kullanır.")
            allowed = self.free_catalog()
            if model not in allowed:
                if model in self.all_catalog or not self.config.get("automatic_fallback"):
                    raise PolicyError("Seçilen model ücretsiz katalogda doğrulanmadı; çağrı engellendi.")
                primary = None  # Removed/unknown ID: do not call it; try verified free alternatives only.
            fallback = [{"provider": "openrouter", "model": mid} for mid in self.config["free_fallbacks"] if mid in allowed]
            if self.config.get("free_catalog_fallback", True):
                fallback.extend({"provider": "openrouter", "model": mid} for mid in sorted(allowed))
        elif mode == "paid":
            if not self.config.get("paid_enabled"):
                raise PolicyError("Ücretli katman açıkça etkinleştirilmedi.")
            fallback = [dict(item) for item in self.config.get("paid_fallbacks", [])]
            if provider == "openrouter":
                paid_chat_models = self.chat_catalog()
                if model not in paid_chat_models:
                    raise PolicyError("Seçilen OpenRouter modeli metin sohbet kataloğunda bulunamadı.")
            elif self.config.get("automatic_fallback") and any(item.get("provider") == "openrouter" for item in fallback):
                try:
                    paid_chat_models = self.chat_catalog()
                except PolicyError:
                    pass  # Native providers can still answer if OpenRouter's catalog is down.
        else:
            raise PolicyError("Geçersiz kullanım katmanı.")
        if not keys.get(provider):
            raise PolicyError("Seçilen katmanda sağlayıcı anahtarı girilmedi.")
        if (provider, model) in excluded:
            primary = None
        if mode == "free":
            configured = max(0, min(100, int(self.config.get("free_max_fallbacks", 0))))
            limit = configured or len({(item["provider"], item["model"]) for item in fallback})
        else:
            limit = max(0, min(5, int(self.config["max_fallbacks"])))
        if not self.config.get("automatic_fallback"):
            limit = 0
        maximum = (1 if primary else 0) + limit
        if maximum == 0:
            return []
        result, seen = [], set()
        for item in ([primary] if primary else []) + (fallback if self.config.get("automatic_fallback") else []):
            identity = (item.get("provider"), item.get("model"))
            if identity in seen or identity in excluded or not all(identity) or not keys.get(identity[0]):
                continue
            if mode == "free" and (identity[0] != "openrouter" or identity[1] not in allowed):
                continue
            if mode == "paid" and identity[0] == "openrouter" and identity[1] not in paid_chat_models:
                continue
            seen.add(identity)
            result.append(item)
            if len(result) >= maximum:
                break
        return result
