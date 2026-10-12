"""Read-only E2B API probe. Does not print credentials or sandbox response bodies."""
import argparse
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(url, key=None):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('HTTP(S) URL without embedded credentials required')
    headers = {'Accept': 'application/json'}
    if key:
        headers['X-API-Key'] = key
    opener = urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with opener.open(request, timeout=10) as response:
            data = response.read(1024 * 1024 + 1)
            return response.status, data
    except urllib.error.HTTPError as error:
        return error.code, b''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api-url', required=True, help='Confirmed E2B control API base URL')
    parser.add_argument('--key-env', default='E2B_API_KEY')
    args = parser.parse_args()
    base = args.api_url.rstrip('/')
    try:
        code, _ = probe(base + '/health')
        print(f'GET /health: HTTP {code} (health alone does not verify sandbox capability)')
        key = os.environ.get(args.key_env) or getpass.getpass('E2B API key (not echoed): ')
        if not key or '\n' in key or '\r' in key:
            raise ValueError('API key is empty or malformed')
        code, body = probe(base + '/sandboxes?limit=1', key)
        print(f'GET /sandboxes?limit=1: HTTP {code}')
        if code != 200:
            print('FAIL: check API address, authorization and supported API version; no mutation was sent.')
            return 1
        if len(body) > 1024 * 1024:
            print('FAIL: response exceeds probe limit; server may ignore pagination.')
            return 1
        data = json.loads(body)
        if not isinstance(data, list) or any(not isinstance(x, dict) or 'sandboxID' not in x for x in data):
            print('FAIL: response shape differs from this adapter; response body was not printed.')
            return 1
        print('PASS: control API reachable and sandbox list readable; create/envd/pause/resume still unverified.')
        return 0
    except (OSError, ValueError, urllib.error.URLError) as error:
        print(f'FAIL: {type(error).__name__}; check network, TLS trust, URL and key locally.')
        return 1


if __name__ == '__main__':
    sys.exit(main())
