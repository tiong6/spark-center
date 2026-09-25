#!/usr/bin/python3 -I
"""固定 NodeSource 來源的兩階段升級。preview/status 唯讀；變更只由 pkexec 啟動。

狀態與舊版放 root 擁有的目錄；不接受任意來源、路徑或 shell 指令。
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile

import apt

SOURCE = Path('/etc/apt/sources.list.d/nodesource.sources')
STORE = Path('/var/lib/spark-center/node-source')
ENV = dict(os.environ, LC_ALL='C.UTF-8', LANG='C.UTF-8')
APT = ['/usr/bin/apt-get', '-o', 'Dpkg::Options::=--force-confold', '--no-remove']


def fail(key):
    raise RuntimeError(key)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def execute(cmd, *, capture=True, cwd=None):
    if not capture:
        print('$ ' + shlex.join(cmd), flush=True)
    r = subprocess.run(cmd, capture_output=capture, text=True, env=ENV,
                       stdin=subprocess.DEVNULL, cwd=cwd)
    if r.returncode:
        if capture:
            raise RuntimeError('node_command_failed:' + (r.stderr or r.stdout)[-2000:])
        fail('node_command_failed')
    return r.stdout if capture else ''


def atomic(path, text):
    fd, tmp = tempfile.mkstemp(prefix='.node-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def state_load():
    p = STORE / 'state.json'
    return json.loads(p.read_text()) if p.exists() else None


def state_save(s):
    atomic(STORE / 'state.json', json.dumps(s, indent=2))


def source_read():
    if SOURCE.is_symlink() or not SOURCE.is_file():
        fail('node_source_unsupported')
    text = SOURCE.read_text()
    fields = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        if ':' not in line or line[:1].isspace():
            fail('node_source_unsupported')
        k, v = line.split(':', 1)
        if k in fields:
            fail('node_source_unsupported')
        fields[k] = v.strip()
    arch = execute(['/usr/bin/dpkg', '--print-architecture']).strip()
    match = re.fullmatch(r'https://deb\.nodesource\.com/node_(\d+)\.x/?', fields.get('URIs', ''))
    if (not match or fields.get('Types') != 'deb' or fields.get('Suites') != 'nodistro'
            or fields.get('Components') != 'main' or fields.get('Architectures') != arch
            or fields.get('Signed-By') != '/usr/share/keyrings/nodesource.gpg'
            or set(fields) != {'Types', 'URIs', 'Suites', 'Components', 'Architectures', 'Signed-By'}
            or not Path(fields['Signed-By']).is_file()):
        fail('node_source_unsupported')
    # 只處理一份來源；多來源或 pin 的歧義不能靠字串替換猜。
    others = list(SOURCE.parent.glob('*.list')) + list(SOURCE.parent.glob('*.sources'))
    others.append(SOURCE.parent.parent / 'sources.list')
    for p in others:
        if p != SOURCE and p.is_file() and any('deb.nodesource.com' in l for l in p.read_text().splitlines() if not l.lstrip().startswith('#')):
            fail('node_source_unsupported')
    return text, int(match[1]), arch


def installed():
    cache = apt.Cache()
    p = cache.get('nodejs')
    if not p or not p.installed or 'nodesource' not in p.installed.version:
        fail('node_source_unsupported')
    return p


def fully_installed(p):
    """dpkg 真的裝完設定完：unpacked／half-configured 都不算。"""
    try:
        return p._pkg.current_state == apt.apt_pkg.CURSTATE_INSTALLED and p._pkg.inst_state == apt.apt_pkg.INSTSTATE_OK
    except AttributeError:
        return False


def effective_state(s, p, source_text):
    """完成狀態只在這裡判定，status／preview／apt 鎖都用同一個結果。
    來源已切到目標、目標版本已完整裝好（可能是從 apt 清單裝的，不是本流程裝的）→ 視為 installed，記 external。
    半途（unpacked、設定失敗）不算完成，失敗提示要留著。"""
    if not s:
        return None
    s = dict(s)
    if (s.get('phase') in ('prepared', 'install_failed') and source_text == s.get('replacement')
            and p.installed.version.startswith(str(s.get('target')) + '.') and fully_installed(p)):
        s['raw_phase'] = s['phase']
        s['phase'] = 'installed'
        s['external'] = True
    return s


def status():
    text, major, arch = source_read()
    p = installed()
    s = effective_state(state_load(), p, text)
    return {'source': str(SOURCE), 'source_text': text, 'major': major, 'arch': arch,
            'installed': p.installed.version, 'fully_installed': fully_installed(p), 'state': s}


def verify_backup(s):
    p = STORE / 'previous.deb'
    if not p.is_file() or digest(p.read_bytes()) != s['sha256']:
        fail('node_backup_invalid')
    return str(p)


def target_version(major):
    p = installed()
    v = p.candidate
    if not v or not v.version.startswith(str(major) + '.') or not any(
            o.site == 'deb.nodesource.com' and o.trusted for o in v.origins):
        fail('node_target_unavailable')
    return v.version


def simulation_changes(output):
    # apt 的非 root NOTE 與下載摘要不是交易內容；保留套件、版本與順序。
    return '\n'.join(line for line in output.splitlines() if re.match(r'^(Inst|Conf|Remv)\s', line))


def preview(action, target):
    st = status()
    s = st['state']
    plan = {'action': action, 'target': target, 'source': st['source'],
            'before': st['source_text'], 'installed': st['installed'], 'simulation': ''}
    if action == 'prepare':
        if s and s['phase'] != 'restored' and not (s['phase'] == 'installed' and st['source_text'] == s['replacement']):
            fail('node_pending')
        if not 20 <= target <= 99 or target <= st['major'] or not st['installed'].startswith(str(st['major']) + '.'):
            fail('node_target_unavailable')
        plan['after'] = st['source_text'].replace('node_' + str(st['major']) + '.x', 'node_' + str(target) + '.x')
    elif action in ('install', 'restore'):
        if not s or s['phase'] == 'restored':
            fail('node_no_pending')
        if st['source_text'] not in (s['original'], s['replacement']):
            fail('node_source_changed')
        plan['after'] = s['replacement'] if action == 'install' else s['original']
        if action == 'install':
            if st['source_text'] != s['replacement'] or s['phase'] not in ('prepared', 'installing', 'install_failed'):
                fail('node_no_pending')
            verify_backup(s)
            plan['version'] = target_version(s['target'])
            p = installed()
            # 目標版本已解包但沒設定完：apt 會說「已是最新」什麼都不做，要 --reinstall 才會重跑 dpkg
            args = (['--reinstall'] if p.installed.version == plan['version'] and not fully_installed(p) else []) + ['nodejs=' + plan['version']]
        else:
            plan['version'] = s['old_version']
            args = [verify_backup(s)] if st['installed'] != s['old_version'] else []
        if args:
            plan['args'] = args
            plan['simulation'] = execute(APT + ['-s', 'install', '--allow-downgrades'] + args)
    else:
        fail('node_bad_action')
    canonical = dict(plan, simulation=simulation_changes(plan['simulation']))
    plan['token'] = digest(json.dumps(canonical, sort_keys=True).encode())
    return plan


def refresh():
    # 只更新這一份來源，--error-on=any 不把失敗當成「使用舊索引也成功」。
    execute(['/usr/bin/apt-get', 'update', '--error-on=any', '-o', 'Dir::Etc::sourcelist=' + str(SOURCE),
             '-o', 'Dir::Etc::sourceparts=-', '-o', 'APT::Get::List-Cleanup=0'], capture=False)


def backup():
    v = installed().installed
    if not v.sha256 or not any(o.trusted for o in v.origins) or v.size > 150 * 1024 * 1024:
        fail('node_backup_invalid')
    with tempfile.TemporaryDirectory(prefix='download-', dir=STORE) as tmp:
        execute(['/usr/bin/apt-get', 'download', 'nodejs=' + v.version], capture=False, cwd=tmp)
        files = list(Path(tmp).glob('*.deb'))
        if len(files) != 1 or digest(files[0].read_bytes()) != v.sha256:
            fail('node_backup_invalid')
        os.replace(files[0], STORE / 'previous.deb')
        os.chmod(STORE / 'previous.deb', 0o644)
    return v.version, v.sha256


def mutate(action, target, token):
    if os.geteuid() != 0:
        fail('node_root_required')
    STORE.mkdir(parents=True, exist_ok=True, mode=0o755)
    with open(STORE / 'lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = preview(action, target)
        if plan['token'] != token:
            fail('node_plan_changed')
        if action == 'prepare':
            # effective_state 判定上一次已完成（含從 apt 清單裝完的），preview 才會放行到這裡；舊紀錄直接被新的取代
            old, sha = backup()   # 失敗就不碰來源；快取與來源都仍是舊的。
            if SOURCE.read_text() != plan['before']:
                fail('node_source_changed')
            s = {'phase': 'preparing', 'original': plan['before'], 'replacement': plan['after'],
                 'old_version': old, 'sha256': sha, 'target': target}
            state_save(s)   # 中斷後也能從面板復原。
            try:
                atomic(SOURCE, s['replacement'])
                refresh()
                target_version(target)   # 確認簽章可信的目標架構版本真的可用。
                s['phase'] = 'prepared'
                state_save(s)
            except Exception:
                atomic(SOURCE, s['original'])
                s['phase'] = 'restore_failed'
                state_save(s)
                refresh()
                s['phase'] = 'restored'
                state_save(s)
                raise
        else:
            s = state_load()
            s['phase'] = 'installing' if action == 'install' else 'restoring'
            state_save(s)
            try:
                if action == 'restore':
                    # 先修正來源，避免降回後被舊目標來源再次升級。
                    atomic(SOURCE, s['original'])
                    refresh()
                if plan.get('args'):
                    # 來源恢復後再檢查一次，不允許解依賴時悄悄移除其他套件。
                    if simulation_changes(execute(APT + ['-s', 'install', '--allow-downgrades'] + plan['args'])) != simulation_changes(plan['simulation']):
                        fail('node_plan_changed')
                    execute(APT + ['install', '-y', '--allow-downgrades'] + plan['args'], capture=False)
                if installed().installed.version != plan['version']:
                    fail('node_version_mismatch')
                s['phase'] = 'installed' if action == 'install' else 'restored'
                state_save(s)
            except Exception:
                s['phase'] = 'install_failed' if action == 'install' else 'restore_failed'
                state_save(s)
                raise


def main():
    try:
        if sys.argv[1:] == ['status']:
            print(json.dumps({'ok': True, **status()}))
        elif len(sys.argv) == 4 and sys.argv[1] == 'preview':
            print(json.dumps({'ok': True, **preview(sys.argv[2], int(sys.argv[3]))}))
        elif len(sys.argv) == 4 and sys.argv[1] in ('prepare', 'install', 'restore'):
            mutate(sys.argv[1], int(sys.argv[2]), sys.argv[3])
        else:
            fail('node_bad_action')
    except Exception as e:
        detail = str(e)
        if sys.argv[1:2] in (['status'], ['preview']):
            print(json.dumps({'ok': False, 'error': detail}))
        else:
            print('NODE_ERROR:' + detail, flush=True)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
