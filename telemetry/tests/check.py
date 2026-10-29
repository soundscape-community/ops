#!/usr/bin/env python3
"""Validate and exercise Vector with Docker and Python's standard library.

Default mode uses a dummy secret and a local mock. --live additionally sends one
synthetic OTLP event through Vector to EU Mixpanel using the runtime secret file.
"""
import argparse
import base64
import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import urllib.error
import uuid

ROOT = Path(__file__).resolve().parents[1]
IMAGE = (ROOT / 'compose.yaml').read_text().split('image: ', 1)[1].splitlines()[0]
TOKEN = 'dummy-project-token'
NS = 1780000000123456789


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value):
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


def attributes(values):
    return b''.join(field(1, field(1, k) + field(2, field(1, v))) for k, v in values.items())


def log_record(name, values, timestamp=NS):
    # OTLP protobuf: fixed64 time_unix_nano, severity INFO, event_name, attributes.
    record = varint(1 << 3 | 1) + struct.pack('<Q', timestamp)
    record += varint(2 << 3) + varint(9) + field(3, 'INFO') + field(12, name)
    for k, v in values.items():
        record += field(6, field(1, k) + field(2, field(1, v)))
    return record


def request_body(records):
    resource = attributes({'service.name': 'soundscape', 'service.version': 'test', 'service.build': '0'})
    scope = field(1, field(1, 'GDATelemetry'))
    return field(1, field(1, resource) + field(2, scope + b''.join(field(2, r) for r in records)))


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError('Timed out waiting for Vector')


def ready(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=0.2):
            return True
    except OSError:
        return False


def post(port, body):
    request = urllib.request.Request(f'http://127.0.0.1:{port}/v1/logs', body,
                                     {'Content-Type': 'application/x-protobuf'})
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200


def unit_tests():
    base = {'event_name': 'screen_view', 'timestamp': '2026-05-28T20:26:40.123456789Z',
            'attributes': {'user_id': 'test-user', 'screen_name': 'home'},
            'resources': {'service.name': 'soundscape'}}
    tests = []

    def case(name, record, checks=None):
        source = '. = parse_json!(' + json.dumps(json.dumps(record)) + ')\n'
        source += '.timestamp = parse_timestamp!(.timestamp, format: "%+")'
        test = {'name': name, 'inputs': [{'insert_at': 'to_mixpanel', 'type': 'vrl', 'source': source}]}
        if checks is None:
            test['no_outputs_from'] = ['to_mixpanel']
        else:
            test['outputs'] = [{'extract_from': 'to_mixpanel', 'conditions':
                                [{'type': 'vrl', 'source': check} for check in checks]}]
        tests.append(test)

    canonical = {'event': 'screen_view', 'timestamp_ns': NS, 'resources': base['resources'],
                 'attributes': base['attributes']}
    expected = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    case('bodyless screen event and canonical hash', base, [
        '.event == "screen_view" && .properties.screen_name == "home"',
        '.properties.distinct_id == "test-user" && .properties.time == 1780000000',
        '.properties."service.name" == "soundscape" && !exists(.timestamp)',
        f'.properties."$insert_id" == "{expected}"'])
    record = json.loads(json.dumps(base))
    record['attributes'].update({'event_id': 'stable-uuid', 'session_id': 'usage-session',
                                 'distinct_id': 'spoof', 'time': 'bad',
                                 '$insert_id': 'spoof', 'service.name': 'event-wins', 'value': '42'})
    case('reserved fields, explicit ID, strings and merge precedence', record, [
        '.properties."$insert_id" == "stable-uuid" && .properties.distinct_id == "test-user"',
        '.properties.time == 1780000000 && .properties."service.name" == "event-wins"',
        '.properties.value == "42"',
        '.properties.session_id == "usage-session" && .properties.event_id == "stable-uuid"'])
    record = json.loads(json.dumps(record))
    record['attributes']['event_id'] = 'another-occurrence'
    case('same session with a separate event ID', record, [
        '.properties.session_id == "usage-session"',
        '.properties."$insert_id" == "another-occurrence"'])
    for key in ['event_name', 'attributes']:
        record = dict(base)
        del record[key]
        case('missing ' + key, record)
    for name, values in [('missing identity', {}), ('empty identity', {'user_id': '  '})]:
        case(name, {**base, 'attributes': values})
    case('empty event name', {**base, 'event_name': ''})
    return {'tests': tests}


class ImportMock(BaseHTTPRequestHandler):
    attempts = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.attempts.append((self.path, self.headers.get('Authorization'),
                              self.headers.get('Content-Type'), body))
        status = 503 if len(self.attempts) == 1 else 200
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"status":1}')

    def log_message(self, *_):
        pass


def compose_secret_file(project_directory=ROOT, env=None):
    result = subprocess.run(
        ['docker', 'compose', '--project-directory', str(project_directory),
         '-f', str(project_directory / 'compose.yaml'), 'config', '--format', 'json'],
        cwd=project_directory, env=env, capture_output=True, text=True)
    if result.returncode:
        raise SystemExit('Cannot resolve Compose configuration; run docker compose config --quiet in telemetry.')
    return Path(json.loads(result.stdout)['secrets']['mixpanel-token']['file'])


def live_secret_file():
    secret = compose_secret_file()
    try:
        if not secret.is_file() or not secret.read_bytes().strip():
            raise SystemExit(f'Missing or empty Mixpanel token file: {secret}')
    except OSError:
        raise SystemExit(f'Cannot read Mixpanel token file: {secret}') from None
    return secret


def check_compose_paths():
    # Isolate dotenv discovery from the checkout and the caller's environment.
    env = {key: value for key, value in os.environ.items()
           if key != 'SECRETS_DIR' and not key.startswith('COMPOSE_')}
    with tempfile.TemporaryDirectory(prefix='soundscape-compose-') as directory:
        project = Path(directory)
        (project / 'compose.yaml').write_text((ROOT / 'compose.yaml').read_text())
        assert compose_secret_file(project, env) == project / 'secrets/mixpanel-token'
        (project / '.env').write_text('SECRETS_DIR=./custom-secrets\n')
        assert compose_secret_file(project, env) == project / 'custom-secrets/mixpanel-token'
        production = '/home/soundscape/.secrets/telemetry'
        assert compose_secret_file(project, {**env, 'SECRETS_DIR': production}) == Path(production) / 'mixpanel-token'
    print('PASS: Compose default, local .env override, production environment override')


@contextlib.contextmanager
def configuration(endpoint=None, live=False):
    with tempfile.TemporaryDirectory(prefix='soundscape-vector-') as directory:
        root = Path(directory)
        (root / 'secrets').mkdir()
        (root / 'secrets/mixpanel-token').write_text(TOKEN + '\n')
        (root / 'secrets/mixpanel-token').chmod(0o600)
        config = (ROOT / 'vector.yaml').read_text()
        http, grpc = free_port(), free_port()
        config = config.replace('0.0.0.0:4318', f'127.0.0.1:{http}')
        config = config.replace('0.0.0.0:4317', f'127.0.0.1:{grpc}')
        if endpoint:
            config = config.replace('https://api-eu.mixpanel.com/import?strict=1', endpoint)
        (root / 'vector.yaml').write_text(config)
        (root / 'mixpanel.vrl').write_text((ROOT / 'mixpanel.vrl').read_text())
        (root / 'tests.json').write_text(json.dumps(unit_tests()))
        secret = live_secret_file() if live else root / 'secrets/mixpanel-token'
        command = ['docker', 'run', '--rm', '--network', 'host',
                   '-v', f'{root}:/etc/vector:ro',
                   '--mount', f'type=bind,source={secret},target=/run/secrets/mixpanel-token,readonly', IMAGE]
        yield command, http


@contextlib.contextmanager
def vector(command):
    name = 'soundscape-vector-test-' + uuid.uuid4().hex[:12]
    command = command[:3] + ['--name', name] + command[3:]
    with tempfile.TemporaryFile(mode='w+') as logs:
        process = subprocess.Popen(command, stdout=logs, stderr=logs)
        try:
            yield process, logs
        finally:
            subprocess.run(['docker', 'stop', '--time', '5', name], capture_output=True)
            process.wait(timeout=15)


def check_local():
    server = ThreadingHTTPServer(('127.0.0.1', 0), ImportMock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with configuration(endpoint=f'http://127.0.0.1:{server.server_port}/import?strict=1') as (command, port):
            subprocess.run(command + ['validate', '--no-environment', '/etc/vector/vector.yaml'], check=True)
            subprocess.run(command + ['test', '/etc/vector/vector.yaml', '/etc/vector/tests.json'], check=True)
            with vector(command):
                wait_for(lambda: ready(port))
                body = request_body([log_record('screen_view', {'user_id': 'test-user', 'screen_name': 'home',
                                                              'event_id': 'original-event-uuid',
                                                              'session_id': 'original-session-uuid'}),
                                     log_record('test.value', {'user_id': 'test-user', 'value': '42'})])
                post(port, body)
                wait_for(lambda: len(ImportMock.attempts) >= 2)
                assert ImportMock.attempts[0] == ImportMock.attempts[1], 'Retry changed the request'
                path, auth, content_type, payload = ImportMock.attempts[1]
                assert path == '/import?strict=1'
                assert auth == 'Basic ' + base64.b64encode((TOKEN + ':').encode()).decode()
                assert content_type == 'application/x-ndjson'
                events = [json.loads(line) for line in payload.splitlines()]
                assert len(events) == 2, 'Expected a two-event NDJSON batch'
                assert events[0]['event'] == 'screen_view'
                assert events[0]['properties']['time'] == NS // 1_000_000_000
                assert events[0]['properties']['service.version'] == 'test'
                assert events[0]['properties']['$insert_id'] == 'original-event-uuid'
                assert events[0]['properties']['event_id'] == 'original-event-uuid'
                assert events[0]['properties']['session_id'] == 'original-session-uuid'
                assert 'session_id' not in events[1]['properties'], 'Do not invent sessions for older records'
                assert events[1]['properties']['value'] == '42'
                assert events[0]['properties']['$insert_id'] != events[1]['properties']['$insert_id']
                post(port, body)
                wait_for(lambda: len(ImportMock.attempts) >= 3)
                assert payload == ImportMock.attempts[2][3], 'Replay changed event IDs or properties'
        print('PASS: protobuf ingress, Basic Auth, NDJSON batch, 503 retry, event/session IDs preserved on replay')
    finally:
        server.shutdown()
        server.server_close()


def check_live():
    # A local relay captures the actual import response, which Vector otherwise
    # consumes internally. The runtime token is mounted directly, never copied.
    responses = []

    class Relay(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            request = urllib.request.Request('https://api-eu.mixpanel.com/import?strict=1', body,
                {'Content-Type': 'application/x-ndjson', 'Authorization': self.headers['Authorization']})
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    status, payload = response.status, response.read()
            except urllib.error.HTTPError as error:
                status, payload = error.code, error.read()
            responses.append((status, json.loads(payload)))
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    identifier = str(uuid.uuid4())
    try:
        with configuration(live=True, endpoint=f'http://127.0.0.1:{server.server_port}/import?strict=1') as (command, port):
            with vector(command):
                wait_for(lambda: ready(port))
                post(port, request_body([log_record('soundscape.telemetry_smoke_test',
                     {'user_id': 'ops-smoke-test', 'event_id': identifier, 'test_source': 'ops-vector'}, time.time_ns())]))
                wait_for(lambda: bool(responses))
                status, result = responses[0]
                assert status == 200 and result.get('num_records_imported') == 1, f'Import failed: HTTP {status}'
                print(f'PASS: EU Mixpanel HTTP {status}, num_records_imported={result["num_records_imported"]}')
    finally:
        server.shutdown()
        server.server_close()
    print(f'Live event: soundscape.telemetry_smoke_test; $insert_id={identifier}')
    print('Confirm in Mixpanel Events → Live view (user ops-smoke-test).')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    check_compose_paths()
    check_local()
    if args.live:
        check_live()
