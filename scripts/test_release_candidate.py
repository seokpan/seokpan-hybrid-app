#!/usr/bin/env python3
# 작성자: 최유준 / 작성 날짜: 2026/10/08
"""Unit tests for scripts/release_candidate.py (hybrid-app #15)."""

from __future__ import annotations

import ast
import copy
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitops_planner  # noqa: E402
import release_candidate as target  # noqa: E402

SHA = "46e21a74dd608b41f2c12a0a57d76bddfcf25949"
INFRA = "1e99e36ed2f8" + "0" * 28
H_BE = "sha256:" + "1" * 64
H_FE = "sha256:" + "2" * 64
E_BE = "sha256:" + "5" * 64
E_FE = "sha256:" + "6" * 64
NOW = datetime(2026, 10, 8, 3, 0, 0, tzinfo=timezone.utc)
RID = f"rel-lab-20261008T030000Z-{SHA[:12]}-a1b2c3d4"


def metadata(ecr: bool = False) -> dict:
    comps, summary = {}, {}
    for name, h, e in (("backend", H_BE, E_BE), ("frontend", H_FE, E_FE)):
        harbor = {"repository": f"seokpan-hybrid/{name}", "final_digest": h, "platforms": ["linux/amd64"]}
        entry = {"harbor": harbor, "ecr": None}
        summary[name] = {"ecr_digest": None, "harbor_digest": h, "platform": "linux/amd64"}
        if ecr:
            entry["ecr"] = {"repository": f"seokpan-fnd-{name}", "final_digest": e, "platforms": ["linux/amd64"]}
            summary[name]["ecr_digest"] = e
        comps[name] = entry
    return {
        "ecr_enabled": ecr,
        "release_json_images": summary,
        "commit_sha_full": SHA,
        "commit_sha_12": SHA[:12],
        "components": comps,
    }


class ReleaseIdTests(unittest.TestCase):
    def test_format(self) -> None:
        self.assertEqual(target.format_release_id("lab", SHA, NOW, "a1b2c3d4"), RID)
        self.assertRegex(RID, target.RELEASE_ID_RE)

    def test_new_release_id_is_valid_and_random(self) -> None:
        a = target.new_release_id("recovery", SHA)
        b = target.new_release_id("recovery", SHA)
        target.validate_release_id(a, "recovery", SHA)
        self.assertNotEqual(a, b)

    def test_non_utc_time_is_rejected_but_offset_zero_is_fine(self) -> None:
        kst = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone(timedelta(hours=9)))
        with self.assertRaises(target.ReleaseError) as ctx:
            target.format_release_id("lab", SHA, kst, "a1b2c3d4")
        self.assertEqual(ctx.exception.code, "TIME_INVALID")
        with self.assertRaises(target.ReleaseError):
            target.format_release_id("lab", SHA, datetime(2026, 10, 8, 3, 0, 0), "a1b2c3d4")

    def test_bad_inputs(self) -> None:
        for env, sha, rand, code in (
            ("prod", SHA, "a1b2c3d4", "ENV_INVALID"),
            ("lab", "abc", "a1b2c3d4", "APP_SHA_INVALID"),
            ("lab", SHA, "a1b2c3", "RANDOM_INVALID"),
            ("lab", SHA, "A1B2C3D4", "RANDOM_INVALID"),
        ):
            with self.assertRaises(target.ReleaseError) as ctx:
                target.format_release_id(env, sha, NOW, rand)
            self.assertEqual(ctx.exception.code, code)

    def test_validate_mismatches(self) -> None:
        target.validate_release_id(RID, "lab", SHA)
        with self.assertRaises(target.ReleaseError) as ctx:
            target.validate_release_id(RID, "recovery", SHA)
        self.assertEqual(ctx.exception.code, "RELEASE_ID_MISMATCH")
        with self.assertRaises(target.ReleaseError) as ctx:
            target.validate_release_id(RID, "lab", "f" * 40)
        self.assertEqual(ctx.exception.code, "RELEASE_ID_MISMATCH")
        for bad in ("git-46e21a74dd60", "rel-lab-20261308T030000Z-46e21a74dd60-a1b2c3d4", RID + "x", ""):
            with self.assertRaises(target.ReleaseError) as ctx:
                target.validate_release_id(bad, "lab", SHA)
            self.assertEqual(ctx.exception.code, "RELEASE_ID_INVALID")

    def test_collision_never_overwrites(self) -> None:
        target.check_no_collision(RID, ["other.json"])
        with self.assertRaises(target.ReleaseError) as ctx:
            target.check_no_collision(RID, [f"{RID}.json"])
        self.assertEqual(ctx.exception.code, "RELEASE_FILE_EXISTS")


class CandidateTests(unittest.TestCase):
    def test_shape_and_unreceived_values_stay_null(self) -> None:
        c = target.build_candidate(metadata(), "lab", RID)
        self.assertEqual(c["schema_version"], 1)
        self.assertEqual(c["record_kind"], "candidate")
        self.assertEqual(c["completeness"], "INCOMPLETE")
        self.assertEqual(c["release_id"], RID)
        self.assertEqual(c["environment"], "lab")
        self.assertEqual(c["source"], {"app_sha": SHA, "infra_sha": None})
        self.assertEqual(c["images"]["backend"], {"ecr_digest": None, "harbor_digest": H_BE, "platform": "linux/amd64"})
        self.assertEqual(c["images"]["frontend"]["harbor_digest"], H_FE)
        self.assertEqual(c["revisions"], {k: None for k in target.REVISION_KEYS})
        self.assertEqual(c["review_refs"], [])
        self.assertEqual(c["verification"], {"render": "NOT RUN", "deployment": "NOT RUN", "acceptance": "NOT RUN"})
        self.assertEqual(
            c["missing_inputs"], ["SOURCE_COMMITS", "CONFIGURATION_REVISIONS", "RENDER_AND_REVIEW"]
        )

    def test_no_self_referencing_gitops_sha(self) -> None:
        text = target.render_json(target.build_candidate(metadata(), "lab", RID))
        self.assertNotIn("gitops_sha", text)

    def test_cloud_without_ecr_keeps_digest_missing(self) -> None:
        rid = RID.replace("rel-lab", "rel-cloud")
        c = target.build_candidate(metadata(), "cloud", rid)
        self.assertIn("ARTIFACT_DIGESTS_AND_PLATFORMS", c["missing_inputs"])
        self.assertIsNone(c["images"]["backend"]["ecr_digest"])

    def test_cloud_with_ecr_records_ecr_digest(self) -> None:
        rid = RID.replace("rel-lab", "rel-cloud")
        c = target.build_candidate(metadata(ecr=True), "cloud", rid)
        self.assertEqual(c["images"]["backend"]["ecr_digest"], E_BE)
        self.assertNotIn("ARTIFACT_DIGESTS_AND_PLATFORMS", c["missing_inputs"])

    def test_known_inputs_clear_only_their_missing_marker(self) -> None:
        c = target.build_candidate(
            metadata(), "lab", RID, infra_sha=INFRA, revisions={"schema": "s1", "config": "c1", "secret": "k1"}
        )
        self.assertEqual(c["missing_inputs"], ["RENDER_AND_REVIEW"])
        self.assertEqual(c["source"]["infra_sha"], INFRA)
        self.assertEqual(c["completeness"], "INCOMPLETE")
        partial = target.build_candidate(metadata(), "lab", RID, revisions={"schema": "s1"})
        self.assertIn("CONFIGURATION_REVISIONS", partial["missing_inputs"])

    def test_invalid_extra_inputs(self) -> None:
        for kwargs, code in (
            ({"infra_sha": "abc"}, "INFRA_SHA_INVALID"),
            ({"revisions": {"bogus": "x"}}, "REVISION_KEY_INVALID"),
            ({"revisions": {"schema": " "}}, "REVISION_VALUE_INVALID"),
        ):
            with self.assertRaises(target.ReleaseError) as ctx:
                target.build_candidate(metadata(), "lab", RID, **kwargs)
            self.assertEqual(ctx.exception.code, code)

    def test_metadata_errors_propagate_and_id_must_match(self) -> None:
        bad = metadata()
        bad["commit_sha_full"] = "abc"
        with self.assertRaises(gitops_planner.PlanError):
            target.build_candidate(bad, "lab", RID)
        with self.assertRaises(target.ReleaseError) as ctx:
            target.build_candidate(metadata(), "recovery", RID)
        self.assertEqual(ctx.exception.code, "RELEASE_ID_MISMATCH")

    def test_inputs_not_mutated_and_json_round_trip(self) -> None:
        meta = metadata()
        snapshot = copy.deepcopy(meta)
        c = target.build_candidate(meta, "lab", RID)
        self.assertEqual(meta, snapshot)
        text = target.render_json(c)
        self.assertTrue(text.endswith("}\n"))
        self.assertEqual(json.loads(text), c)


class PurityTests(unittest.TestCase):
    def test_no_network_process_or_token(self) -> None:
        src = (Path(__file__).resolve().parent / "release_candidate.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        forbidden = {"subprocess", "socket", "urllib", "http", "requests", "shutil", "ssl", "os"}
        self.assertEqual(imported & forbidden, set())
        self.assertNotIn("GH_TOKEN", src)
        self.assertNotIn("github_pat_", src)


class CliTests(unittest.TestCase):
    def run_cli(self, argv: list) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = target.main(argv)
        return code, out.getvalue(), err.getvalue()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.meta = self.root / "meta.json"
        self.meta.write_text(json.dumps(metadata()), encoding="utf-8")
        self.releases = self.root / "releases"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def base(self) -> list:
        return ["--env", "lab", "--metadata", str(self.meta), "--release-id", RID]

    def test_dry_run_writes_nothing(self) -> None:
        code, out, _ = self.run_cli(self.base() + ["--releases-dir", str(self.releases)])
        self.assertEqual(code, 0)
        self.assertIn("RELEASE_STATUS=DRY_RUN", out)
        self.assertIn(f"RELEASE_FILE=releases/{RID}.json", out)
        self.assertFalse(self.releases.exists())

    def test_write_creates_once_and_never_overwrites(self) -> None:
        code, out, _ = self.run_cli(self.base() + ["--releases-dir", str(self.releases), "--write"])
        self.assertEqual(code, 0)
        self.assertIn("RELEASE_STATUS=CREATED", out)
        path = self.releases / f"{RID}.json"
        first = path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(first)["release_id"], RID)
        code, _, err = self.run_cli(self.base() + ["--releases-dir", str(self.releases), "--write"])
        self.assertEqual(code, 2)
        self.assertIn("RELEASE_ERROR: RELEASE_FILE_EXISTS", err)
        self.assertEqual(path.read_text(encoding="utf-8"), first)

    def test_generated_id_when_not_given(self) -> None:
        code, out, _ = self.run_cli(["--env", "recovery", "--metadata", str(self.meta)])
        self.assertEqual(code, 0)
        rid = re.search(r"RELEASE_ID=(\S+)", out).group(1)
        target.validate_release_id(rid, "recovery", SHA)

    def test_errors_exit_2(self) -> None:
        code, _, err = self.run_cli(["--env", "lab", "--metadata", str(self.meta), "--write"])
        self.assertEqual((code, "ARGUMENT_INVALID" in err), (2, True))
        code, _, err = self.run_cli(["--env", "recovery", "--metadata", str(self.meta), "--release-id", RID])
        self.assertEqual((code, "RELEASE_ID_MISMATCH" in err), (2, True))
        broken = self.root / "broken.json"
        broken.write_text("{}", encoding="utf-8")
        code, _, err = self.run_cli(["--env", "lab", "--metadata", str(broken), "--release-id", RID])
        self.assertEqual((code, "METADATA_INVALID" in err), (2, True))


if __name__ == "__main__":
    unittest.main()
