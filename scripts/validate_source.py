"""Reject accidentally packaged credentials and broken relative README links."""
import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
errors = []
for name in filter(None, paths):
    p = Path(name)
    if p.name == '.env' or p.suffix.lower() in {'.p12', '.pfx', '.key', '.sqlite', '.db'} or name.startswith('data/'):
        errors.append(f'Forbidden runtime file: {name}')
    text = (root / p).read_text(errors='replace')
    if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', text):
        errors.append(f'Private key content: {name}')
    if re.search(r'\b(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{50,}|sk-(?:proj-)?[A-Za-z0-9_-]{40,})\b', text):
        errors.append(f'Possible access token: {name}')
for link in re.findall(r'\]\(([^)]+)\)', (root / 'README.md').read_text()):
    if '://' not in link and not link.startswith('#') and not (root / link.split('#')[0]).exists():
        errors.append(f'Broken local README link: {link}')
if errors:
    raise SystemExit('\n'.join(errors))
print('Source hygiene and local README links passed (heuristic scan).')
