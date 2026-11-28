"""Run the optional Lens SDK in its own environment."""

import json
import os
import sys
from pathlib import Path


def search(path):
    from google_lens_pro import LensConfig, LensRateLimitError, LensScraper

    browser = os.environ.get('TRACEMUSE_LENS_BROWSER')
    config = LensConfig(use_saved_session=False, headless=True, timeout=8, executable_path=browser)
    try:
        result = LensScraper(config=config).search_file(path)
    except LensRateLimitError as exc:
        return {'status': 'captcha' if 'captcha' in str(exc).lower() else 'rate_limited'}
    except Exception as exc:
        message = str(exc).lower()
        if 'captcha' in message or '/sorry/' in message or 'unusual traffic' in message:
            return {'status': 'captcha'}
        if 'err_cert_' in message or 'certificate_verify_failed' in message:
            return {'status': 'certificate_error'}
        if 'executable doesn\'t exist' in message or 'install chromium' in message or 'playwright install' in message:
            return {'status': 'not_configured'}
        return {'status': 'unavailable'}
    matches = [{'title': item.title, 'url': item.link, 'thumbnail': item.thumbnail}
               for item in result.visual_matches[:40]]
    return {'status': 'found' if matches else 'no_results', 'matches': matches}


if __name__ == '__main__':
    print(json.dumps(search(Path(sys.argv[1])), ensure_ascii=True))
