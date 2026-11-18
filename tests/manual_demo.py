"""Optional real-Qt manual exercise. Fake API only; temporary profile and sessions."""
from copy import deepcopy
from pathlib import Path
import re
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def fake_response(self, provider, model, prompt, key, system_prompt=None):
    if system_prompt is None:
        return "ÇEVRİMDIŞI DEMO: Bu metin gerçek model çıktısı değildir. Kanıtları inceleyip sonraki adımları insan belirlemelidir."
    name = re.search(r"Adın: (.+)", system_prompt)[1]
    round_number = int(re.search(r"Tur (\d+)/", prompt)[1])
    meta = ["GÖRÜŞ: depolama | SQLite | Tek kullanıcı varsayımı (sahte yanıt)"]
    if name == "Mimar" and round_number == 1:
        meta.append("KARAR: Yerel SQLite kullanımı")
    if name == "Eleştirmen" and round_number == 1:
        meta.append("SORU: Mimar | Eşzamanlı kullanıcı sayısı kaç?")
    if name == "Mimar" and round_number == 2:
        meta.append("YANIT: Q1 | Bu demo için tek kullanıcı varsayıldı.")
    if round_number == 3:
        meta.append("DESTEK: D1 | Demo varsayımı altında uygun")
    return f"[SAHTE API] {name}, tur {round_number}: Tek kullanıcılı yerel kullanım değerlendirildi.\n---META---\n" + "\n".join(meta)


def prepare_offline(ui, orchestrator, session_log, config, cfg, sessions, simulate_rate_limit=True):
    """Patch every network/catalog boundary in this separate demo process."""
    from ai_boardroom import routing
    from ai_boardroom import account_ui
    account_ui.fetch_account = lambda key, management_key="": {
        "quota": {"used": 12, "limit": 50, "remaining": 38}, "credit_remaining": "9.25",
        "checked_at": "ÇEVRİMDIŞI DEMO", "credit_source": "SAHTE HESAP"}
    routing.fetch_free_quota = lambda key: None
    from ai_boardroom import model_ui
    identifiers = ["demo/one:free", "demo/two:free", "demo/three:free"]
    data = {"checked_at": "ÇEVRİMDIŞI DEMO", "models": [
        {"provider": "openrouter", "id": mid, "name": "SAHTE " + mid, "tier": "free",
         "pricing": {"prompt": "0", "completion": "0"}, "source": "catalog"} for mid in identifiers] + [
        {"provider": "openrouter", "id": "demo/paid", "name": "SAHTE Ücretli Örnek", "tier": "paid",
         "pricing": {"prompt": "0.000001", "completion": "0.000002"}, "source": "catalog"}]}
    cfg["catalog_auto_refresh"] = False
    cfg["routing"] = {**deepcopy(routing.DEFAULT_ROUTING), "max_retries": 0, "free_fallbacks": identifiers}
    cfg["summary_provider"], cfg["summary_model"] = "openrouter", identifiers[0]
    for index, persona in enumerate(cfg["personas"]):
        persona.update(provider="openrouter", model=identifiers[index % len(identifiers)])
    ui.load_config = lambda: cfg
    ui.save_config = lambda value: None
    ui.SESSIONS_DIR = session_log.SESSIONS_DIR = sessions
    routing.fetch_catalog = lambda: deepcopy(data)
    model_ui.fetch_catalog = lambda: deepcopy(data)
    model_ui.cached_catalog = lambda: deepcopy(data)
    model_ui.save_catalog = lambda value: None
    clock = [1000.0]
    routing.PACER = routing.RequestPacer(clock=lambda: clock[0])
    def wait(self, seconds, reason=""):
        self._check_stop()
        clock[0] += seconds
    orchestrator.DiscussionWorker._routing_wait = wait
    seen = []
    def raw(self, provider, model, prompt, key, system_prompt=None):
        if simulate_rate_limit and not seen and model == identifiers[0]:
            seen.append(True)
            error = RuntimeError("DEMO 429 Provider returned error")
            error.status_code = 429
            raise error
        text = fake_response(self, provider, model, prompt, key, system_prompt)
        self._save_call_usage(provider, model, model, 100, 100, 0, "stop")
        return text
    orchestrator.DiscussionWorker._raw_call = raw


def main():
    from ai_boardroom import config
    from ai_boardroom import main as ui
    from ai_boardroom import orchestrator
    from ai_boardroom import session_log
    from PyQt6.QtWidgets import QApplication

    with tempfile.TemporaryDirectory(prefix="ai-board-demo-") as temporary:
        sessions = Path(temporary)
        cfg = deepcopy(config.DEFAULT_CONFIG)
        cfg["rounds"] = 3
        cfg["api_keys"] = {provider: "offline-demo" for provider in cfg["api_keys"]}
        prepare_offline(ui, orchestrator, session_log, config, cfg, sessions)
        app = QApplication(sys.argv)
        window = ui.MainWindow()
        window.setWindowTitle("AI-Boardroom — ÇEVRİMDIŞI DEMO / SAHTE API / GEÇİCİ KAYITLAR")
        window.project_input.setPlainText("ÇEVRİMDIŞI DEMO: Tek kullanıcılı yerel not uygulamasının depolaması")
        window.show()
        return app.exec()


if __name__ == "__main__":
    sys.exit(main())
