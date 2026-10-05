#!/usr/bin/env python3
import json
import subprocess
import time
import urllib.request
import urllib.parse
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / '.env'
TOKENS_PATH = ROOT / '.strava-tokens.json'
PORT = 8791
ALLOWED_SPORTS = {'Run', 'Ride', 'Elliptical'}

GARMIN_PYTHON = Path.home() / 'garmin-ai' / '.venv' / 'bin' / 'python3'
GARMIN_SCRIPT = ROOT / 'garmin_bridge.py'
GARMIN_CACHE_PATH = ROOT / '.garmin-cache.json'
GARMIN_CACHE_TTL = 900  # seconds


def fetch_garmin():
    if GARMIN_CACHE_PATH.exists():
        try:
            cached = json.loads(GARMIN_CACHE_PATH.read_text())
            if time.time() - cached.get('_fetchedAt', 0) < GARMIN_CACHE_TTL:
                return cached
        except Exception:
            pass
    try:
        proc = subprocess.run(
            [str(GARMIN_PYTHON), str(GARMIN_SCRIPT)],
            capture_output=True, text=True, timeout=30,
        )
        lines = [l for l in proc.stdout.strip().splitlines() if l.strip()]
        data = json.loads(lines[-1]) if lines else {'error': proc.stderr.strip() or 'no output'}
    except Exception as e:
        data = {'error': str(e)}
    data['_fetchedAt'] = time.time()
    try:
        GARMIN_CACHE_PATH.write_text(json.dumps(data))
    except Exception:
        pass
    return data


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    return env


ENV = load_env()
CLIENT_ID = ENV.get('STRAVA_CLIENT_ID', '')
CLIENT_SECRET = ENV.get('STRAVA_CLIENT_SECRET', '')


def load_tokens():
    if TOKENS_PATH.exists():
        return json.loads(TOKENS_PATH.read_text())
    return None


def save_tokens(tokens):
    TOKENS_PATH.write_text(json.dumps(tokens, indent=2))


def strava_token_request(payload):
    data = urllib.parse.urlencode(payload).encode()
    req = urllib.request.Request('https://www.strava.com/oauth/token', data=data, method='POST')
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def get_valid_access_token():
    tokens = load_tokens()
    if not tokens:
        return None
    if tokens.get('expires_at', 0) - 120 > time.time():
        return tokens['access_token']
    refreshed = strava_token_request({
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'grant_type': 'refresh_token',
        'refresh_token': tokens['refresh_token'],
    })
    tokens.update(refreshed)
    save_tokens(tokens)
    return tokens['access_token']


def fetch_strava_activities(access_token, after_epoch):
    qs = urllib.parse.urlencode({'after': after_epoch, 'per_page': 200})
    req = urllib.request.Request(f'https://www.strava.com/api/v3/athlete/activities?{qs}')
    req.add_header('Authorization', f'Bearer {access_token}')
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location):
        self.send_response(302)
        self.send_header('Location', location)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == '/auth/strava':
            redirect_uri = f'http://localhost:{PORT}/auth/strava/callback'
            params = {
                'client_id': CLIENT_ID,
                'redirect_uri': redirect_uri,
                'response_type': 'code',
                'approval_prompt': 'auto',
                'scope': 'read,activity:read_all',
            }
            url = 'https://www.strava.com/oauth/authorize?' + urllib.parse.urlencode(params)
            self._redirect(url)
            return

        if path == '/auth/strava/callback':
            if qs.get('error'):
                self._redirect('/?strava=error')
                return
            code = qs.get('code', [None])[0]
            if not code:
                self._redirect('/?strava=error')
                return
            try:
                tokens = strava_token_request({
                    'client_id': CLIENT_ID,
                    'client_secret': CLIENT_SECRET,
                    'code': code,
                    'grant_type': 'authorization_code',
                })
                save_tokens(tokens)
                self._redirect('/?strava=connected')
            except Exception:
                self._redirect('/?strava=error')
            return

        if path == '/api/strava/status':
            self._send_json({'connected': bool(load_tokens())})
            return

        if path == '/api/strava/activities':
            try:
                access_token = get_valid_access_token()
                if not access_token:
                    self._send_json({'connected': False, 'activities': []})
                    return
                default_after = int(time.time()) - 21 * 86400
                after = int(qs.get('after', [str(default_after)])[0])
                raw = fetch_strava_activities(access_token, after)
                activities = []
                for a in raw:
                    sport = a.get('sport_type') or a.get('type')
                    if sport not in ALLOWED_SPORTS:
                        continue
                    start_local = a.get('start_date_local', '')
                    date_iso = start_local[:10] if start_local else None
                    if not date_iso:
                        continue
                    activities.append({
                        'id': a.get('id'),
                        'date': date_iso,
                        'sport': sport,
                        'name': a.get('name'),
                        'distance_km': round((a.get('distance') or 0) / 1000, 1),
                        'moving_time_min': round((a.get('moving_time') or 0) / 60),
                    })
                self._send_json({'connected': True, 'activities': activities})
            except Exception as e:
                self._send_json({'connected': False, 'error': str(e)})
            return

        if path == '/api/garmin/recovery':
            raw = fetch_garmin()
            if raw.get('error'):
                self._send_json({'connected': False, 'error': raw['error']})
                return
            stats = raw.get('stats') or {}
            sleep_dto = (raw.get('sleep') or {}).get('dailySleepDTO') or {}
            hrv = (raw.get('hrv') or {}).get('hrvSummary') or {}
            sleep_seconds = sleep_dto.get('sleepTimeSeconds') or stats.get('sleepingSeconds') or 0
            sleep_overall = ((sleep_dto.get('sleepScores') or {}).get('overall') or {}).get('value')
            race_pred = raw.get('racePredictions') or {}
            self._send_json({
                'connected': True,
                'date': raw.get('date'),
                'restingHR': stats.get('restingHeartRate'),
                'sleepHours': round(sleep_seconds / 3600, 1) if sleep_seconds else None,
                'sleepScore': sleep_overall,
                'bodyBatteryWake': stats.get('bodyBatteryAtWakeTime'),
                'bodyBatteryNow': stats.get('bodyBatteryMostRecentValue'),
                'stressAvg': stats.get('averageStressLevel'),
                'hrvStatus': hrv.get('status'),
                'hrvLastNight': hrv.get('lastNightAvg'),
                'time5K': race_pred.get('time5K'),
                'time10K': race_pred.get('time10K'),
            })
            return

        self._serve_static(path)

    def _serve_static(self, path):
        if path == '/':
            path = '/index.html'
        file_path = (ROOT / path.lstrip('/')).resolve()
        if ROOT != file_path and ROOT not in file_path.parents:
            self.send_response(403)
            self.end_headers()
            return
        if not file_path.exists() or not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            return
        content_type = 'text/html'
        if file_path.suffix == '.js':
            content_type = 'application/javascript'
        elif file_path.suffix == '.json':
            content_type = 'application/json'
        elif file_path.suffix == '.css':
            content_type = 'text/css'
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def local_lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def main():
    if not CLIENT_ID or not CLIENT_SECRET:
        print('Missing STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET in .env')
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Training dashboard running at http://localhost:{PORT}')
    lan_ip = local_lan_ip()
    if lan_ip:
        print(f'On your phone (same Wi-Fi): http://{lan_ip}:{PORT}')
    server.serve_forever()


if __name__ == '__main__':
    main()
