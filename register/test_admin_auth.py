import os
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

_data = tempfile.TemporaryDirectory()
os.environ.update(DATA_DIR=_data.name, SECRET_KEY='test-only-secret', FERNET_KEY=Fernet.generate_key().decode())
import app as portal


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = portal.app.test_client()
        with portal.db() as conn:
            conn.execute('DELETE FROM admin_auth')

    def form(self, client, path):
        client.get(path)
        with client.session_transaction() as session:
            return session['csrf_token']

    def test_password_only_never_creates_session(self):
        csrf = self.form(self.client, '/admin/login')
        with patch.object(portal.admin_auth, 'call', side_effect=[{}, {'user': {'authentication_token': 'password-only'}}]):
            result = self.client.post('/admin/login', data={'csrf_token': csrf, 'username': 'administrator', 'password': 'irrelevant'})
        self.assertEqual(result.status_code, 200)
        self.assertIn('/admin/login', self.client.get('/admin').location)

    def test_csrf_required(self):
        self.assertEqual(self.client.post('/admin/login').status_code, 400)

    def challenge(self):
        return portal.admin_auth.save('challenge', {'cookies': [], 'username': 'administrator'}, 300)

    def test_two_factor_success_exact_role_and_server_side_token(self):
        key = self.challenge()
        with self.client.session_transaction() as session:
            session['admin_challenge'] = key
        csrf = self.form(self.client, '/admin/2fa')
        with patch.object(portal.admin_auth, 'call', return_value={'user': {'authentication_token': 'sensitive-token'}}), patch('admin_auth.requests.Session.get') as get:
            get.return_value.json.return_value = {'username': 'administrator', 'roles': [{'name': 'administrator'}]}
            result = self.client.post('/admin/2fa', data={'csrf_token': csrf, 'code': '123456'})
        self.assertTrue(result.location.endswith('/admin'))
        self.assertEqual(self.client.get('/admin').status_code, 200)
        with self.client.session_transaction() as session:
            self.assertNotIn('sensitive-token', str(dict(session)))
        self.assertEqual(portal.app.test_client().get('/admin').status_code, 302)
        with self.assertRaises(ValueError):
            portal.admin_auth.read(key, 'challenge')

    def test_invalid_codes_lock_challenge(self):
        key = self.challenge()
        with patch.object(portal.admin_auth, 'call', return_value={'errors': ['invalid code']}):
            for _ in range(5):
                with self.assertRaises(ValueError):
                    portal.admin_auth.finish(key, '000000', 1200)
        with self.assertRaises(ValueError):
            portal.admin_auth.read(key, 'challenge')

    def test_non_admin_is_rejected(self):
        key = self.challenge()
        with patch.object(portal.admin_auth, 'call', return_value={'user': {'authentication_token': 'token'}}), patch('admin_auth.requests.Session.get') as get:
            get.return_value.json.return_value = {'username': 'administrator', 'roles': [{'name': 'admin-helper'}]}
            with self.assertRaises(ValueError):
                portal.admin_auth.finish(key, '123456', 1200)

    def test_expiry_and_logout_revoke_session(self):
        key = portal.admin_auth.save('session', {'token': 'token'}, -1)
        with self.assertRaises(ValueError):
            portal.admin_auth.token(key)
        key = portal.admin_auth.save('session', {'token': 'token'}, 1200)
        with self.client.session_transaction() as session:
            session['admin_session'] = key
        csrf = self.form(self.client, '/admin')
        self.client.post('/admin/logout', data={'csrf_token': csrf})
        with self.assertRaises(ValueError):
            portal.admin_auth.token(key)


if __name__ == '__main__':
    unittest.main()
