"""Explicit, bounded real free API check; no paid key or paid fallback."""
import argparse
import json
from pathlib import Path
import queue
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', action='store_true')
    if not parser.parse_args().run:
        parser.error('Gerçek ücretsiz çağrılar için --run gerekli')
    from PyQt6.QtCore import QCoreApplication
    from ai_boardroom.config import load_config, active_api_keys
    from ai_boardroom.orchestrator import DiscussionWorker
    from ai_boardroom.routing import DEFAULT_ROUTING
    app = QCoreApplication([])
    worker = DiscussionWorker('Rol sürekliliği canlı kontrolü', [], 2,
        active_api_keys(load_config(), 'free'), queue.Queue(), routing_config={
            **DEFAULT_ROUTING, 'mode': 'free', 'max_retries': 0,
            'free_retry_same_model': False, 'free_max_fallbacks': 1,
            'free_fallbacks': ['nvidia/nemotron-3.5-lightning:free'],
            'free_catalog_fallback': False})
    worker.status.connect(lambda text: print(text, flush=True))
    result = {'test': 'real_free_role_continuity', 'responses': []}
    try:
        for turn in (1, 2):
            started = time.monotonic()
            previous = result['responses'][-1]['text'] if result['responses'] else ''
            text, provider, model = worker._request_with_fallback('openrouter',
                'inclusionai/ling-3.2-flash:free',
                'Çocuklara sayıları öğretmek için bir oyun öner. En fazla iki kısa Türkçe cümle. '
                + ('Önceki önerini değerlendir: ' + previous if previous else ''),
                purpose=f'Tur {turn} / Eleştirmen', role_name='Eleştirmen')
            result['responses'].append({'turn': turn, 'provider': provider, 'model': model,
                                       'text': text, 'seconds': round(time.monotonic()-started, 2)})
        result['same_model'] = len({r['model'] for r in result['responses']}) == 1
    except Exception as exc:
        # Never serialize raw SDK exceptions or requests.
        result['failure_type'] = type(exc).__name__
    result['state'] = worker.snapshot()
    target = ROOT / 'outputs' / 'live_role_continuity.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'responses': len(result['responses']), 'same_model': result.get('same_model'),
                      'failure_type': result.get('failure_type'), 'report': str(target)}), flush=True)
    return 0 if result.get('same_model') else 1


if __name__ == '__main__':
    sys.exit(main())
