"""Read-only verification of installed and loaded system sleep conditions."""
from pathlib import Path
import subprocess

from system_guard_install import DROPIN, DROPIN_NAME, HELPER, MARKER, UNITS, protected, validate_loaded


def inspect_guard(*, root=Path('/'), run=subprocess.run):
    try:
        helper = root / HELPER.relative_to('/')
        protected(helper)
        expected = MARKER.encode() + Path(__file__).with_name('sleep_guard.py').read_bytes()
        if helper.read_bytes() != expected:
            raise ValueError('System sleep guard version differs from this agent')
        for unit in UNITS:
            dropin = root / 'etc/systemd/system' / (unit + '.d') / DROPIN_NAME
            protected(dropin)
            if dropin.read_bytes() != DROPIN:
                raise ValueError(f'System sleep guard configuration differs for {unit}')
        result = run(['/usr/bin/systemctl', 'show', *UNITS, '--property=Id',
                      '--property=ExecCondition', '--property=NeedDaemonReload',
                      '--property=SuccessExitStatus'],
                     check=True, capture_output=True, text=True, timeout=8)
        validate_loaded(result.stdout)
        return 'verified', ''
    except Exception as error:
        return 'unavailable', str(error)
