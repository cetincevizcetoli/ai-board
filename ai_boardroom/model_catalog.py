"""Public model metadata only. No credentials, generation or billing requests."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json

from .config import CONFIG_DIR
from .session_log import atomic_write

CATALOG_FILE = CONFIG_DIR / "openrouter_models.json"
CATALOG_VERSION = 1


def is_free(entry):
    try:
        prices = entry.get("pricing", {})
        return all(Decimal(str(prices[field])) == 0 for field in ("prompt", "completion")) and all(
            Decimal(str(prices.get(field, "0"))) == 0 for field in ("request", "image", "web_search"))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return False


def normalize_catalog(data):
    result = []
    for model in data.get("data", []):
        if not isinstance(model.get("id"), str) or not model["id"]:
            continue
        architecture = model.get("architecture") or {}
        # Audio/image generators can advertise text as a secondary output.
        # A text chat request is only valid for text-output endpoints.
        if architecture.get("output_modalities") != ["text"]:
            continue
        if "text" not in (architecture.get("input_modalities") or []):
            continue
        entry = {"provider": "openrouter", "id": model["id"], "name": model.get("name") or model["id"],
                 "pricing": model.get("pricing") or {}, "context_length": model.get("context_length"),
                 "source": "catalog"}
        entry["tier"] = "free" if is_free(entry) else "paid"
        result.append(entry)
    return result


def fetch_catalog():
    import requests
    response = requests.get("https://openrouter.ai/api/v1/models", params={"output_modalities": "text"}, timeout=25)
    response.raise_for_status()
    models = normalize_catalog(response.json())
    if not models:
        raise ValueError("Model kataloğu boş")
    return {"version": CATALOG_VERSION, "checked_at": datetime.now(timezone.utc).isoformat(), "models": models}


def cached_catalog():
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == CATALOG_VERSION and isinstance(data.get("models"), list):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"checked_at": "", "models": []}


def save_catalog(data):
    CATALOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(CATALOG_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def merged_models(catalog, custom):
    entries = {(m["provider"], m["id"]): dict(m) for m in catalog.get("models", [])}
    for item in custom:
        key = (item.get("provider", "openrouter"), item.get("id", ""))
        if not key[1]:
            continue
        # Custom labels cannot change authoritative prices or make a paid model free.
        existing = entries.get(key)
        if existing:
            existing.update(name=item.get("name") or existing["name"], custom=True)
        elif key[0] != "openrouter":
            entries[key] = {**item, "source": "custom", "tier": "unverified", "custom": True}
    return sorted(entries.values(), key=lambda m: (m["tier"] != "free", m.get("name", m["id"]).casefold()))


def price_label(entry):
    if entry.get("tier") == "free":
        return "Ücretsiz (katalog)"
    try:
        prices = entry["pricing"]
        prompt = Decimal(str(prices["prompt"])) * 1000000
        completion = Decimal(str(prices["completion"])) * 1000000
        return f"1M token: giriş ${prompt:g} / çıkış ${completion:g}"
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return "Fiyat doğrulanmadı"
