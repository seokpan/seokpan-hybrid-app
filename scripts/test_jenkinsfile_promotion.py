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
                        TEXT.index("stage('Promote GitOps (Recovery PR)')"))
        stages = block("    stages {")
        self.assertIn("Promote GitOps (Recovery PR)", stages)

    def test_credential_only_in_promotion_stage(self):
        self.assertEqual(TEXT.count("withCredentials([usernamePassword(\n                                credentialsId: env.GITOPS_WRITER_CRED"), 1)
        promo = block("stage('Promote GitOps (Recovery PR)')")
        image = block("stage('Image Build & Registry Operations')")
        self.assertNotIn("hybrid-gitops-writer", image)
        self.assertNotIn("GITOPS_WRITER_CRED", image)
        self.assertNotIn("GH_TOKEN", image)
        self.assertEqual(TEXT.count("'hybrid-gitops-writer'"), 1)
        self.assertIn("GITOPS_WRITER_CRED", promo)

    def test_plan_stage_has_no_credentials(self):
        plan = block("stage('Promotion Plan (offline)')")
        self.assertNotIn("withCredentials", plan)
        self.assertIn("runPromotion('')", plan)

    def test_write_only_for_write_mode(self):
        self.assertEqual(TEXT.count("'--write'"), 1)
        self.assertIn("params.PROMOTION_MODE == 'WRITE' ? '--write' : '--remote-check'", TEXT)

    def test_main_only_and_p1_required(self):
        promo = block("stage('Promote GitOps (Recovery PR)')")
        for needle in ("PROMOTION_MODE != 'OFF'", "env.P1_PASSED == 'true'", "currentResult == 'SUCCESS'", "env.BRANCH_NAME != 'main'"):
            self.assertIn(needle, promo)

    def test_recovery_only_and_approved_remote(self):
        self.assertIn("--env recovery", TEXT)
        self.assertNotIn("--env lab", TEXT)
        self.assertNotIn("--env cloud", TEXT)
        self.assertIn("https://github.com/seokpan/seokpan-hybrid-gitops.git", TEXT)
        self.assertIn("git -c credential.helper= clone", TEXT)

    def test_failure_is_not_ignored(self):
        helper = block("def runPromotion")
        self.assertIn("returnStatus: true", helper)
        self.assertIn("error(", helper)

    def test_evidence_stash_and_cleanup(self):
        self.assertIn("stash name: 'image-evidence', includes: 'image-metadata.json'", TEXT)
        self.assertIn("unstash 'image-evidence'", TEXT)
        self.assertIn("rm -rf .ci/gitops", TEXT)

    def test_no_secret_literals(self):
        self.assertNotRegex(TEXT, r"github_pat_|ghp_[A-Za-z0-9]{10,}")


if __name__ == "__main__":
    unittest.main()
