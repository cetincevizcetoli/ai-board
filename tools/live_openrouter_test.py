"""Explicitly invoked real API test; fixed free models, no paid/provider fallback."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
import queue
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description="OpenRouter ile gerçek, ücretsiz model testi")
    parser.add_argument("--run", action="store_true", help="Gerçek API çağrılarını açıkça etkinleştir")
    parser.add_argument("--critic-model", default="nvidia/nemotron-3-ultra-550b-a55b:free")
    parser.add_argument("--summary-model", default="inclusionai/ling-3.2-flash:free")
    args = parser.parse_args()
    if not args.run:
        parser.error("Gerçek API testi için --run gerekli.")

    import requests
    from PyQt6.QtCore import QCoreApplication
    from ai_boardroom.config import CONFIG_FILE
    from ai_boardroom import orchestrator
    from ai_boardroom.session_log import new_session_dir

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    key = config.get("api_keys", {}).get("openrouter") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OpenRouter anahtarı bulunamadı. Uygulamanın Ayarlar sekmesinden girin.")
        return 1
    models = ["nvidia/nemotron-3.5-lightning:free", args.critic_model, args.summary_model]
    response = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
    response.raise_for_status()
    catalog = {m["id"]: m for m in response.json()["data"]}
    for model in models:
        entry = catalog.get(model)
        if not model.endswith(":free") or not entry or any(Decimal(str(entry.get("pricing", {}).get(field, "-1"))) != 0
                            for field in ("prompt", "completion")):
            print("Model bulunamadı veya ücretsiz değil; çağrı yapılmadı:", model)
            return 1
    app = QCoreApplication([])
    topic = ("CANLI OPENROUTER TESTİ: Tek kişinin kendi bilgisayarında kullanacağı basit bir yapılacaklar "
             "listesinde JSON dosyası mı SQLite mı seçilmeli? İnternet, hesap açma, bildirim ve çok "
             "kullanıcılı kullanım kapsam dışı. Kararı gerekçelendir; gereksiz özellik ekleme.")
    personas = [
        {"name": "Mimar", "provider": "openrouter", "model": models[0],
         "role": "Basitlik ve düşük bakım maliyetini savun. JSON seçeneğini önce değerlendir. "
                 "Diğer ajanın gerekçelerini değerlendir; gerekirse fikrini değiştir. "
                 "Türkçe, en fazla 120 kelime ve kısa META satırları kullan."},
        {"name": "Eleştirmen", "provider": "openrouter", "model": models[1],
         "role": "Veri bütünlüğü ve gerçek ihtiyaçları değerlendir. SQLite seçeneğini sorgula ve karşılaştır. "
                 "Mimarın görüşüne doğrudan cevap ver ve ona bir somut soru sor. "
                 "Türkçe, en fazla 120 kelime ve kısa META satırları kullan."},
    ]
    # Only these free OpenRouter calls are allowed in this diagnostic run.
    orchestrator.SUMMARY_FALLBACK_ORDER = ["openrouter"]
    orchestrator.MAX_RETRIES = 0
    session = new_session_dir(topic)
    worker = orchestrator.DiscussionWorker(topic, personas, 2, {"openrouter": key}, queue.Queue(),
                                          summary_provider="openrouter", summary_model=models[2], session_path=session)
    worker.round_paused.connect(lambda number: worker.resume())
    worker.status.connect(lambda text: print(text, flush=True))
    worker.error.connect(lambda text: print(text, flush=True))
    worker.run()
    exported = ROOT / "outputs" / session.name
    exported.mkdir(parents=True, exist_ok=True)
    for name in ("transcript.json", "ozet.md"):
        source = session / name
        if source.exists():
            shutil.copy2(source, exported / name)
    results = {"status": worker.session_status, "models": models, "rounds": worker.completed_rounds,
               "message_count": len(worker.transcript), "api_calls": worker.api_calls,
               "questions": worker.questions, "assessments": worker.decision_assessments(),
               "warnings": worker.meta_warnings, "unresolved_references": worker.unresolved_references,
               "api_errors": worker.api_errors,
               "original_session": str(session), "export_folder": str(exported)}
    (exported / "canli_test_sonucu.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": worker.session_status, "rounds": worker.completed_rounds,
                      "messages": len(worker.transcript), "api_call_count": len(worker.api_calls),
                      "reported_cost": sum(c["cost"] for c in worker.api_calls) if worker.api_calls and all(c.get("cost") is not None for c in worker.api_calls) else None,
                      "original_session": str(session), "export_folder": str(exported)}, ensure_ascii=False), flush=True)
    return 0 if worker.session_status == "completed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Never print raw SDK/request errors or credentials.
        print("Canlı test başlatılamadı:", type(error).__name__)
        sys.exit(1)
