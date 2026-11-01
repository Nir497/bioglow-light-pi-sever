import os
import subprocess
import unittest

REPO_ROOT = os.path.abspath(os.path.dirname(__file__))
INSTALL_SCRIPT = os.path.join(REPO_ROOT, "scripts", "install_service.sh")
UNINSTALL_SCRIPT = os.path.join(REPO_ROOT, "scripts", "uninstall_service.sh")


class ServiceScriptTests(unittest.TestCase):
    def test_scripts_exist_and_executable(self):
        self.assertTrue(os.path.isfile(INSTALL_SCRIPT), "install_service.sh missing")
        self.assertTrue(os.path.isfile(UNINSTALL_SCRIPT), "uninstall_service.sh missing")
        self.assertTrue(os.access(INSTALL_SCRIPT, os.X_OK), "install_service.sh is not executable")
        self.assertTrue(os.access(UNINSTALL_SCRIPT, os.X_OK), "uninstall_service.sh is not executable")

    def test_bash_syntax_validity(self):
        res_install = subprocess.run(["bash", "-n", INSTALL_SCRIPT], capture_output=True, text=True)
        self.assertEqual(res_install.returncode, 0, f"install_service.sh syntax error: {res_install.stderr}")

        res_uninstall = subprocess.run(["bash", "-n", UNINSTALL_SCRIPT], capture_output=True, text=True)
        self.assertEqual(res_uninstall.returncode, 0, f"uninstall_service.sh syntax error: {res_uninstall.stderr}")

    def test_install_script_dry_run_output(self):
        cmd = [INSTALL_SCRIPT, "--dry-run", "--port", "8000"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout

        self.assertIn("[Unit]", out)
        self.assertIn("Description=BioGlow Light & Sensor Dashboard Service", out)
        self.assertIn("After=network.target network-online.target sound.target", out)
        self.assertIn(f"WorkingDirectory={REPO_ROOT}", out)
        self.assertIn("birdcast_service.py --port 8000", out)
        self.assertIn("Restart=always", out)
        self.assertIn("WantedBy=multi-user.target", out)

    def test_install_script_dry_run_custom_options(self):
        cmd = [
            INSTALL_SCRIPT,
            "--dry-run",
            "--port", "8888",
            "--no-animal-analysis",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = res.stdout

        self.assertIn("--port 8888 --no-animal-analysis", out)

    def test_uninstall_script_dry_run(self):
        cmd = [UNINSTALL_SCRIPT, "--dry-run"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("systemctl stop bioglow.service", res.stdout)
        self.assertIn("systemctl disable bioglow.service", res.stdout)
        self.assertIn("/etc/systemd/system/bioglow.service", res.stdout)


if __name__ == "__main__":
    unittest.main()
