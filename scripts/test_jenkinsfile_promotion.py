# 작성자: 최유준
# 작성 날짜: 2026-10-08
"""Jenkinsfile.image-pipeline 의 Promotion Stage 정적 검증 (Jenkins 실행 아님)."""
import re
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# repo 루트(scripts/ 의 상위)를 우선 사용하고, 같은 디렉터리 사본도 허용한다.
_JENKINSFILE = next(
    (c for c in (_HERE.parent / "Jenkinsfile.image-pipeline", _HERE / "Jenkinsfile.image-pipeline") if c.is_file()),
    _HERE.parent / "Jenkinsfile.image-pipeline",
)
TEXT = _JENKINSFILE.read_text(encoding="utf-8")


def block(start_marker):
    i = TEXT.index(start_marker)
    depth, j = 0, TEXT.index("{", i)
    for k in range(j, len(TEXT)):
        if TEXT[k] == "{":
            depth += 1
        elif TEXT[k] == "}":
            depth -= 1
            if depth == 0:
                return TEXT[i:k + 1]
    raise AssertionError("unbalanced")


class PromotionStageTest(unittest.TestCase):
    def test_braces_balanced(self):
        stripped = re.sub(r"'''.*?'''|\"\"\".*?\"\"\"", "", TEXT, flags=re.S)
        stripped = re.sub(r"//[^\n]*", "", stripped)
        stripped = re.sub(r"'[^'\n]*'|\"[^\"\n]*\"", "", stripped)
        self.assertEqual(stripped.count("{"), stripped.count("}"))

    def test_header(self):
        self.assertTrue(TEXT.startswith("// 작성자: 최유준\n// 작성 날짜:"))

    def test_default_off(self):
        m = re.search(r"choice\(\s*name: 'PROMOTION_MODE',\s*choices: \[([^\]]*)\]", TEXT)
        self.assertTrue(m)
        self.assertTrue(m.group(1).lstrip().startswith("'OFF'"))

    def test_stage_is_top_level_after_image_stage(self):
        self.assertLess(TEXT.index("stage('Image Build & Registry Operations')"),
                        TEXT.index("stage('Promote GitOps (Recovery + Cloud PR)')"))
        stages = block("    stages {")
        self.assertIn("Promote GitOps (Recovery + Cloud PR)", stages)

    def test_credential_only_in_promotion_stage(self):
        self.assertEqual(TEXT.count("withCredentials([usernamePassword(\n                                credentialsId: env.GITOPS_WRITER_CRED"), 1)
        promo = block("stage('Promote GitOps (Recovery + Cloud PR)')")
        image = block("stage('Image Build & Registry Operations')")
        self.assertNotIn("hybrid-gitops-writer", image)
        self.assertNotIn("GITOPS_WRITER_CRED", image)
        self.assertNotIn("GH_TOKEN", image)
        self.assertEqual(TEXT.count("'hybrid-gitops-writer'"), 1)
        self.assertIn("GITOPS_WRITER_CRED", promo)

    def test_no_plan_mode_and_no_network_when_off(self):
        self.assertNotIn("'PLAN'", TEXT)
        self.assertNotIn("Promotion Plan (offline)", TEXT)
        prep = block("stage('Prepare (pinned SHA + Evidence)')")
        self.assertNotIn("clone", prep)
        self.assertNotIn("GITOPS_URL", prep)

    def test_clone_only_in_remote_modes_without_credential(self):
        self.assertEqual(TEXT.count("git -c credential.helper= clone"), 1)
        clone = block("stage('Clone GitOps main (read-only, no credential)')")
        self.assertIn("params.PROMOTION_MODE == 'REMOTE_CHECK' || params.PROMOTION_MODE == 'WRITE'", clone)
        self.assertNotIn("withCredentials", clone)
        self.assertNotIn("GH_TOKEN", clone)
        self.assertLess(TEXT.index("stage('Clone GitOps main"), TEXT.index("stage('Promotion (credentialed)')"))

    def test_write_only_for_write_mode(self):
        self.assertEqual(TEXT.count("'--write'"), 1)
        promo = block("stage('Promote GitOps (Recovery + Cloud PR)')")
        self.assertIn("if (params.PROMOTION_MODE == 'WRITE') {", promo)
        self.assertGreater(promo.index("'--write'"), promo.index("if (params.PROMOTION_MODE == 'WRITE') {"))

    def test_main_only_and_p1_required(self):
        promo = block("stage('Promote GitOps (Recovery + Cloud PR)')")
        for needle in ("PROMOTION_MODE != 'OFF'", "env.P1_PASSED == 'true'", "currentResult == 'SUCCESS'", "env.BRANCH_NAME != 'main'"):
            self.assertIn(needle, promo)

    def test_only_recovery_and_cloud_are_promoted_from_the_approved_remote(self):
        self.assertIn("--env ${promotionEnv}", TEXT)
        self.assertNotIn("--env lab", TEXT)
        self.assertIn("https://github.com/seokpan/seokpan-hybrid-gitops.git", TEXT)

    def test_failure_is_not_ignored(self):
        helper = block("def runPromotion")
        self.assertIn("returnStatus: true", helper)
        self.assertIn("error(", helper)

    def test_evidence_stash_and_cleanup(self):
        self.assertIn("stash name: 'image-evidence', includes: 'image-metadata.json'", TEXT)
        self.assertIn("unstash 'image-evidence'", TEXT)
        self.assertIn("rm -rf .ci/gitops", TEXT)

    def test_cloud_target_only_when_ecr_is_enabled(self):
        promo = block("stage('Promote GitOps (Recovery + Cloud PR)')")
        self.assertIn("def targets = ['recovery']", promo)
        self.assertRegex(promo, r"if \(ecrOn\(\)\) \{\s*targets << 'cloud'")
        self.assertEqual(promo.count("'cloud'"), 1)
        self.assertEqual(promo.count("'recovery'"), 1)

    def test_remote_check_runs_for_every_target_before_any_write(self):
        promo = block("stage('Promote GitOps (Recovery + Cloud PR)')")
        check = promo.index("runPromotion(target, '--remote-check', baseSha)")
        write = promo.index("runPromotion(target, '--write', baseSha)")
        self.assertLess(check, write)

    def test_each_environment_keeps_its_own_result_files(self):
        helper = block("def runPromotion")
        self.assertIn("promotion-result-${promotionEnv}-", helper)
        self.assertNotIn("promotion-result.txt", TEXT)
        self.assertIn("promotion-result-*.txt", TEXT)

    def test_ecr_is_on_by_default_and_promotion_stays_off(self):
        m = re.search(r"booleanParam\(\s*name: 'ENABLE_ECR',\s*defaultValue: (true|false)", TEXT)
        self.assertTrue(m)
        self.assertEqual(m.group(1), "true")
        m = re.search(r"choice\(\s*name: 'PROMOTION_MODE',\s*choices: \[([^\]]*)\]", TEXT)
        self.assertTrue(m.group(1).lstrip().startswith("'OFF'"))

    def test_base_sha_is_pinned_once_and_checkout_is_restored_before_every_run(self):
        promo = block("stage('Promote GitOps (Recovery + Cloud PR)')")
        helper = block("def runPromotion")
        self.assertEqual(promo.count("rev-parse HEAD"), 1)  # pinned once, from the first clone
        self.assertLess(promo.index("def baseSha"),
                        promo.index("runPromotion(target, '--remote-check', baseSha)"))
        self.assertIn("baseSha ==~ /[0-9a-f]{40}/", promo)
        self.assertIn("switch --detach ${baseSha}", helper)
        self.assertIn("--expected-base-sha ${baseSha}", helper)
        self.assertLess(helper.index("switch --detach"), helper.index("gitops_promote.py"))
        self.assertNotIn("git fetch", helper)  # the base is never refreshed
        self.assertNotIn("git -C .ci/gitops pull", TEXT)

    def test_no_secret_literals(self):
        self.assertNotRegex(TEXT, r"github_pat_|ghp_[A-Za-z0-9]{10,}")

if __name__ == "__main__":
    unittest.main()
