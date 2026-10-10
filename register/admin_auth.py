"""Native OpenTAKServer two-factor authentication; no local password database."""
import json
import secrets
import time

import requests


class AdminAuth:
    def __init__(self, connect, cipher, base_url, verify_tls):
        self.connect, self.cipher = connect, cipher
        self.base_url, self.verify_tls = base_url.rstrip('/'), verify_tls

    def init_db(self):
        with self.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS admin_auth (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload BLOB NOT NULL, expires INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0)')

    def save(self, kind, payload, ttl):
        key = secrets.token_urlsafe(32)
        with self.connect() as conn:
            conn.execute('DELETE FROM admin_auth WHERE expires <= ?', (int(time.time()),))
            conn.execute('INSERT INTO admin_auth(id,kind,payload,expires) VALUES(?,?,?,?)', (key, kind, self.cipher.encrypt(json.dumps(payload).encode()), int(time.time()) + ttl))
        return key

    def delete(self, key):
        with self.connect() as conn:
            return conn.execute('DELETE FROM admin_auth WHERE id=?', (key,)).rowcount

    def read(self, key, kind, attempt=False):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM admin_auth WHERE id=? AND kind=?', (key, kind)).fetchone()
            if not row or row['expires'] <= time.time() or row['attempts'] >= 5:
                raise ValueError('驗證已逾時或超過嘗試次數，請重新登入。')
            if attempt:
                conn.execute('UPDATE admin_auth SET attempts=attempts+1 WHERE id=?', (key,))
            return json.loads(self.cipher.decrypt(row['payload']))

    def call(self, client, method, path, **kwargs):
        headers = {'Accept': 'application/json'}
        csrf = client.cookies.get('XSRF-TOKEN') or getattr(client, '_ots_csrf', None)
        if csrf:
            headers['X-XSRF-Token'] = csrf
        response = client.request(method, self.base_url + path, headers=headers,
                                  verify=self.verify_tls, timeout=30, **kwargs)
        if response.status_code >= 400:
            raise ValueError('帳密或驗證碼不正確，或 OpenTAK 驗證服務無法使用。')
        result = response.json().get('response', {})
        if result.get('csrf_token'):
            client._ots_csrf = result['csrf_token']
        return result

    def begin(self, username, password):
        with requests.Session() as client:
            self.call(client, 'GET', '/api/login')
            result = self.call(client, 'POST', '/api/login', params={'include_auth_token': ''},
                               json={'username': username, 'password': password})
            # A password-only token is never sufficient to open the admin portal.
            if result.get('tf_state') != 'ready':
                raise ValueError('請先在 OpenTAKServer 管理介面啟用兩步驟驗證，並設定每次登入驗證。')
            cookies = [dict(name=c.name, value=c.value, domain=c.domain, path=c.path,
                            secure=c.secure, expires=c.expires) for c in client.cookies]
            key = self.save('challenge', {'cookies': cookies, 'username': username,
                                         'csrf': getattr(client, '_ots_csrf', None)}, 300)
            return key, result.get('tf_primary_method', 'authenticator')

    def finish(self, key, code, ttl):
        data = self.read(key, 'challenge', attempt=True)
        with requests.Session() as client:
            client._ots_csrf = data.get('csrf')
            for cookie in data['cookies']:
                client.cookies.set_cookie(requests.cookies.create_cookie(**cookie))
            result = self.call(client, 'POST', '/api/tf-validate',
                               params={'include_auth_token': ''}, json={'code': code})
            token = result.get('user', {}).get('authentication_token')
            if not token:
                raise ValueError('驗證碼未通過，請重試。')
            response = client.get(self.base_url + '/api/me', headers={'Authentication-Token': token},
                                  verify=self.verify_tls, timeout=30)
            response.raise_for_status()
            me = response.json()
            user = me.get('response', {}).get('user', me)
            roles = user.get('roles', [])
            names = {r if isinstance(r, str) else r.get('name') for r in roles if isinstance(r, (str, dict))}
            if 'administrator' not in names or user.get('username') != data['username']:
                self.delete(key)
                raise ValueError('此帳號不是 OpenTAK administrator。')
        if not self.delete(key):
            raise ValueError('此驗證已使用，請重新登入。')
        return self.save('session', {'token': token}, ttl)

    def token(self, key):
        return self.read(key, 'session')['token']
