#!/usr/bin/env python3
"""ORBIT Skill Vault: local, opt-in, non-destructive skill inventory and sync.

No internet access, no remote shell, no execution of skill payloads. A skill is
untrusted code/instructions until a human reviews and approves its exact digest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from pathlib import Path

DENIED_NAMES = {'.env', '.env.local', '.npmrc', '.netrc', 'id_rsa', 'id_ed25519', 'credentials.json', 'secrets.json', '.DS_Store'}
DENIED_SUFFIXES = {'.pem', '.pfx', '.p12', '.key'}
MAX_FILE_BYTES = 1_000_000
MAX_SKILL_BYTES = 5_000_000
MAX_SKILL_FILES = 250
NAME = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
SUSPECT = [
    (re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----'), 'private-key marker'),
    (re.compile(rb'ghp_[A-Za-z0-9]{30,}'), 'GitHub token marker'),
    (re.compile(rb'github_pat_[A-Za-z0-9_]{30,}'), 'GitHub token marker'),
    (re.compile(rb'AKIA[A-Z0-9]{16}'), 'AWS key marker'),
    (re.compile(rb'(?i)(?:curl|wget)[^\n]{0,200}\|\s*(?:sudo\s+)?(?:ba)?sh\b'), 'remote shell execution'),
    (re.compile(rb'(?i)rm\s+-rf\s+(/|~)'), 'destructive command'),
    (re.compile(rb'(?i)(?:ignore (?:all |previous )?instructions|disable (?:safety|approval)|exfiltrat)'), 'instruction safety warning'),
]
DEFAULT_SOURCES = [
    '~/.hermes/skills', '~/.agents/skills', '~/.claude/skills', '~/.codex/skills',
    '~/.config/opencode/skills', '~/.gemini/skills', '~/.luna/skills'
]


def expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value))).absolute()


def metadata(path: Path) -> dict:
    """Small fail-closed parser for standard single-line name/description frontmatter."""
    try:
        content = (path / 'SKILL.md').read_text(encoding='utf-8')
    except (OSError, UnicodeError) as e:
        raise ValueError('missing or non-UTF8 SKILL.md') from e
    m = re.match(r'\A---\s*\n(.*?)\n---(?:\s*\n|\Z)', content, re.S)
    if not m:
        raise ValueError('YAML frontmatter required')
    info = {}
    lines = m.group(1).splitlines()
    for index, line in enumerate(lines):
        hit = re.match(r'^([a-z][a-z-]*):\s*(.*?)\s*$', line)
        if hit:
            value = hit.group(2).strip().strip('"\'')
            if hit.group(1) == 'description' and value in ('>', '|', '>-', '|-'):
                rest = []
                for following in lines[index + 1:]:
                    if following and not following[0].isspace():
                        break
                    rest.append(following.strip())
                value = ' '.join(x for x in rest if x)
            if hit.group(1) in ('name', 'description'):
                info[hit.group(1)] = value
    name, description = info.get('name', ''), info.get('description', '')
    if not name or len(name) > 64 or not NAME.fullmatch(name):
        raise ValueError('invalid skill name')
    if not description or len(description) > 1024:
        raise ValueError('missing or overlong description')
    return {'name': name, 'description': description}


def tree_digest(root: Path) -> tuple[str, list[str], int]:
    """Deterministic SHA256 of safe regular-file tree; refuse symlinks and secrets."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError('skill root must be a real directory')
    digest = hashlib.sha256()
    warnings = []
    total = 0
    count = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        for entry in dirs:
            subdir = Path(parent) / entry
            if entry in DENIED_NAMES or entry in ('.ssh', '.aws', '.gnupg', '.git'):
                raise ValueError(f'credential-like directory: {subdir.relative_to(root)}')
            if subdir.is_symlink():
                raise ValueError(f'symlink directory: {subdir.relative_to(root)}')
        for entry in sorted(files):
            file = Path(parent) / entry
            rel = file.relative_to(root)
            if file.is_symlink() or not file.is_file() or not stat.S_ISREG(file.lstat().st_mode):
                raise ValueError(f'nonregular file: {rel}')
            if entry in DENIED_NAMES or file.suffix.lower() in DENIED_SUFFIXES:
                raise ValueError(f'credential-like filename: {rel}')
            size = file.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError(f'oversized file: {rel}')
            count += 1
            total += size
            if count > MAX_SKILL_FILES or total > MAX_SKILL_BYTES:
                raise ValueError('skill exceeds file count or size limit')
            data = file.read_bytes()
            for pattern, reason in SUSPECT:
                if pattern.search(data):
                    warnings.append(f'{rel}: {reason}')
            if rel.suffix.lower() in ('.sh', '.ps1', '.bat', '.exe', '.py', '.js', '.ts') or 'scripts' in rel.parts:
                warnings.append(f'{rel}: executable/script content requires review')
            digest.update(str(rel).replace('\\', '/').encode() + b'\0')
            digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest(), sorted(set(warnings)), count


def discover(paths: list[Path]) -> list[dict]:
    found = []
    for root in paths:
        if not root.is_dir():
            continue
        for parent, dirs, files in os.walk(root, followlinks=False):
            current = Path(parent)
            depth = len(current.relative_to(root).parts)
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (current / d).is_symlink()) if depth < 5 else []
            if 'SKILL.md' not in files:
                continue
            dirs[:] = []
            result = {'path': str(current), 'source': str(root)}
            try:
                info = metadata(current)
                hash_value, warnings, count = tree_digest(current)
                result.update(info, digest=hash_value, warnings=warnings, file_count=count, valid=True)
            except (ValueError, OSError) as e:
                result.update(valid=False, error=str(e))
            found.append(result)
    return sorted(found, key=lambda item: (item.get('name', ''), item['path']))


def inspect_one(path: Path) -> dict:
    info = metadata(path)
    value, warnings, count = tree_digest(path)
    return {**info, 'digest': value, 'warnings': warnings, 'file_count': count}


def guard_dir(vault: Path):
    if vault.is_symlink():
        raise ValueError('vault must not be a symlink')
    vault.mkdir(parents=True, exist_ok=True)
    try:
        vault.chmod(0o700)
    except OSError:
        pass


def copy_checked(source: Path, dest: Path, expected: str):
    if dest.exists() or dest.is_symlink():
        raise ValueError(f'refusing to overwrite {dest}')
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest, symlinks=True)
    try:
        actual = inspect_one(dest)['digest']
        if actual != expected:
            raise ValueError('source changed while copying; destination removed')
    except Exception:
        shutil.rmtree(dest)
        raise


def stage(source: Path, vault: Path, apply: bool) -> dict:
    info = inspect_one(source)
    dest = vault / 'staging' / info['name']
    approved = vault / 'approved' / info['name']
    if dest.exists() or approved.exists():
        return {'status': 'conflict', 'name': info['name'], 'reason': 'existing staged or approved name; manual comparison needed', **info}
    result = {'status': 'staged' if apply else 'dry-run', 'name': info['name'], 'source': str(source), **info}
    if apply:
        guard_dir(vault)
        copy_checked(source, dest, info['digest'])
    return result


def approve(vault: Path, name: str, digest: str, apply: bool) -> dict:
    if not NAME.fullmatch(name):
        raise ValueError('invalid skill name')
    source, dest = vault / 'staging' / name, vault / 'approved' / name
    if not source.is_dir() or source.is_symlink():
        raise ValueError('skill not staged')
    info = inspect_one(source)
    if info['name'] != name or digest != info['digest']:
        raise ValueError('name or SHA256 mismatch: stage has changed')
    if dest.exists() or dest.is_symlink():
        raise ValueError('approved skill of that name exists: refusing overwrite')
    if apply:
        guard_dir(vault)
        copy_checked(source, dest, digest)
    return {'status': 'approved' if apply else 'dry-run', **info}


def deploy(vault: Path, target: Path, apply: bool) -> list[dict]:
    if target.is_symlink():
        raise ValueError('target must not be a symlink')
    approved = vault / 'approved'
    if not approved.is_dir():
        return []
    results = []
    for source in sorted(approved.iterdir()):
        if not source.is_dir() or source.is_symlink():
            continue
        try:
            info = inspect_one(source)
            if source.name != info['name']:
                raise ValueError('directory name must match skill name')
            dest = target / source.name
            if dest.exists() or dest.is_symlink():
                try:
                    existing = inspect_one(dest) if dest.is_dir() and not dest.is_symlink() else {}
                except (ValueError, OSError):
                    existing = {}
                results.append({'name': info['name'], 'status': 'already-present' if existing.get('digest') == info['digest'] else 'conflict', 'target': str(dest)})
                continue
            if apply:
                copy_checked(source, dest, info['digest'])
            results.append({'name': info['name'], 'status': 'deployed' if apply else 'dry-run', 'target': str(dest)})
        except (ValueError, OSError) as e:
            results.append({'name': source.name, 'status': 'error', 'reason': str(e)})
    return results


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    inventory = sub.add_parser('inventory', help='Find and hash skill directories without modifications')
    inventory.add_argument('--source', action='append', help='Scan root (repeatable; default common agent paths)')
    staging = sub.add_parser('stage', help='Copy one skill to private staging; NEVER auto-approve')
    staging.add_argument('source', help='Path to a single skill directory')
    staging.add_argument('--vault', default='~/.local/share/orbit-skill-vault')
    staging.add_argument('--apply', action='store_true')
    approval = sub.add_parser('approve', help='Explicitly approve exact, previously reviewed SHA256 digest')
    approval.add_argument('name')
    approval.add_argument('--sha256', required=True)
    approval.add_argument('--vault', default='~/.local/share/orbit-skill-vault')
    approval.add_argument('--apply', action='store_true')
    publishing = sub.add_parser('deploy', help='Copy approved skills into target (never overwrites)')
    publishing.add_argument('--vault', default='~/.local/share/orbit-skill-vault')
    publishing.add_argument('--target', default='~/.agents/skills')
    publishing.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'inventory':
            paths = [expand(p) for p in (args.source or DEFAULT_SOURCES)]
            results = discover(paths)
            seen = {}
            for item in results:
                key = item.get('name')
                if key:
                    seen.setdefault(key, set()).add(item['digest'])
            output = {'sources': [str(p) for p in paths], 'skills': results, 'found': len(results),
                      'conflicting_names': sorted(k for k, v in seen.items() if len(v) > 1)}
        elif args.command == 'stage':
            output = stage(expand(args.source), expand(args.vault), args.apply)
        elif args.command == 'approve':
            output = approve(expand(args.vault), args.name, args.sha256, args.apply)
        else:
            output = deploy(expand(args.vault), expand(args.target), args.apply)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if isinstance(output, dict) and output.get('status') == 'conflict':
            return 2
        return 0
    except (OSError, ValueError) as e:
        print(json.dumps({'error': str(e)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(cli())
