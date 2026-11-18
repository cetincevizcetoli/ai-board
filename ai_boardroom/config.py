import json
from copy import deepcopy
from datetime import datetime
import shutil
from pathlib import Path

CONFIG_DIR = Path.home() / ".yz_panel"
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSIONS_DIR = CONFIG_DIR / "sessions"

# 2026-11-17: catalog membership and real short responses verified.
OPENROUTER_MODELS = (
    "inclusionai/ling-3.2-flash:free",
    "nvidia/nemotron-3.5-lightning:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
)

DEFAULT_CONFIG = {
    "api_keys": {
        "anthropic": "",
        "openai": "",
        "gemini": "",
        "groq": "",
        "openrouter": ""
    },
    "rounds": 2,
    "summary_provider": "openrouter",
    "summary_model": "inclusionai/ling-3.2-flash:free",
    "personas": [
        {
            "name": "Mimar",
            "role": "Sistem mimarı, performans ve veritabanı uzmanı. Sağlam altyapı tasarlar.",
            "provider": "openrouter",
            "model": "inclusionai/ling-3.2-flash:free"
        },
        {
            "name": "Eleştirmen",
            "role": "Kritik yaklaşım sergileyen, güvenlik açıklarını ve mantık hatalarını yakalayan uzman.",
            "provider": "openrouter",
            "model": "nvidia/nemotron-3-ultra-550b-a55b:free"
        },
        {
            "name": "Ürün-UX",
            "role": "Kullanıcı deneyimi, akış hızı ve işlevsellik odaklı ürün yöneticisi.",
            "provider": "openrouter",
            "model": "inclusionai/ling-3.2-flash:free"
        }
    ]
}


def load_config():
    from .routing import DEFAULT_ROUTING
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        data = deepcopy(DEFAULT_CONFIG)
        data.update(routing=deepcopy(DEFAULT_ROUTING), paid_api_keys={p: "" for p in DEFAULT_CONFIG["api_keys"]}, custom_models=[])
        save_config(data)
        return data
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "api_keys" not in data:
            data["api_keys"] = deepcopy(DEFAULT_CONFIG["api_keys"])
        else:
            for k in DEFAULT_CONFIG["api_keys"]:
                if k not in data["api_keys"]:
                    data["api_keys"][k] = ""
        if "rounds" not in data:
            data["rounds"] = 2
        if "summary_provider" not in data:
            data["summary_provider"] = DEFAULT_CONFIG["summary_provider"]
        if "summary_model" not in data:
            data["summary_model"] = DEFAULT_CONFIG["summary_model"]
        if "personas" not in data:
            data["personas"] = deepcopy(DEFAULT_CONFIG["personas"])
        data["routing"] = {**deepcopy(DEFAULT_ROUTING), **data.get("routing", {})}
        data.setdefault("paid_api_keys", {p: "" for p in DEFAULT_CONFIG["api_keys"]})
        data.setdefault("custom_models", [])
        return data
    except Exception:
        data = deepcopy(DEFAULT_CONFIG)
        data.update(routing=deepcopy(DEFAULT_ROUTING), paid_api_keys={p: "" for p in DEFAULT_CONFIG["api_keys"]}, custom_models=[])
        return data


def save_config(cfg):
    from .session_log import atomic_write
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        backup_dir = CONFIG_DIR / "backups"
        backup_dir.mkdir(exist_ok=True)
        shutil.copy2(CONFIG_FILE, backup_dir / ("config_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json"))
    atomic_write(CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=4))


def active_api_keys(cfg, mode=None):
    mode = mode or cfg.get("routing", {}).get("mode", "free")
    if mode == "free":
        return {"openrouter": cfg.get("api_keys", {}).get("openrouter", "")}
    return dict(cfg.get("paid_api_keys", {}))
