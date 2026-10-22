#!/usr/bin/python3
"""Check real ExecCondition semantics using only harmless user units.

Never starts a system unit or sleep command. The production guard receives a
fake inhibitor list. The only ExecStart writes a marker inside a temporary dir.
"""

from pathlib import Path
import subprocess
import sys
import tempfile
import uuid


def main():
    source = Path(__file__).resolve().parents[1] / 'sleep_guard.py'
    with tempfile.TemporaryDirectory(prefix='sleep-disabler-guard-check-') as directory:
        root = Path(directory)
        condition = root / 'condition.py'
        condition.write_text('''import importlib.util
import sys
spec = importlib.util.spec_from_file_location('guard', sys.argv[1])
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)
def fake_read():
    if sys.argv[2] == 'unknown':
        raise TimeoutError('simulated unavailable bus')
    if sys.argv[2] == 'blocked':
        return [('sleep', 'Sleep Disabler', 'Keep this session awake', 'block', 1000, 42)]
    return []
guard.read_inhibitors = fake_read
sys.exit(guard.main())
''')
        action = root / 'action.py'
        action.write_text('from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text("ran")\n')
        # Simulates prevention on, off, unavailable state, then a fresh release.
        for scenario in ('blocked', 'released', 'unknown', 'released'):
            marker = root / uuid.uuid4().hex
            unit = 'sleep-disabler-guard-check-' + uuid.uuid4().hex
            # The paths are generated under /tmp without spaces or quoting chars.
            command = ['systemd-run', '--user', '--wait', '--collect', '--quiet',
                       '--unit=' + unit, '--property=Type=oneshot',
                       '--property=ExecCondition=' + ' '.join([
                           sys.executable, '-I', str(condition), str(source), scenario]),
                       sys.executable, '-I', str(action), str(marker)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=20)
            expected = scenario == 'released'
            if marker.exists() != expected or (expected and result.returncode != 0):
                raise AssertionError(f'{scenario}: marker={marker.exists()}, '
                                     f'exit={result.returncode}, {result.stderr}')
            if scenario == 'unknown' and result.returncode == 0:
                raise AssertionError('Unknown state must fail the harmless unit')
            print(f'{scenario}: ExecStart marker {"present" if expected else "absent"}; '
                  f'unit exit {result.returncode}')
    print('Four harmless user-unit cases passed; no sleep service was started.')


if __name__ == '__main__':
    main()
