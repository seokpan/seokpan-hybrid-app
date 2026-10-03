// 작성자: 최유준
// 작성 날짜: 2026-09-09 (2026-09-10 개정: v3.1 - headless_shell 파일명, Checkout 컨테이너 수정)
//
// seokpan-app 표준 Jenkinsfile v3 (App #62 팀 검토 반영)
//
// [구조 변경 이력]
// v1(PR-54): PR Pipeline(Backend/Frontend Lint&Test + Build Verify)만 포함.
// v3(이번): App #62의 6단계 검사(verify_ci.py/export_openapi_ci.py/verify:ci/e2e:ci/summarize_ci.py)로
//   기존 Backend/Frontend Lint&Test를 완전히 대체. 팀 검토 결론(Q1 답변):
//   "기존 검사 항목이 새 실행 명령에 포함되므로 별도로 반복할 필요가 없다."
// v3.1(2026-09-10): app-ci Dockerfile 실제 Build 검증(seokpan-gitops #53)에서 확인된 두 가지를
//   반영. (1) Playwright 실행 파일명이 headless_shell이 아니라 chrome-headless-shell로
//   배포됨 - Prepare Browser Cache의 find 조건 확장. (2) buildkit-rootless-pr은 buildkit
//   컨테이너 1개뿐이고 git이 설치돼 있지 않음 - Build Verify (PR) 그룹의 Checkout을
//   container('buildkit') 밖(자동 주입되는 jnlp 컨테이너)에서 실행하도록 수정.
//
// [Agent 두 그룹으로 분리 - 팀 검토 Q3 답변 + 보완사항 ① 반영]
//   - "App CI Check": app-ci-check PodTemplate (Credential 없음, app-ci 통합 컨테이너 1개)
//   - "Build Verify (PR)": buildkit-rootless-pr PodTemplate (Credential 없음, buildkit 컨테이너 1개)
//   기존 buildkit-rootless(Harbor Credential 포함, python/node 컨테이너 존치)는 이번 작업에서
//   삭제하지 않음 - Python은 seokpan-app Issue #58 main Harbor API 작업에서 계속 사용됨(팀 답변).
//
// [보완사항 ② 반영] Frontend Build Verify 앞의 별도 `npm run build` 제거.
//   frontend/Dockerfile이 내부에서 npm ci && npm run build를 수행하고, 이번 두 번째 Pod에는
//   첫 Pod(App CI Check)의 node_modules가 없으므로 외부에서 npm run build를 먼저 돌리면 실패한다.
//
// [보완사항 ③ 반영] Commit·Run ID는 최초 Checkout에서 한 번만 계산해 env로 양쪽 그룹에 공유.
//   Build Verify 그룹은 그 SHA를 정확히 Checkout하고 일치 여부를 확인한다. 같은 브랜치의
//   최신 Commit을 다시 받거나 각 Pod에서 Run ID를 새로 계산하지 않는다.
//
// [보완사항 ④ 반영] Build Verify는 beforeAgent true로 PR 여부 + 명시적 통과 상태(env.APP_CI_PASSED)
//   + 현재 Build 성공 상태를 모두 확인한 뒤에만 Pod를 할당한다. 단, 조건 불충족으로 이 Stage를
//   건너뛰는 것과 Pipeline 자체 실패는 다르다 - App CI Check 단계의 실패는 그 자체로 Pipeline을
//   FAILURE로 만들며, Build Verify가 "skipped"로 표시되는 것과 별개로 최종 상태는 실패로 남는다.
//
// [보완사항 ⑤ 반영] 정상 집계·필수 보고서 수집은 App CI Check 그룹의 Aggregate Stage에서 Image
//   단계 전에 끝낸다. 실패 시 남은 집계·수집은 App CI Check 그룹(Stage 전체)의 post에서 처리한다.
//   최상위 agent none의 post에서는 첫 Pod의 파일에 접근할 수 없다고 가정하고 아무 파일도 참조하지 않는다.
//
// [Template 참조] label 대신 inheritFrom으로 등록된 Template 이름을 명시 참조한다
//   (Kubernetes Plugin 공식 권장 - Kubernetes Plugin 문서 참고, 팀 코멘트 반영).
//

pipeline {
    agent none

    options {
        skipDefaultCheckout()
        disableConcurrentBuilds()
        disableRestartFromStage()
        timestamps()
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    stages {
        // ==================================================================
        // Group 1: App CI Check - app-ci-check PodTemplate (Credential 없음)
        // ==================================================================
        stage('App CI Check') {
            agent {
                kubernetes {
                    inheritFrom 'app-ci-check'
                }
            }
            environment {
                REGISTRY_HOST = 'harbor.seokpan.soldesk.store'
                HARBOR_PROJECT = 'seokpan'
                HOME = '/tmp'
            }
            stages {
                stage('Checkout') {
                    steps {
                        container('app-ci') {
                            script {
                                def scmVars = checkout scm
                                env.SEOKPAN_GIT_SHA = scmVars.GIT_COMMIT
                                // Run ID는 여기서 한 번만 계산 - Build Verify 그룹은 이 값을
                                // env로 물려받아 그대로 쓰고 재계산하지 않는다 (보완사항 ③)
                                env.SEOKPAN_CI_RUN_ID = "a09-${env.BUILD_NUMBER}-${env.SEOKPAN_GIT_SHA.take(12)}"
                                sh 'id'
                                echo "commit=${env.SEOKPAN_GIT_SHA} run_id=${env.SEOKPAN_CI_RUN_ID}"
                            }
                        }
                    }
                }

                stage('Prepare Browser Cache') {
                    steps {
                        container('app-ci') {
                            dir('frontend') {
                                sh '''
                                    set -eu
                                    SRC="/opt/playwright-cache"
                                    DEST=".browser-cache"

                                    # 새 작업 공간에 예상외 캐시가 있으면 덮어쓰거나 삭제하지 않고 중단
                                    if [ -e "$DEST" ] || [ -L "$DEST" ]; then
                                        echo "ERROR: $DEST already exists in a fresh workspace. Aborting." >&2
                                        exit 1
                                    fi

                                    START=$(date +%s)
                                    SRC_SIZE=$(du -sh "$SRC" | cut -f1)
                                    cp -r "$SRC" "$DEST"
                                    END=$(date +%s)
                                    echo "browser-cache copy: ${SRC_SIZE}, $((END-START))s, src=${SRC}"

                                    # 고정 Playwright 1.63.0 버전에 대응하는 브라우저인지 확인
                                    REVISION_DIR="$DEST/chromium_headless_shell-1243"
                                    [ -d "$REVISION_DIR" ] || { echo "ERROR: revision 1243 missing" >&2; exit 1; }
                                    # [v3.1 수정] 실제 Dockerfile Build 검증(seokpan-gitops #53)에서
                                    # 실행 파일명이 headless_shell이 아니라 chrome-headless-shell로
                                    # 배포됨을 확인 - 두 이름 다 찾도록 조건 확장.
                                    BIN=$(find "$REVISION_DIR" -type f -name "headless_shell" -o -name "chrome-headless-shell" | head -1)

                                    [ -n "$BIN" ] && [ -x "$BIN" ] || { echo "ERROR: binary missing/not executable" >&2; exit 1; }
                                    if ldd "$BIN" 2>&1 | grep -q "not found"; then
                                        echo "ERROR: missing shared libraries:" >&2
                                        ldd "$BIN" | grep "not found" >&2
                                        exit 1
                                    fi

                                    # 원본은 검사 UID가 변경하지 못해야 함 - 쓰기 가능하면 이미지 설정 오류
                                    if touch "$SRC/.write-test" 2>/dev/null; then
                                        echo "ERROR: source is writable by CI UID - security regression" >&2
                                        exit 1
                                    fi
                                    echo "source read-only confirmed"
                                '''
                            }
                        }
                    }
                }

                stage('Backend: Install & Verify') {
                    steps {
                        container('app-ci') {
                            dir('backend') {
                                sh 'python scripts/verify_ci.py --run-id ${SEOKPAN_CI_RUN_ID} --uv /usr/local/bin/uv'
                            }
                        }
                    }
                }

                stage('OpenAPI Export') {
                    steps {
                        container('app-ci') {
                            dir('backend') {
                                sh '.venv/bin/python scripts/export_openapi_ci.py --run-id ${SEOKPAN_CI_RUN_ID}'
                            }
                        }
                    }
                }

                stage('Frontend: Install & Verify') {
                    steps {
                        container('app-ci') {
                            dir('frontend') {
                                sh 'npm run verify:ci -- --run-id ${SEOKPAN_CI_RUN_ID}'
                            }
                        }
                    }
                }

                stage('Browser: UI') {
                    steps {
                        container('app-ci') {
                            dir('frontend') {
                                sh 'npm run e2e:ci -- ui --run-id ${SEOKPAN_CI_RUN_ID}'
                            }
                        }
                    }
                }

                stage('Browser: Full') {
                    steps {
                        container('app-ci') {
                            dir('frontend') {
                                sh 'npm run e2e:ci -- full --run-id ${SEOKPAN_CI_RUN_ID}'
                            }
                        }
                    }
                }

                stage('Aggregate') {
                    steps {
                        container('app-ci') {
                            dir('backend') {
                                script {
                                    // 집계 "시도" 여부를 호출 전에 기록 - 실패해도 post에서 재집계하지 않음
                                    env.CI_AGGREGATE_ATTEMPTED = 'true'
                                    sh 'python scripts/summarize_ci.py --run-id ${SEOKPAN_CI_RUN_ID}'
                                }
                            }
                            // 정상 경로 수집 - 비어있으면 그 자체로 실패 처리 (allowEmptyArchive 없음)
                            archiveArtifacts(
                                artifacts: "test-results/${env.SEOKPAN_CI_RUN_ID}/**",
                                fingerprint: true
                             )
                             // 집계 + 정상 경로 수집까지 모두 성공해야 통과 상태로 인정 (보완사항 ④)
                             script { env.APP_CI_PASSED = 'true' }
                        }
                    }
                }
            }
            post {
                always {
                    script {
                        try {
                            container('app-ci') {
                                // 이미 시도했다면(성공이든 실패든) 재집계하지 않고 수집만 시도
                                if (env.CI_AGGREGATE_ATTEMPTED != 'true') {
                                    dir('backend') {
                                        sh 'python scripts/summarize_ci.py --run-id ${SEOKPAN_CI_RUN_ID} || true'
                                    }
                                }
                                archiveArtifacts(
                                    artifacts: "test-results/${env.SEOKPAN_CI_RUN_ID}/**",
                                    allowEmptyArchive: true,
                                    fingerprint: true
                                )
                            }
                        } catch (Exception e) {
                            // container 부재 등 Jenkins 레벨 예외 - 원래 실패 상태를 덮지 않고 기록만 남김
                            echo "WARN: fallback collection skipped (agent/container unavailable): ${e.getMessage()}"
                        }
                    }
                }
            }
        } // 

        // ==================================================================
        // Group 2: Build Verify (PR) - buildkit-rootless-pr PodTemplate
        // (Credential 없음, push=false만 수행)
        // ==================================================================
        stage('Build Verify (PR)') {
            when {
                beforeAgent true
                allOf {
                    expression { env.CHANGE_ID != null }          // PR 여부
                    expression { env.APP_CI_PASSED == 'true' }    // 명시적 통과 상태
                    expression { currentBuild.currentResult == 'SUCCESS' }  // 현재 Build 성공 상태
                }
            }
            agent {
                kubernetes {
                    inheritFrom 'buildkit-rootless-pr'
                }
            }
            environment {
                REGISTRY_HOST = 'harbor.seokpan.soldesk.store'
                HARBOR_PROJECT = 'seokpan'
            }
            stages {
                stage('Checkout (pinned SHA)') {
                    // [v3.1 수정] buildkit-rootless-pr은 buildkit 컨테이너 1개뿐이고
                    // moby/buildkit 이미지에는 git이 없다. container('buildkit')로 감싸면
                    // git 명령이 실패한다 - 자동 주입되는 jnlp 컨테이너(git 포함)에서 실행하도록
                    // container() 블록 없이 실행. Workspace는 emptyDir 공유 Volume이라 이후
                    // container('buildkit') 안에서도 그대로 보인다.
                    steps {
                        script {
                            checkout([
                                $class: 'GitSCM',
                                branches: [[name: env.SEOKPAN_GIT_SHA]],
                                userRemoteConfigs: [[url: scm.userRemoteConfigs[0].url]]
                            ])
                            def actualSha = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
                            if (actualSha != env.SEOKPAN_GIT_SHA) {
                                error "Commit mismatch: expected ${env.SEOKPAN_GIT_SHA}, got ${actualSha}"
                            }
                        }
                    }
                }

                stage('Backend: Build Verify') {
                    steps {
                        container('buildkit') {
                            dir('backend') {
                                sh '''
                                    buildctl-daemonless.sh build \
                                      --frontend dockerfile.v0 \
                                      --local context=. \
                                      --local dockerfile=. \
                                      --output type=image,name=${REGISTRY_HOST}/${HARBOR_PROJECT}/backend:pr-verify,push=false
                                '''
                            }
                        }
                    }
                }

                stage('Frontend: Build Verify') {
                    // 보완사항 ② - 별도 npm run build 단계 제거. Dockerfile 내부에서
                    // npm ci && npm run build를 수행하며, 이 Pod에는 App CI Check 그룹의
                    // node_modules가 없으므로 외부 npm 명령은 실패한다.
                    steps {
                        container('buildkit') {
                            dir('frontend') {
                                sh '''
                                    buildctl-daemonless.sh build \
                                      --frontend dockerfile.v0 \
                                      --local context=. \
                                      --local dockerfile=. \
                                      --output type=image,name=${REGISTRY_HOST}/${HARBOR_PROJECT}/frontend:pr-verify,push=false
                                '''
                            }
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            // 최상위 post는 첫 Pod(app-ci-check)가 이미 종료된 뒤일 수 있으므로
            // 그 Workspace의 파일을 절대 참조하지 않는다 (보완사항 ⑤).
            echo "Pipeline finished: branch=${env.BRANCH_NAME}, commit=${env.SEOKPAN_GIT_SHA}, run_id=${env.SEOKPAN_CI_RUN_ID}, result=${currentBuild.currentResult}"
        }
    }
}
