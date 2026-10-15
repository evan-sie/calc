"""Offline network failure/recovery checks; never touch real Wi-Fi."""
import collections
import contextlib
import curses
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wifi_manager as wifi
import casio_ai as app


def status(ready=True, ssid='Rawon', ident='1', state='COMPLETED'):
    return dict(ready=ready, ssid=ssid, id=ident, state=state,
                ip='192.168.50.125' if ready else '', route=ready)


class WifiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = [patch.object(wifi, 'RUN_DIR', Path(self.tmp.name)),
                   patch.object(wifi, 'event_log', return_value=MagicMock()),
                   patch.object(wifi, 'command', return_value='active'),
                   patch.object(socket.socket, 'connect', side_effect=AssertionError('No network'))]
        self.command = patches[2].start()
        self.addCleanup(patches[2].stop)
        for item in patches[:2] + patches[3:]:
            item.start()
            self.addCleanup(item.stop)
        self.backend = MagicMock()
        self.backend.status.return_value = status()

    def test_healthy_never_changes_network(self):
        state = dict(failed_since=0, attempts=5, next_attempt=0, ssid='Rawon')
        wifi.recovery_step(self.backend, state, 1000)
        self.backend.renew_dhcp.assert_not_called()
        self.backend.restart_interface.assert_not_called()
        self.backend.wpa.assert_not_called()
        self.command.assert_not_called()
        self.assertEqual(state['attempts'], 0)

    def test_slow_boot_dhcp_gets_grace_period(self):
        self.backend.status.return_value = status(False)
        state = wifi.recovery_step(self.backend, {}, 0)
        wifi.recovery_step(self.backend, state, 89)
        self.backend.renew_dhcp.assert_not_called()
        wifi.recovery_step(self.backend, state, 90)
        self.backend.renew_dhcp.assert_called_once()
        self.backend.restart_interface.assert_not_called()

    def test_missing_hotspot_retries_without_disabling_saved_networks(self):
        self.backend.status.return_value = status(False, ssid='', state='SCANNING')
        state = dict(failed_since=0)
        wifi.recovery_step(self.backend, state, 91)
        self.backend.wpa.assert_called_once_with('reassociate')
        wifi.recovery_step(self.backend, state, 100)
        self.backend.restart_interface.assert_not_called()
        wifi.recovery_step(self.backend, state, 182)
        self.backend.restart_interface.assert_called_once()

    def test_boot_ifup_job_is_not_interrupted(self):
        self.command.return_value = 'activating'
        self.backend.status.return_value = status(False)
        wifi.recovery_step(self.backend, dict(failed_since=0), 180)
        self.backend.renew_dhcp.assert_not_called()
        self.backend.restart_interface.assert_not_called()

    def test_automatic_hotspot_roam_renews_old_subnet(self):
        self.backend.status.return_value = status(True, 'iPhone', '0')
        state = dict(ssid='Rawon')
        wifi.recovery_step(self.backend, state, 300)
        wifi.recovery_step(self.backend, state, 320)
        self.backend.renew_dhcp.assert_called_once()

    def test_failed_roam_renewal_is_retried_even_with_stale_ip(self):
        self.backend.status.return_value = status(True, 'iPhone', '0')
        self.backend.renew_dhcp.side_effect = [RuntimeError('temporary failure'), None]
        state = dict(ssid='Rawon')
        with self.assertRaises(RuntimeError):
            wifi.recovery_step(self.backend, state, 300)
        self.assertTrue(state['pending_dhcp'])
        wifi.recovery_step(self.backend, state, 320)
        self.assertEqual(self.backend.renew_dhcp.call_count, 1)
        wifi.recovery_step(self.backend, state, 391)
        self.assertEqual(self.backend.renew_dhcp.call_count, 2)
        self.assertFalse(state['pending_dhcp'])

    def test_log_capture_failure_does_not_prevent_recovery(self):
        with patch.object(wifi, 'retain_network_journal', side_effect=RuntimeError('journal unavailable')), \
                patch.object(wifi, 'WifiBackend', return_value=self.backend):
            wifi.recover_once()
        self.backend.status.assert_called_once()
        self.assertTrue((Path(self.tmp.name) / 'state.json').exists())

    def test_control_failure_eventually_repairs_interface(self):
        self.backend.status.side_effect = RuntimeError('No supplicant')
        wifi.recovery_step(self.backend, dict(failed_since=0), 91)
        self.backend.restart_interface.assert_called_once()

    def test_dhcp_has_separate_systemd_owner(self):
        wifi.WifiBackend().renew_dhcp()
        commands = [call.args[0] for call in self.command.call_args_list]
        self.assertEqual(commands[0], ['systemctl', 'stop', 'calc-wifi-dhcp.service'])
        self.assertEqual(commands[-1], ['systemctl', 'start', 'calc-wifi-dhcp.service'])
        self.assertFalse(any('-r' in cmd for cmd in commands))

    def test_interface_repair_uses_existing_os_unit(self):
        wifi.WifiBackend().restart_interface()
        self.command.assert_called_once_with(['systemctl', 'restart', '--no-block', 'ifup@wlan0.service'])

    def test_address_without_authentication_is_not_healthy(self):
        self.command.side_effect = ['wpa_state=SCANNING', json.dumps([
            dict(addr_info=[dict(scope='global', local='192.168.50.125')])]), 'default via 192.168.50.1']
        self.assertFalse(wifi.WifiBackend().status()['ready'])

    def test_no_route_is_not_healthy(self):
        self.command.side_effect = ['wpa_state=COMPLETED\nssid=Rawon',
                                    '[{"addr_info":[{"scope":"global","local":"192.168.50.125"}]}]', '']
        self.assertFalse(wifi.WifiBackend().status()['ready'])

    def connect_backend(self):
        backend = wifi.WifiBackend()
        backend.profiles = MagicMock(return_value=[dict(id='0', ssid='iPhone', disabled=False),
                                                   dict(id='1', ssid='Rawon', disabled=False),
                                                   dict(id='2', ssid='Unused', disabled=True)])
        backend.wpa = MagicMock(side_effect=lambda *args: '0' if args[0] == 'get_network' else 'OK')
        backend.renew_dhcp = MagicMock()
        return backend

    def test_successful_manual_hotspot_switch_restores_fallback(self):
        backend = self.connect_backend()
        backend.status = MagicMock(side_effect=[status(), status(False, 'iPhone', '0'),
                                               status(True, 'iPhone', '0')])
        self.assertEqual(backend.connect('0'), 'Connected')
        backend.renew_dhcp.assert_called_once()
        calls = [c.args for c in backend.wpa.call_args_list]
        self.assertIn(('set_network', '0', 'priority', 1), calls)
        self.assertEqual(calls[-1], ('set_network', '0', 'priority', 0))
        self.assertFalse(any(c[0] in ('select_network', 'disable_network') for c in calls))
        self.assertFalse(any('save_config' in c for c in calls))

    def test_association_failure_restores_all_enabled_profiles(self):
        backend = self.connect_backend()
        backend.status = MagicMock(return_value=status())
        with patch.object(wifi.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaisesRegex(RuntimeError, 'unavailable'):
                backend.connect('0')
        backend.wpa.assert_any_call('set_network', '0', 'priority', 0)
        self.assertFalse(any(c.args[0] in ('select_network', 'disable_network') for c in backend.wpa.call_args_list))
        backend.renew_dhcp.assert_not_called()

    def test_dhcp_failure_restores_fallback_and_never_writes_config(self):
        backend = self.connect_backend()
        backend.status = MagicMock(side_effect=[status(), status(False, 'iPhone', '0')])
        backend.renew_dhcp.side_effect = RuntimeError('DHCP failed')
        with self.assertRaisesRegex(RuntimeError, 'DHCP'):
            backend.connect('0')
        backend.wpa.assert_any_call('set_network', '0', 'priority', 0)
        self.assertFalse(any(c.args[0] == 'save_config' for c in backend.wpa.call_args_list))

    def test_selecting_current_connection_is_noop(self):
        backend = self.connect_backend()
        backend.status = MagicMock(return_value=status())
        self.assertEqual(backend.connect('1'), 'Already connected')
        backend.wpa.assert_not_called()

    def test_restore_error_does_not_disable_fallback_profiles(self):
        backend = self.connect_backend()
        backend.status = MagicMock(side_effect=[status(), status(False, 'iPhone', '0'), status(True, 'iPhone', '0')])
        def wpa(*args):
            if args == ('set_network', '0', 'priority', 0):
                raise RuntimeError('failed')
            return '0' if args[0] == 'get_network' else 'OK'
        backend.wpa.side_effect = wpa
        backend.connect('0')
        self.assertFalse(any(c.args[0] in ('select_network', 'disable_network') for c in backend.wpa.call_args_list))

    def test_empty_flags_column_preserves_last_saved_profile(self):
        self.command.return_value = 'network id / ssid / bssid / flags\n0\tiPhone\tany\t[CURRENT]\n1\tRawon\tany\t'
        profiles = wifi.WifiBackend().profiles()
        self.assertEqual([p['ssid'] for p in profiles], ['iPhone', 'Rawon'])

    def test_controller_stays_responsive_during_blocked_operation(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.backend.restart_interface.side_effect = lambda: release.wait(2)
        controller = wifi.WifiController(self.backend)
        self.assertTrue(controller.start('reconnect'))
        before = time.monotonic()
        self.assertTrue(controller.snapshot()['busy'])
        self.assertFalse(controller.start('connect', '0'))
        self.assertLess(time.monotonic() - before, .1)
        release.set()
        controller.worker.join(1)
        self.assertFalse(controller.snapshot()['busy'])

    def test_network_lock_prevents_competing_repair(self):
        with wifi.network_lock():
            with self.assertRaisesRegex(RuntimeError, 'in progress'):
                with wifi.network_lock():
                    self.fail('Second lock should fail')

    def test_user_reconnect_is_queued_during_background_status_check(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def blocked_status():
            release.wait(2)
            return status()
        self.backend.status.side_effect = blocked_status
        controller = wifi.WifiController(self.backend)
        controller.start()
        self.assertTrue(controller.start('reconnect'))
        release.set()
        deadline = time.monotonic() + 2
        while controller.snapshot()['busy'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.backend.restart_interface.assert_called_once()

    def test_offline_menu_f2_exits_while_worker_busy_preserving_chat(self):
        controller = MagicMock()
        controller.snapshot.return_value = dict(state='SCANNING', ip='', ssid='', ready=False,
                profiles=[], signals={}, message='Waiting', busy=True)
        screen = MagicMock()
        screen.getmaxyx.return_value = (28, 99)
        screen.getch.return_value = curses.KEY_F2
        history = list(app.chat_history)
        with patch.object(app, 'wifi', controller), patch.object(app, 'stop_viewfinder'), \
                patch.object(app, 'STYLES', collections.defaultdict(int)):
            app.show_network_menu(screen)
        self.assertEqual(app.chat_history, history)

    def test_network_shortcut_is_independent_of_class_confirmation(self):
        with patch.object(app, 'sym_active', True), patch.object(app, 'context_confirm_active', True):
            self.assertTrue(app.network_shortcut(curses.KEY_F2))

    def test_help_keeps_ip_visible_on_real_17_row_display(self):
        with patch.object(app, 'get_wlan_ip', return_value='172.20.10.2'):
            lines = app.get_startup_splash_lines()
        self.assertLessEqual(len(lines), 13)
        self.assertTrue(any('SYM+F2' in line for line in lines))
        self.assertIn('IP      172.20.10.2', lines)


if __name__ == '__main__':
    unittest.main()
