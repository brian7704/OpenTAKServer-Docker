"""Exercise the deployed mail function without real accounts or an SMTP server."""
import ast
import io
import os
from pathlib import Path
import smtplib
import ssl
import unittest
import zipfile
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

source = ast.parse(Path(__file__).with_name('app.py').read_text())
function = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == 'send_decision_email')
namespace = dict(os=os, smtplib=smtplib, ssl=ssl, EmailMessage=EmailMessage, io=io, zipfile=zipfile)
exec(compile(ast.Module(body=[function], type_ignores=[]), 'app.py', 'exec'), namespace)
send = namespace['send_decision_email']
row = dict(id=1, email='test@example.com', username='test', callsign='TEST')

class MailTests(unittest.TestCase):
    def test_missing_and_blank_host_skip_before_package_read(self):
        for env in ({}, {'SMTP_HOST': ''}, {'SMTP_HOST': '   '}):
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), patch.object(smtplib, 'SMTP') as smtp:
                self.assertEqual(send(row, True), 'disabled')
                smtp.assert_not_called()

    def test_no_recipient(self):
        self.assertEqual(send(dict(row, email=''), False), 'no-email')

    def test_starttls_and_auth(self):
        with patch.dict(os.environ, {'SMTP_HOST':'mail.example.com','SMTP_PORT':'587','SMTP_STARTTLS':'true','SMTP_USERNAME':'user','SMTP_PASSWORD':'secret'}, clear=True), patch.object(smtplib,'SMTP') as factory:
            smtp = factory.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            self.assertEqual(send(row, False), 'sent')
            factory.assert_called_once_with('mail.example.com',587,timeout=15)
            smtp.starttls.assert_called_once()
            smtp.login.assert_called_once_with('user','secret')
            self.assertEqual(smtp.send_message.call_args.args[0]['To'],row['email'])

    def test_implicit_tls(self):
        with patch.dict(os.environ, {'SMTP_HOST':'mail.example.com','SMTP_SSL':'true'}, clear=True), patch.object(smtplib,'SMTP_SSL') as factory:
            smtp = factory.return_value.__enter__.return_value
            smtp.send_message.return_value = {}
            self.assertEqual(send(row,False),'sent')
            self.assertEqual(factory.call_args.args,('mail.example.com',465))
            smtp.starttls.assert_not_called()

    def test_invalid_settings_fail_clearly(self):
        for options in ({'SMTP_SSL':'true','SMTP_STARTTLS':'true'}, {'SMTP_PORT':'bad'}, {'SMTP_PORT':'0'}, {'SMTP_PORT':'65536'}, {'SMTP_USERNAME':'user'}, {'SMTP_PASSWORD':'secret'}):
            with self.subTest(options=options), patch.dict(os.environ,dict(SMTP_HOST='mail.example.com',**options),clear=True):
                with self.assertRaises(RuntimeError): send(row,False)

    def test_refused_recipient_is_not_reported_as_sent(self):
        with patch.dict(os.environ, {'SMTP_HOST':'mail.example.com'}, clear=True), patch.object(smtplib,'SMTP') as factory:
            factory.return_value.__enter__.return_value.send_message.return_value = {row['email']:(550,b'rejected')}
            with self.assertRaises(RuntimeError): send(row,False)

if __name__ == '__main__': unittest.main()
