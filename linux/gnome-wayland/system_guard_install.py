#!/usr/bin/python3
"""Install/remove the sleep boundary. Never starts or restarts a sleep unit.

Run with administrator authentication. Backups and failed deployments are retained
under /var/lib/sleep-disabler-guard; unrelated configuration is never overwritten.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

MARKER = '# Managed by Sleep Disabler system guard v1\n'
HELPER = Path('/usr/local/libexec/sleep-disabler/sleep_guard.py')
UNITS = tuple('systemd-' + name + '.service' for name in
              ('suspend', 'hibernate', 'hybrid-sleep', 'suspend-then-hibernate'))
DROPIN_NAME = '90-sleep-disabler-guard.conf'
CONDITION = '/usr/bin/python3 -I ' + str(HELPER)
DROPIN = (MARKER + '[Service]\nExecCondition=' + CONDITION + '\n').encode()


def protected(path):
    """Check each existing ancestor without following symlinks."""
    for item in reversed((path, *path.parents)):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f'Symlink in managed system path: {item}')
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError(f'System path is not exclusively administrator-owned: {item}')
        if item != path and not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f'Non-directory ancestor: {item}')


def reload_manager():
    subprocess.run(['/usr/bin/systemctl', 'daemon-reload'], check=True, timeout=30)


def verify_manager():
    result = subprocess.run(['/usr/bin/systemctl', 'show', *UNITS,
                             '--property=Id', '--property=ExecCondition',
                             '--property=NeedDaemonReload', '--property=SuccessExitStatus'],
                            check=True, capture_output=True, text=True, timeout=10)
    validate_loaded(result.stdout)


def validate_loaded(output):
    records = {}
    for section in output.strip().split('\n\n'):
        properties = dict(line.split('=', 1) for line in section.splitlines() if '=' in line)
        records[properties.get('Id')] = properties
    for unit in UNITS:
        properties = records.get(unit, {})
        if properties.get('NeedDaemonReload') != 'no':
            raise RuntimeError(f'Systemd configuration needs reloading for {unit}')
        if properties.get('SuccessExitStatus') not in ('', '0'):
            raise RuntimeError(f'Custom success exit status makes the guard unsafe for {unit}')
        expected = 'path=/usr/bin/python3 ; argv[]=' + CONDITION + ' ; ignore_errors=no ;'
        if expected not in properties.get('ExecCondition', ''):
            raise RuntimeError(f'Loaded sleep guard missing for {unit}')


def deploy(source, *, remove=False, root=Path('/'),
           reload=reload_manager, verify=verify_manager, check=protected):
    """root/reload/verify/check injection is for isolated filesystem tests only."""
    helper = root / HELPER.relative_to('/')
    targets = {helper: MARKER.encode() + source}
    targets.update({root / 'etc/systemd/system' / (unit + '.d') / DROPIN_NAME: DROPIN
                    for unit in UNITS})
    history = root / 'var/lib/sleep-disabler-guard'
    backup = history / uuid.uuid4().hex
    check(backup)
    if history.exists():
        for previous in history.iterdir():
            if previous.name == 'install.lock':
                continue
            check(previous)
            record = previous / 'transaction.json'
            try:
                outcome = json.loads(record.read_text())['status']
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise RuntimeError(f'Unfinished system guard transaction: {previous}') from error
            if outcome not in ('committed', 'rolled-back'):
                raise RuntimeError(f'Unfinished system guard transaction: {previous}')
    # Preflight the complete set before modifying a managed destination.
    for target in targets:
        check(target)
        if target.exists():
            if not target.is_file() or not target.read_bytes().startswith(MARKER.encode()):
                raise RuntimeError(f'Unrelated file at managed destination: {target}')
    backup.mkdir(parents=True, mode=0o700)
    def receipt(status):
        temporary = backup / ('receipt.' + uuid.uuid4().hex)
        with temporary.open('x') as stream:
            json.dump({'status': status, 'remove': remove,
                       'targets': [str(target) for target in targets]}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(backup / 'transaction.json')
        directory = os.open(backup, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    receipt('deploying')
    changed = []
    try:
        for index, (target, data) in enumerate(targets.items()):
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            check(target)
            original = backup / f'{index}.original'
            staged = backup / f'{index}.staged'
            if not remove:
                with staged.open('xb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                staged.chmod(0o644)
            existed = target.exists()
            if existed:
                target.rename(original)
            changed.append((index, target, existed))
            if not remove:
                staged.rename(target)
        reload()
        if not remove:
            verify()
        receipt('committed')
    except BaseException as failure:
        rollback_errors = []
        for index, target, existed in reversed(changed):
            try:
                if target.exists():
                    target.rename(backup / f'{index}.failed')
                if existed:
                    (backup / f'{index}.original').rename(target)
            except OSError as error:
                rollback_errors.append(str(error))
        try:
            reload()
        except Exception as error:
            rollback_errors.append(str(error))
        if rollback_errors:
            receipt('rollback-incomplete')
            raise RuntimeError(f'Deployment failed: {failure}; rollback incomplete: '
                               f'{rollback_errors}; retained at {backup}') from failure
        receipt('rolled-back')
        raise
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Administrator authentication is required (sudo or pkexec).')
    source = b''
    if not args.remove:
        # This dependency probe only imports modules; it never contacts login1.
        subprocess.run(['/usr/bin/python3', '-I', '-c', 'import dbus'], check=True)
        source = Path(__file__).with_name('sleep_guard.py').read_bytes()
        compile(source, 'sleep_guard.py', 'exec')
    lock = Path('/var/lib/sleep-disabler-guard/install.lock')
    protected(lock)
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        backup = deploy(source, remove=args.remove)
    print(f'System sleep guard {"removed" if args.remove else "installed"}; '
          f'backups retained at {backup}. No sleep unit was started.')


if __name__ == '__main__':
    main()
