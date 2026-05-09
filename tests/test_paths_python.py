import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from server import _paths
from server.services import chat_retry


def _touch_python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env python\n", "utf-8")
    return path


class PythonDiscoveryTests(unittest.TestCase):
    def test_discover_user_python_prefers_env(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            env_python = _touch_python(root / "env-python")
            ga_root = root / "GenericAgent"
            _touch_python(ga_root / ".venv/bin/python3")

            with mock.patch.dict(_paths.os.environ, {"GA_PYTHON": str(env_python)}, clear=False), \
                 mock.patch.object(_paths, "load_config", return_value={}):
                self.assertEqual(_paths.discover_user_python(ga_root), str(env_python.resolve()))

    def test_discover_user_python_prefers_config_over_ga_venv(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            configured_python = _touch_python(root / "configured-python")
            ga_root = root / "GenericAgent"
            _touch_python(ga_root / ".venv/bin/python3")

            with mock.patch.dict(_paths.os.environ, {}, clear=True), \
                 mock.patch.object(_paths, "load_config", return_value={"python_path": str(configured_python)}):
                self.assertEqual(_paths.discover_user_python(ga_root), str(configured_python.resolve()))

    def test_discover_user_python_uses_ga_venv_before_path(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            ga_root = root / "GenericAgent"
            ga_python = _touch_python(ga_root / ".venv/bin/python3")

            with mock.patch.dict(_paths.os.environ, {}, clear=True), \
                 mock.patch.object(_paths, "load_config", return_value={}), \
                 mock.patch.object(_paths.shutil, "which", return_value=str(root / "system-python")):
                self.assertEqual(_paths.discover_user_python(ga_root), str(ga_python.resolve()))

    def test_discover_user_python_uses_known_location_before_path(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            known_python = _touch_python(root / "known/bin/python3")
            path_python = _touch_python(root / "path/bin/python3")

            with mock.patch.dict(_paths.os.environ, {}, clear=True), \
                 mock.patch.object(_paths, "load_config", return_value={}), \
                 mock.patch.object(_paths, "_ga_venv_python_candidates", return_value=[]), \
                 mock.patch.object(_paths, "_known_python_candidates", return_value=[str(known_python)]), \
                 mock.patch.object(_paths.shutil, "which", return_value=str(path_python)):
                self.assertEqual(_paths._discover_user_python_with_source()[0], str(known_python.resolve()))

    def test_set_ga_root_rejects_bad_python_path(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            admin_data = root / "admin-data"
            config_file = admin_data / "config.json"
            ga_root = root / "GenericAgent"
            (ga_root / "memory").mkdir(parents=True)
            (ga_root / "agentmain.py").write_text("", "utf-8")

            with mock.patch.object(_paths, "ADMIN_DATA", admin_data), \
                 mock.patch.object(_paths, "CONFIG_FILE", config_file):
                with self.assertRaisesRegex(ValueError, "Python 解释器不存在"):
                    _paths.set_ga_root(str(ga_root), str(root / "missing-python"))
                self.assertFalse(config_file.exists())

    def test_set_ga_root_saves_python_path(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            admin_data = root / "admin-data"
            config_file = admin_data / "config.json"
            ga_root = root / "GenericAgent"
            (ga_root / "memory").mkdir(parents=True)
            (ga_root / "agentmain.py").write_text("", "utf-8")
            python_path = _touch_python(root / "python")

            with mock.patch.object(_paths, "ADMIN_DATA", admin_data), \
                 mock.patch.object(_paths, "CONFIG_FILE", config_file):
                _paths.set_ga_root(str(ga_root), str(python_path))

            saved = json.loads(config_file.read_text("utf-8"))
            self.assertEqual(saved["ga_root"], str(ga_root.resolve()))
            self.assertEqual(saved["python_path"], str(python_path.resolve()))

    def test_bootstrap_sys_path_adds_external_python_site_packages(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            ga_root = root / "GenericAgent"
            (ga_root / "frontends").mkdir(parents=True)
            site_path = root / "site-packages"
            site_path.mkdir()

            original_sys_path = list(sys.path)
            try:
                with mock.patch.object(_paths, "external_python_site_paths", return_value=[str(site_path)]):
                    _paths.bootstrap_sys_path(ga_root)

                self.assertIn(str(ga_root.resolve()), sys.path)
                self.assertIn(str((ga_root / "frontends").resolve()), sys.path)
                self.assertIn(str(site_path), sys.path)
            finally:
                sys.path[:] = original_sys_path


class ChatRetryConfigTests(unittest.TestCase):
    def test_classify_recoverable_error_requires_final_ssl_marker(self):
        match = chat_retry.classify_recoverable_error("partial answer\n!!!Error: SSLError")
        self.assertIsNotNone(match)
        self.assertEqual(match.code, "ssl_error")

    def test_classify_recoverable_error_ignores_non_final_marker(self):
        self.assertIsNone(chat_retry.classify_recoverable_error("!!!Error: SSLError\nRecovered final text"))

    def test_normalize_chat_retry_config_clamps_attempts(self):
        cfg = chat_retry.normalize_chat_retry_config({"enabled": "false", "max_attempts": 99})
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.max_attempts, chat_retry.MAX_CONFIG_ATTEMPTS)

    def test_save_chat_retry_config_preserves_existing_admin_config(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            admin_data = root / "admin-data"
            config_file = admin_data / "config.json"
            admin_data.mkdir(parents=True)
            config_file.write_text(json.dumps({"ga_root": "/tmp/ga"}), "utf-8")

            with mock.patch.object(_paths, "ADMIN_DATA", admin_data), \
                 mock.patch.object(_paths, "CONFIG_FILE", config_file):
                saved = chat_retry.save_chat_retry_config({"enabled": False, "max_attempts": 3})

            self.assertEqual(saved.to_dict(), {"enabled": False, "max_attempts": 3})
            config = json.loads(config_file.read_text("utf-8"))
            self.assertEqual(config["ga_root"], "/tmp/ga")
            self.assertEqual(config[chat_retry.CONFIG_KEY], {"enabled": False, "max_attempts": 3})


if __name__ == "__main__":
    unittest.main()
