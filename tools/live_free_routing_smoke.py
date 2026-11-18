"""Small real free-layer test. No paid keys or fallback across layers."""
import argparse
import json
from pathlib import Path
import queue
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="store_true", help="Gerçek ücretsiz API çağrılarını başlat")
    args = parser.parse_args()
    if not args.run:
        parser.error("Gerçek çağrı için --run gerekli")
    from PyQt6.QtCore import QCoreApplication
    from ai_boardroom.config import CONFIG_FILE, active_api_keys
    from ai_boardroom.routing import DEFAULT_ROUTING
    from ai_boardroom.orchestrator import DiscussionWorker
    from ai_boardroom.session_log import new_session_dir
    app = QCoreApplication([])
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    keys = active_api_keys(cfg, "free")
    topic = "ÜCRETSİZ YEDEK MODEL TESTİ: Tek kişinin yerel not uygulamasında JSON mu SQLite mı? En fazla üç cümleyle bir öneri sun."
    path = new_session_dir(topic)
    route = {**DEFAULT_ROUTING, "mode": "free", "max_retries": 0, "max_fallbacks": 1,
             "free_fallbacks": ["inclusionai/ling-3.2-flash:free"], "free_interval_seconds": 6}
    personas = [{"name": "Değerlendirici", "role": "Kısa Türkçe yanıt ver; açık GÖRÜŞ ve KARAR META satırları yaz.",
                 "provider": "openrouter", "model": "google/gemma-4-31b-it:free"}]
    worker = DiscussionWorker(topic, personas, 1, keys, queue.Queue(), summary_provider="openrouter",
                              summary_model="inclusionai/ling-3.2-flash:free", session_path=path, routing_config=route)
    worker.status.connect(lambda message: print(message, flush=True))
    worker.error.connect(lambda message: print(message, flush=True))
    worker.run()
    target = ROOT / "outputs" / path.name
    target.mkdir(parents=True, exist_ok=True)
    for file in path.iterdir():
        if file.is_file():
            shutil.copy2(file, target / file.name)
    result = {"status": worker.session_status, "transcript_count": len(worker.transcript),
              "calls": len(worker.api_calls), "errors": worker.api_errors,
              "fallbacks": sum(e["event"] == "fallback" for e in worker.routing_events),
              "reported_cost": sum(c["cost"] for c in worker.api_calls) if worker.api_calls and all(c.get("cost") is not None for c in worker.api_calls) else None,
              "session": str(path), "export": str(target)}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if worker.session_status == "completed" else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Test başlatılamadı:", type(exc).__name__)
        sys.exit(1)
