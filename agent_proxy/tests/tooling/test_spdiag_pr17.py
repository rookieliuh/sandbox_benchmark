"""Offline checks of the isolated PR build workflow; no git/network/compiler runs."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_spdiag_pr17 as builder


class PRBuildTest(unittest.TestCase):
    def setUp(self):
        (ROOT / "test-results").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "test-results")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.commands = []
        self.revision = builder.REVISION
        self.dirty = False
        self.install = None
        self.old = self.root / ".deps/spdiag/usr/lib64/libspdiag.so.1"
        self.old.parent.mkdir(parents=True)
        self.old.write_bytes(b"original RPM")

    def child(self, command, **kwargs):
        self.commands.append(command)
        self.assertNotIn("DESTDIR", kwargs["env"])
        if "rev-parse" in command:
            kwargs["stdout"].write(self.revision + "\n")
        elif "status" in command and self.dirty:
            kwargs["stdout"].write(" M src/sdk/perf_point.cpp\n")
        if command[:2] == ["cmake", "-S"]:
            self.install = Path(next(x.split("=", 1)[1] for x in command
                                     if x.startswith("-DCMAKE_INSTALL_PREFIX=")))
        if command[:2] == ["cmake", "--install"]:
            for name in ("lib64/libspdiag.so.1", "bin/spdiag", "lib64/cmake/SpDiag/SpDiagConfig.cmake"):
                path = self.install / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"new build")
        return Mock(wait=Mock(return_value=0))

    def prepare(self):
        with patch.object(builder, "ROOT", self.root), \
                patch.object(builder.subprocess, "Popen", side_effect=self.child), \
                patch.dict(builder.os.environ, {"DESTDIR": "/unrelated"}), \
                redirect_stdout(io.StringIO()):
            return builder.prepare()

    def test_private_install_and_manifest_preserve_old_sdk(self):
        prefix, manifest = self.prepare()
        self.assertTrue(prefix.is_relative_to(self.root / ".deps"))
        self.assertNotEqual(prefix, self.root / ".deps/spdiag")
        self.assertEqual(self.old.read_bytes(), b"original RPM")
        self.assertEqual(manifest["source"]["commit"], builder.REVISION)
        self.assertEqual(manifest["files"]["usr/lib64/libspdiag.so.1"], builder.digest(self.install / "lib64/libspdiag.so.1"))
        self.assertEqual(json.loads((prefix / "agent-proxy-manifest.json").read_text()), manifest)
        build = next(c for c in self.commands if c[:2] == ["cmake", "--build"])
        self.assertEqual(build[-2:], ["--parallel", "1"])
        self.assertEqual(builder.OPTIONS["CMAKE_INSTALL_SYSCONFDIR"], "etc")

    def test_changed_pr_head_stops_before_build(self):
        self.revision = "0" * 40
        with self.assertRaisesRegex(ValueError, "PR head changed"):
            self.prepare()
        self.assertFalse(any(c[0] == "cmake" for c in self.commands))
        self.assertEqual(self.old.read_bytes(), b"original RPM")

    def test_dirty_source_does_not_produce_accepted_manifest(self):
        self.dirty = True
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.prepare()
        self.assertFalse((self.install.parent / "agent-proxy-manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
