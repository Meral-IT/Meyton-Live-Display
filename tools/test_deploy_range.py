"""Run with: python3 -m unittest discover -s tools -p test_deploy_range.py"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class DeployRangeTests(unittest.TestCase):
    def test_deployment_and_failed_load(self):
        script = Path(__file__).with_name("deploy-range.sh").resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            remote = root / "range's directory"
            remote.mkdir()
            (remote / ".env").write_text("SM_ADMIN_PASSWORD=target-only\n")
            compose = remote / "compose.yaml"
            compose.write_text("installation-specific compose\n")
            log = root / "commands"
            for name, contents in {
                "ssh": '#!/bin/sh\nexec sh -c "$2"\n',
                "docker": '''#!/bin/sh
echo "$*" >> "$COMMAND_LOG"
case "$1" in
    version) echo arm64 ;;
    save) echo archive > "$3" ;;
    load) test "$(cat "$3")" = archive; test "${FAIL_LOAD:-0}" = 0 ;;
esac
''',
            }.items():
                executable = root / name
                executable.write_text(contents)
                executable.chmod(0o755)
            environment = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                               COMMAND_LOG=str(log))
            for fail in (False, True):
                log.write_text("")
                result = subprocess.run(
                    ["bash", str(script), "range-user@host", str(remote)],
                    env=dict(environment, FAIL_LOAD=str(int(fail))),
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode == 0, not fail, result.stderr)
                commands = log.read_text()
                self.assertEqual(commands.count("build --platform linux/arm64"), 2)
                self.assertIn("save -o", commands)
                self.assertEqual("up -d --pull never --no-build" in commands, not fail)
                self.assertEqual(compose.read_text(), "installation-specific compose\n")
                self.assertEqual(list(remote.glob("range-images.*")), [])
            compose.unlink()
            environment["FAIL_LOAD"] = "0"
            subprocess.run(["bash", str(script), "host", str(remote)],
                           env=environment, check=True, capture_output=True)
            self.assertEqual(compose.read_text(), script.parent.parent.joinpath("compose.yaml").read_text())


if __name__ == "__main__":
    unittest.main()
