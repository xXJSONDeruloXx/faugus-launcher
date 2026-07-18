import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "hv" / "hv_helper.py"
spec = importlib.util.spec_from_file_location("uwu_hv_helper", HELPER)
hv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hv)


class HvBackendTests(unittest.TestCase):
    def test_module_source_is_complete(self):
        source = ROOT / "hv" / "cpuid_fault_emulation"
        for relative in (
            "Makefile", "dkms.conf", "src/cpuid_fault_emulation.c",
            "src/capture_context.S", "src/run_vm.S",
            "inc/host_state.h", "inc/vmcb_layout.h",
        ):
            self.assertTrue((source / relative).is_file(), relative)
        self.assertIn("invlpg_tlbsync_enable", (source / "src/cpuid_fault_emulation.c").read_text())

    def test_grub_argument_is_reversible_and_idempotent(self):
        original = 'GRUB_TIMEOUT=5\nGRUB_CMDLINE_LINUX_DEFAULT="quiet splash"\n'
        applied = hv.replace_kernel_arg(original, "grub", True)
        self.assertIn('"quiet splash clearcpuid=514"', applied)
        self.assertEqual(hv.replace_kernel_arg(applied, "grub", True), applied)
        self.assertEqual(hv.replace_kernel_arg(applied, "grub", False), original)

    def test_systemd_boot_argument_is_reversible(self):
        original = "title Test\nlinux /vmlinuz\noptions quiet splash\n"
        applied = hv.replace_kernel_arg(original, "systemd-boot", True)
        self.assertIn("options quiet splash clearcpuid=514", applied)
        self.assertEqual(hv.replace_kernel_arg(applied, "systemd-boot", False), original)

    def test_reap_leases_handles_exit_and_pid_reuse(self):
        leases = {
            "alive": {"pid": 10, "uid": 1000, "starttime": "20", "game_id": "one"},
            "reused": {"pid": 11, "uid": 1000, "starttime": "21", "game_id": "two"},
            "gone": {"pid": 12, "uid": 1000, "starttime": "22", "game_id": "three"},
        }
        identities = {10: (1000, "20"), 11: (1000, "999")}
        result = hv.reap_leases(leases, identity=lambda pid: identities.get(pid))
        self.assertEqual(set(result), {"alive"})

    def test_runtime_protocol_rejects_other_users_and_pid_spoofing(self):
        with self.assertRaises(hv.BackendError):
            hv.validate_runtime_request({"action": "status"}, 100, 1001, 1000)
        with self.assertRaises(hv.BackendError):
            hv.validate_runtime_request(
                {"action": "acquire", "pid": 999, "starttime": "1", "game_id": "game"},
                100, 1000, 1000,
            )
        self.assertEqual(hv.validate_runtime_request({"action": "status"}, 100, 1000, 1000), "status")

    def test_privileged_paths_are_root_owned_namespaces(self):
        self.assertEqual(hv.STATE_DIR, Path("/var/lib/uwu-launcher"))
        self.assertEqual(hv.INSTALLED_ROOT, Path("/usr/lib/uwu-launcher"))
        self.assertEqual(hv.RUNTIME_SOCKET, Path("/run/uwu-launcher/hv-runtime.sock"))
        self.assertEqual(hv.SERVICE_NAME, "uwu-hv-runtime.service")


if __name__ == "__main__":
    unittest.main()
