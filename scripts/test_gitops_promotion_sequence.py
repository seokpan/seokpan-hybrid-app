#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/14
"""Sequential WRITE regression (hybrid-app#49 review).

The first WRITE moves the GitOps checkout HEAD onto its Promotion Branch, so the next
environment must start from the pinned base again. Real git against a local bare repository
(the GitFixture of test_gitops_transport). The restore command is read from
Jenkinsfile.image-pipeline and executed exactly as written there.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import gitops_promote  # noqa: E402
import gitops_writer as writer  # noqa: E402
import test_gitops_transport as tt  # noqa: E402
import test_gitops_writer as wx  # noqa: E402

JENKINSFILE = Path(__file__).resolve().parent.parent / "Jenkinsfile.image-pipeline"
RECOVERY_PATH = gitops_planner.ENV_KUSTOMIZATION["recovery"]
CLOUD_PATH = gitops_planner.ENV_KUSTOMIZATION["cloud"]
RECOVERY_ID = "rel-recovery-20261014T010000Z-0829a2436483-aaaaaaaa"
CLOUD_ID = "rel-cloud-20261014T010000Z-0829a2436483-bbbbbbbb"
CLOUD_KUST = (
    "images:\n"
    "  - name: seokpan-backend\n"
    "    newName: registry.example.invalid/backend\n"
    "    digest: INPUT_REQUIRED\n"
    "  - name: seokpan-frontend\n"
    "    newName: registry.example.invalid/frontend\n"
    "    digest: INPUT_REQUIRED\n"
)


def restore_command(base_sha: str, checkout: Path) -> str:
    """The checkout-restore command of runPromotion, exactly as written in the Jenkinsfile."""
    text = JENKINSFILE.read_text(encoding="utf-8")
    start = text.index("def runPromotion")
    helper = text[start:text.index("\n}\n", start)]
    lines = [line for line in helper.splitlines() if "switch --detach" in line]
    if len(lines) != 1:
        raise AssertionError("runPromotion must contain exactly one restore command line")
    match = re.search(r'script:\s*"(.*)"\s*$', lines[0])
    if match is None:
        raise AssertionError("cannot read the restore command string from runPromotion")
    command = match.group(1).replace('\\"', '"').replace("\\$", "$")
    return command.replace("${baseSha}", base_sha).replace(".ci/gitops", str(checkout))


@unittest.skipUnless(tt.HAVE_GIT, "git is not installed")
class SequentialWriteTests(tt.GitFixture, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        # origin main also carries a Cloud overlay, so both environments modify an existing file.
        cloud = self.checkout / CLOUD_PATH
        cloud.parent.mkdir(parents=True, exist_ok=True)
        with open(cloud, "w", encoding="utf-8", newline="") as handle:
            handle.write(CLOUD_KUST)
        tt.git("add", "-A", cwd=self.checkout)
        tt.git("commit", "-m", "cloud overlay", cwd=self.checkout)
        tt.git("push", "origin", "HEAD:refs/heads/main", cwd=self.checkout)
        self.base = tt.git("rev-parse", "HEAD", cwd=self.checkout)

    def write(self, release_id: str, path: str, new_text: str, base: str) -> str:
        branch = writer.branch_name(release_id)
        files = {path: new_text, f"releases/{release_id}.json": "{}\n"}
        self.transport().push_commit(branch, base, files, f"chore(promotion): {release_id}")
        return branch

    def write_recovery(self, base: str) -> str:
        return self.write(RECOVERY_ID, RECOVERY_PATH,
                          wx.fx.RECOVERY_KUST.replace(wx.fx.OLD_BE, wx.NEW_BE), base)

    def write_cloud(self, base: str) -> str:
        return self.write(CLOUD_ID, CLOUD_PATH, CLOUD_KUST.replace("INPUT_REQUIRED", "sha256:" + "c" * 64), base)

    def restore(self) -> subprocess.CompletedProcess:
        return subprocess.run(restore_command(self.base, self.checkout), shell=True,
                              capture_output=True, text=True)

    def head(self) -> str:
        return tt.git("rev-parse", "HEAD", cwd=self.checkout)

    def remote_main(self) -> str:
        return tt.git("rev-parse", "main", cwd=self.bare)

    def changed(self, branch: str) -> list:
        return sorted(tt.git("diff", "--name-only", self.base, branch, cwd=self.bare).splitlines())

    def test_first_write_moves_head_off_the_base_and_blocks_the_next_environment(self) -> None:
        self.write_recovery(self.base)
        self.assertNotEqual(self.head(), self.base)
        # Without a restore, the default expected base (the checkout HEAD) is no longer the remote main,
        # which the Writer compares against (PROMOTION_BASE_MOVED).
        self.assertNotEqual(gitops_promote._head_sha(self.checkout), self.remote_main())
        with self.assertRaises(writer.TransportError):
            self.write_cloud(self.base)  # the transport refuses the pinned base while HEAD is elsewhere
        self.assertFalse(self.bare_has(writer.branch_name(CLOUD_ID)))

    def test_restoring_the_pinned_base_lets_the_second_environment_write(self) -> None:
        recovery = self.write_recovery(self.base)
        restored = self.restore()
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(self.head(), self.base)
        self.assertEqual(gitops_promote._head_sha(self.checkout), self.remote_main())
        cloud = self.write_cloud(self.base)
        self.assertEqual(self.remote_main(), self.base)  # main untouched by both writes
        for branch in (recovery, cloud):
            self.assertEqual(tt.git("rev-parse", branch + "^", cwd=self.bare), self.base)
        self.assertEqual(self.changed(recovery), sorted([RECOVERY_PATH, f"releases/{RECOVERY_ID}.json"]))
        self.assertEqual(self.changed(cloud), sorted([CLOUD_PATH, f"releases/{CLOUD_ID}.json"]))

    def test_a_dirty_checkout_fails_the_restore_and_nothing_more_is_written(self) -> None:
        self.write_recovery(self.base)
        (self.checkout / "stray.txt").write_text("x", encoding="utf-8")
        self.assertNotEqual(self.restore().returncode, 0)
        with self.assertRaises(writer.TransportError):  # push_commit refuses a dirty checkout as well
            self.write_cloud(self.base)
        self.assertFalse(self.bare_has(writer.branch_name(CLOUD_ID)))

    def test_the_pinned_base_is_never_refreshed_so_a_moved_main_stays_visible(self) -> None:
        # The Writer's own BASE_MOVED rejection is covered by test_gitops_promote.py; this test only
        # proves the restore keeps the pinned base instead of fetching the new main.
        self.write_recovery(self.base)
        root = Path(self.tmp.name)
        other = root / "other"
        tt.git("clone", str(self.bare), str(other), cwd=root)
        (other / "note.txt").write_text("main moved\n", encoding="utf-8")
        tt.git("add", "-A", cwd=other)
        tt.git("commit", "-m", "main moves", cwd=other)
        tt.git("push", "origin", "HEAD:refs/heads/main", cwd=other)
        moved = self.remote_main()
        self.assertNotEqual(moved, self.base)
        restored = self.restore()
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(self.head(), self.base)
        self.assertNotEqual(gitops_promote._head_sha(self.checkout), moved)


if __name__ == "__main__":
    unittest.main()
