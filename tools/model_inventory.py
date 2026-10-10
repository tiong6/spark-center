#!/usr/bin/env python3
"""Explicit model sources, persisted metadata snapshots; never hash model weights.

No NAS access during page polling. Explicit refresh uses one isolated worker at
a time; a stuck hard-NFS read cannot hold the web server or multiply workers.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

DATA = Path(__file__).resolve().parents[1] / 'data'
SOURCES = DATA / 'model-sources.json'
CACHE = DATA / 'model-inventory.json'
_lock = threading.Lock()
_running = False
_worker = None


def read(path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback


def snapshot():
    with _lock:
        return {**read(CACHE, {'sources': []}), 'scanning': _running or (_worker is not None and _worker.poll() is None)}


def mounted(path):
    # Read the kernel mount table without touching automount paths.
    mounts = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        p = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), left.split()[4])
        if path == p or path.startswith(p.rstrip('/') + '/'):
            mounts.append((len(p), right.split()[0]))
    return max(mounts, default=(0, 'unknown'))[1]


def file_info(paths):
    files, missing, seen = [], [], set()
    for p in sorted(set(map(str, paths))):
        try:
            st = Path(p).stat()
            if not Path(p).is_file():
                raise FileNotFoundError(p)
            if (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                files.append({'path': p, 'bytes': st.st_size})
        except FileNotFoundError:
            missing.append(p)
    return files, missing


def row(name, provider, paths, **extra):
    files, missing = file_info(paths)
    return {'name': name, 'provider': provider, 'files': files, 'missing': missing,
            'bytes': sum(f['bytes'] for f in files) if not missing else None,
            'state': 'missing' if missing else 'metadata', **extra}


def scan(source):
    root = Path(source['path'])
    fs = mounted(str(root))
    if source.get('storage') == 'NAS' and fs not in ('nfs', 'nfs4', 'cifs'):
        raise OSError('NAS not mounted; previous snapshot retained')
    rows = []
    if source['type'] == 'strata':
        cfg = json.loads(root.read_text())
        args = cfg['args']
        paths = []
        cwd = Path(cfg.get('cwd') or root.parent)
        for i, flag in enumerate(args[:-1]):
            if flag in ('--native', '--ple-gguf', '--pack', '--mtp', '--vision-model'):
                p = Path(args[i + 1])
                if not p.is_absolute():
                    p = cwd / p
                if flag in ('--pack', '--mtp') and p.is_dir():
                    paths.extend(x for x in p.rglob('*') if x.is_file())
                else:
                    paths.append(p)
        vision = cfg.get('vision') or {}
        for key in ('model', 'mmproj'):
            if vision.get(key):
                paths.append(Path(vision[key]))
        rows.append(row(cfg['model_name'], 'Strata', paths, config=str(root)))
    elif source['type'] in ('ollama', 'ollama_archive'):
        manifests = ([root / source['manifest']] if source.get('manifest') else
                     sorted((root / 'manifests').glob('*/*/*/*')))
        groups = {}
        if not (root / 'manifests').is_dir():
            raise OSError('Ollama manifest directory unavailable')
        for manifest in manifests:
            j = json.loads(manifest.read_text())
            layers = [j['config'], *j['layers']]
            for layer in layers:
                if not re.fullmatch(r'sha256:[0-9a-f]{64}', layer['digest']):
                    raise ValueError('Invalid blob digest')
            weights = tuple(x['digest'] for x in layers if x.get('mediaType') == 'application/vnd.ollama.image.model')
            key = weights or (str(manifest),)
            registry, namespace, model, tag = manifest.relative_to(root / 'manifests').parts
            name = ('' if registry == 'registry.ollama.ai' else registry + '/')
            name += ('' if namespace == 'library' else namespace + '/') + model + ':' + tag
            group = groups.setdefault(key, {'names': [], 'paths': [], 'mismatched': []})
            group['names'].append(name)
            for layer in layers:
                p = root / 'blobs' / layer['digest'].replace(':', '-')
                group['paths'].append(p)
                try:
                    if p.stat().st_size != layer['size']:
                        group['mismatched'].append(str(p))
                except FileNotFoundError:
                    pass
        for group in groups.values():
            r = row(group['names'][0], 'Ollama', group['paths'], aliases=group['names'],
                    mismatched=group['mismatched'])
            if group['mismatched']:
                r.update(state='missing', bytes=None)
            rows.append(r)
    elif source['type'] == 'gguf':
        if not root.is_dir():
            raise OSError('Model directory unavailable')
        for p in sorted(root.glob('*/*/*.gguf')):
            rows.append(row(p.name, 'LM Studio', [p]))
    else:
        raise ValueError('Unknown registered source type')
    # Unique referenced paths within a source; aliases do not inflate totals.
    unique = {f['path']: f['bytes'] for r in rows for f in r['files']}
    return {**source, 'rows': rows, 'state': 'metadata', 'checked_at': time.time(),
            'filesystem': fs, 'bytes': sum(unique.values()) if all(r['state'] == 'metadata' for r in rows) else None}


def refresh():
    global _running
    with _lock:
        if _running or (_worker is not None and _worker.poll() is None):
            return False
        _running = True
    threading.Thread(target=_refresh, daemon=True).start()
    return True


def _refresh():
    global _running, _worker
    try:
        sources = read(SOURCES, [])
        previous = {s['id']: s for s in snapshot().get('sources', [])}
        results = []
        for source in sources:
            # Keep a single outstanding process, even if SIGKILL cannot finish
            # until a hard-mounted NAS reconnects.
            if _worker is not None and _worker.poll() is None:
                result = {**previous.get(source['id'], source), 'state': 'unavailable', 'error': 'Previous filesystem read still pending'}
            else:
                _worker = subprocess.Popen([sys.executable, __file__, '--scan'], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    out, err = _worker.communicate(json.dumps(source), timeout=10)
                    if _worker.returncode:
                        raise ValueError(err.strip()[-500:])
                    result = json.loads(out)
                except (subprocess.TimeoutExpired, ValueError) as exc:
                    _worker.kill()
                    result = {**previous.get(source['id'], source), 'state': 'unavailable',
                              'error': 'Filesystem check timed out' if isinstance(exc, subprocess.TimeoutExpired) else str(exc)}
            result['attempted_at'] = time.time()
            results.append(result)
        with _lock:
            DATA.mkdir(exist_ok=True)
            tmp = CACHE.with_suffix('.tmp')
            tmp.write_text(json.dumps({'sources': results}, ensure_ascii=False, indent=2))
            tmp.replace(CACHE)
    finally:
        with _lock:
            _running = False


if __name__ == '__main__' and sys.argv[1:] == ['--scan']:
    try:
        print(json.dumps(scan(json.load(sys.stdin)), ensure_ascii=False))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        sys.exit(str(exc))
