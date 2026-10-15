"""Wi-Fi controls for this DietPi's ifupdown/wpa_supplicant/DHCP setup.

Never reads credentials or writes the supplicant configuration. All operations
are bounded; the UI calls them on a worker. Recovery and manual switching share
a process lock so they cannot restart DHCP underneath one another.
"""
import contextlib
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import subprocess
import threading
import time

RUN_DIR = Path('/run/calc-wifi')
LOG_DIR = Path('/var/lib/calc-network')
IFACE = 'wlan0'


def command(args, timeout=5):
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise RuntimeError(f'{args[0]} timed out') from None
    if proc.returncode:
        # Avoid echoing command output into the UI/log, even on failure.
        raise RuntimeError(f'{args[0]} failed ({proc.returncode})')
    # Trailing TAB is an empty flags column in LIST_NETWORKS; keep it.
    return out.rstrip('\r\n')


@contextlib.contextmanager
def network_lock():
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    with (RUN_DIR / 'control.lock').open('a') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Network operation already in progress') from None
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def event_log():
    logger = logging.getLogger('calc-wifi')
    if not logger.handlers:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(LOG_DIR / 'events.log', maxBytes=262144,
                                      backupCount=2)
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def update_recovery_state(**values):
    """Caller holds network_lock; keep journal cursor and recovery metadata."""
    path = RUN_DIR / 'state.json'
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = {}
    state.update(values)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


class WifiBackend:
    def wpa(self, *args):
        out = command(['wpa_cli', '-i', IFACE, *map(str, args)])
        if out.startswith('FAIL') or 'Could not connect' in out:
            raise RuntimeError('Wi-Fi control unavailable')
        return out

    def status(self):
        values = dict(line.split('=', 1) for line in self.wpa('status').splitlines()
                      if '=' in line)
        addresses = json.loads(command(['ip', '-j', '-4', 'addr', 'show', 'dev', IFACE]))
        address = next((a['local'] for link in addresses for a in link.get('addr_info', [])
                        if a.get('scope') == 'global'), '')
        route = command(['ip', '-4', 'route', 'show', 'default', 'dev', IFACE])
        state = values.get('wpa_state', 'UNKNOWN')
        return dict(ssid=values.get('ssid', ''), id=values.get('id', ''),
                    state=state, ip=address, route=bool(route),
                    ready=state == 'COMPLETED' and bool(address) and bool(route))

    def profiles(self):
        profiles = []
        for line in self.wpa('list_networks').splitlines():
            fields = line.split('\t')
            if len(fields) >= 4 and fields[0].isdigit():
                profiles.append(dict(id=fields[0], ssid=fields[1],
                                     disabled='[DISABLED]' in fields[3]))
        return profiles

    def scan(self):
        # Scan completion is asynchronous; subsequent menu refreshes read it.
        self.wpa('scan')

    def visible(self):
        signals = {}
        for line in self.wpa('scan_results').splitlines():
            fields = line.split('\t', 4)
            if len(fields) == 5:
                try:
                    signals[fields[4]] = max(int(fields[2]), signals.get(fields[4], -999))
                except ValueError:
                    pass
        return signals

    def renew_dhcp(self):
        # One client, using the exact pid/lease paths owned by ifupdown. Do not
        # send RELEASE to the old network; remove its stale address locally.
        command(['systemctl', 'stop', 'calc-wifi-dhcp.service'], 12)
        command(['dhclient', '-x', '-pf', '/run/dhclient.wlan0.pid', IFACE], 8)
        command(['ip', '-4', 'addr', 'flush', 'dev', IFACE, 'scope', 'global'])
        command(['ip', '-4', 'route', 'flush', 'dev', IFACE])
        # A separate unit owns the daemon. Otherwise closing foot/restarting
        # the UI (or finishing a oneshot recovery) could kill the new client.
        command(['systemctl', 'start', 'calc-wifi-dhcp.service'], 15)

    def restart_interface(self):
        # Only for a prolonged failure or an explicit manual reconnect.
        # Reuse the OS-owned hotplug unit; do not spawn competing daemons.
        command(['systemctl', 'restart', '--no-block', f'ifup@{IFACE}.service'])

    def connect(self, network_id):
        profiles = self.profiles()
        if network_id not in {p['id'] for p in profiles}:
            raise RuntimeError('Saved network no longer exists; refresh')
        old = self.status()
        if old['id'] == network_id and old['ready']:
            return 'Already connected'
        # Prefer the chosen profile without SELECT_NETWORK, which disables
        # every other profile. Fallback must survive even an app crash/restart.
        priorities = {p['id']: int(self.wpa('get_network', p['id'], 'priority'))
                      for p in profiles}
        try:
            self.wpa('set_network', network_id, 'priority', max(priorities.values()) + 1)
            self.wpa('enable_network', network_id)
            self.wpa('reassociate')
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                state = self.status()
                if state['state'] == 'COMPLETED' and state['id'] == network_id:
                    update_recovery_state(ssid=state['ssid'], pending_dhcp=True)
                    self.renew_dhcp()
                    update_recovery_state(pending_dhcp=False)
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        connected = self.status()
                        if connected['ready'] and connected['id'] == network_id:
                            return 'Connected'
                        time.sleep(1)
                    raise RuntimeError('Wi-Fi joined, but DHCP timed out; auto-recovery continues')
                time.sleep(1)
            raise RuntimeError('Network unavailable or authentication failed; saved networks restored')
        finally:
            try:
                self.wpa('set_network', network_id, 'priority', priorities[network_id])
            except (OSError, RuntimeError):
                # All other profiles remain enabled even if control vanishes.
                # Persistent configuration was never changed.
                event_log().warning('Could not restore transient priority for profile %s', network_id)


class WifiController:
    """Thread-safe snapshots; curses never waits for a command or network."""
    def __init__(self, backend=None):
        self.backend = backend or WifiBackend()
        self.lock = threading.Lock()
        self.worker = None
        self.current_action = None
        self.queued = None
        self.updated = 0
        self.data = dict(state='Checking Wi-Fi', ip='', ssid='', ready=False,
                         profiles=[], signals={}, message='Checking saved networks...')

    def snapshot(self):
        with self.lock:
            return dict(self.data, busy=bool(self.worker and self.worker.is_alive()))

    def start(self, action='refresh', network_id=None):
        with self.lock:
            if self.worker and self.worker.is_alive():
                if action != 'refresh' and self.current_action == 'refresh' and not self.queued:
                    self.queued = (action, network_id)
                    self.data['message'] = 'Waiting for status check; network action queued'
                    return True
                return False
            self._start_locked(action, network_id)
        return True

    def _start_locked(self, action, network_id):
        self.updated = time.monotonic()
        self.current_action = action
        if action != 'refresh':
            self.data['message'] = {'connect': 'Connecting; F2 returns to chat',
                                    'reconnect': 'Reconnecting; F2 returns to chat',
                                    'scan': 'Scanning...'}[action]
        self.worker = threading.Thread(target=self._work, args=(action, network_id), daemon=True)
        self.worker.start()

    def _work(self, action, network_id):
        message = None
        try:
            if action != 'refresh':
                with network_lock():
                    if action == 'connect':
                        message = self.backend.connect(network_id)
                        # Prevent recovery from renewing this new lease again
                        # merely because it last observed the previous SSID.
                        update_recovery_state(ssid=self.backend.status()['ssid'], failed_since=None,
                                              attempts=0, next_attempt=0, pending_dhcp=False)
                    elif action == 'reconnect':
                        self.backend.restart_interface()
                        message = 'Reconnect started; waiting for Wi-Fi/DHCP'
                    elif action == 'scan':
                        self.backend.scan()
                        message = 'Scan requested'
            status = self.backend.status()
            profiles = self.backend.profiles()
            signals = self.backend.visible()
            with self.lock:
                self.data.update(status, profiles=profiles, signals=signals)
                if message:
                    self.data['message'] = message
                elif self.data['message'] == 'Checking saved networks...':
                    self.data['message'] = 'Select a saved network, or continue offline'
        except (OSError, RuntimeError, ValueError) as exc:
            with self.lock:
                self.data.update(message=str(exc), ready=False, state='Check/retry Wi-Fi')
        finally:
            if action not in ('refresh', 'scan'):
                try:
                    event_log().info('Manual %s: %s', action, self.snapshot()['message'])
                except OSError:
                    pass
            with self.lock:
                if self.queued:
                    queued = self.queued
                    self.queued = None
                    self._start_locked(*queued)


def recovery_step(backend, state, now):
    """Conservative escalation. Healthy Wi-Fi is never restarted or scanned."""
    try:
        status = backend.status()
    except (OSError, RuntimeError, ValueError):
        status = dict(ready=False, state='CONTROL_UNAVAILABLE', ssid='', ip='')
    previous_ssid = state.get('ssid', '')
    changed_network = bool(previous_ssid and status['ssid'] and
                           previous_ssid != status['ssid'] and status['state'] == 'COMPLETED')
    if (changed_network or state.get('pending_dhcp')) and status['state'] == 'COMPLETED':
        # A new AP/subnet can leave the old lease looking healthy. Renew once.
        if not changed_network and now < state.get('next_dhcp', 0):
            return state
        state['ssid'] = status['ssid']
        state.update(pending_dhcp=True, next_dhcp=now + 90)
        backend.renew_dhcp()
        state['pending_dhcp'] = False
        status = backend.status()
    if status['ssid']:
        state['ssid'] = status['ssid']
    label = f"{status['state']} ip={status['ip'] or 'none'}"
    if state.get('label') != label:
        event_log().info('State: %s', label)
        state['label'] = label
    if status['ready']:
        state.update(failed_since=None, attempts=0, next_attempt=0)
        return state
    if state.get('failed_since') is None:
        state['failed_since'] = now
    if now - state['failed_since'] < 90 or now < state.get('next_attempt', 0):
        return state
    if command(['systemctl', 'show', f'ifup@{IFACE}.service',
                '--property=ActiveState', '--value']) == 'activating':
        # Boot's ifup may still be negotiating DHCP. Never fight its job.
        return state
    attempt = state.get('attempts', 0)
    # First retry keeps the supplicant running; later retries repair the stack.
    state.update(attempts=attempt + 1, next_attempt=now + min(300, 90 * (attempt + 1)))
    if attempt == 0 and status['state'] != 'CONTROL_UNAVAILABLE':
        event_log().info('Recovery: reassociate / renew DHCP')
        if status['state'] == 'COMPLETED':
            backend.renew_dhcp()
        else:
            backend.wpa('reassociate')
    else:
        event_log().info('Recovery: restart failed interface (attempt %s)', attempt + 1)
        backend.restart_interface()
    return state


def retain_network_journal(state):
    """Keep relevant OS events across DietPi's RAM-log resets/reboots."""
    args = ['journalctl', '-b', '--no-pager', '-o', 'json', '-n', '300',
            '_TRANSPORT=kernel', '+', 'SYSLOG_IDENTIFIER=wpa_supplicant', '+',
            'SYSLOG_IDENTIFIER=dhclient']
    if state.get('cursor'):
        args[1:1] = ['--after-cursor', state['cursor']]
    rows = command(args, 8).splitlines()
    for row in rows:
        entry = json.loads(row)
        message = entry.get('MESSAGE', '')
        if isinstance(message, str) and any(word in message.lower() for word in
                ('wlan0', 'brcmfmac', 'dhcp', 'bound to', 'voltage', 'timed out')):
            source_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(
                int(entry.get('__REALTIME_TIMESTAMP', '0')) / 1000000))
            event_log().info('OS boot=%s at %s: %s', entry.get('_BOOT_ID', '')[:8],
                             source_time, message.replace('\n', ' ')[:500])
        state['cursor'] = entry.get('__CURSOR', state.get('cursor'))


def recover_once():
    with network_lock():
        path = RUN_DIR / 'state.json'
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        try:
            retain_network_journal(state)
        except (OSError, RuntimeError, ValueError) as exc:
            event_log().warning('Journal capture error: %s', exc)
        try:
            recovery_step(WifiBackend(), state, time.monotonic())
        except (OSError, RuntimeError, ValueError) as exc:
            event_log().warning('Recovery error: %s', exc)
        finally:
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(state))
            tmp.replace(path)


if __name__ == '__main__':
    try:
        recover_once()
    except RuntimeError as exc:
        # A manual switch holds the lock; let the next timer tick check again.
        print(exc)
