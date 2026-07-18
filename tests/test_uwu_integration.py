import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TEST_ROOT = tempfile.TemporaryDirectory()
os.environ["HOME"] = _TEST_ROOT.name
os.environ["XDG_CONFIG_HOME"] = str(Path(_TEST_ROOT.name) / "config")
os.environ["XDG_DATA_HOME"] = str(Path(_TEST_ROOT.name) / "data")
os.environ["XDG_STATE_HOME"] = str(Path(_TEST_ROOT.name) / "state")

from faugus import hv_client
from faugus.config_manager import ConfigManager
from faugus.steam_setup import is_uwu_shortcut
from faugus.utils import GAME_FIELDS, prepare_game_kwargs


class UwUIntegrationTests(unittest.TestCase):
    def test_old_game_records_default_to_cpuid_enabled(self):
        values = prepare_game_kwargs({"gameid": "test", "title": "Test"})
        self.assertTrue(values["hv_enabled"])
        self.assertIn("hv_enabled", GAME_FIELDS)

    def test_explicit_cpuid_disable_is_preserved(self):
        values = prepare_game_kwargs({"gameid": "test", "hv_enabled": False})
        self.assertIs(values["hv_enabled"], False)

    def test_new_game_default_is_enabled(self):
        config = ConfigManager()
        self.assertEqual(config.config["hv-default"], "True")

    def test_steam_shortcut_ownership_does_not_claim_faugus(self):
        self.assertFalse(is_uwu_shortcut({
            "AppName": "Same title",
            "LaunchOptions": "--game same-title",
            "ShortcutPath": "",
        }))
        self.assertTrue(is_uwu_shortcut({
            "AppName": "Same title",
            "LaunchOptions": "--game same-title",
            "ShortcutPath": "uwu-launcher:same-title",
        }, "same-title"))

    def test_runtime_lease_acquires_and_releases_over_socket(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "runtime.sock"
            original = hv_client.RUNTIME_SOCKET
            hv_client.RUNTIME_SOCKET = socket_path
            requests = []
            ready = threading.Event()

            def server():
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(socket_path))
                listener.listen(2)
                ready.set()
                try:
                    for _ in range(2):
                        connection, _ = listener.accept()
                        with connection:
                            request = json.loads(connection.makefile("r", encoding="utf-8").readline())
                            requests.append(request)
                            response = {"ok": True, "loaded": request["action"] == "acquire"}
                            if request["action"] == "acquire":
                                response["token"] = "a" * 32
                            connection.sendall(json.dumps(response).encode() + b"\n")
                finally:
                    listener.close()

            thread = threading.Thread(target=server)
            thread.start()
            ready.wait(2)
            try:
                lease = hv_client.RuntimeLease("hosted-game").acquire()
                self.assertTrue(lease.active)
                lease.release()
                self.assertFalse(lease.active)
            finally:
                hv_client.RUNTIME_SOCKET = original
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual([request["action"] for request in requests], ["acquire", "release"])
            self.assertEqual(requests[0]["pid"], os.getpid())
            self.assertEqual(requests[0]["game_id"], "hosted-game")

    def test_package_identity_is_private(self):
        meson = (ROOT / "meson.build").read_text()
        self.assertIn("'uwu-launcher'", meson)
        self.assertIn("'uwu-launcher' / 'faugus'", (ROOT / "faugus/meson.build").read_text())
        self.assertNotIn("site-packages", (ROOT / "faugus/meson.build").read_text())
        wrapper = (ROOT / "uwu-launcher").read_text()
        self.assertIn("../lib/uwu-launcher", wrapper)


if __name__ == "__main__":
    unittest.main()
