# 공개 CA 시험 자료

`public-ca.crt`는 CA 로딩·검증 설정 및 다른 시험 CA를 거부하는 검사에 사용한다.
개인키는 포함하지 않는다. 실제 Runtime의 기본 CA나 자동 배포 파일이 아니다.

- 출처: [GitOps 공개 CA 파일](https://github.com/seokpan/seokpan-gitops/blob/4066497937106a14cc3978be72d568603b6b5614/apps/backend/database-ca-configmap.yaml), [PR #52](https://github.com/seokpan/seokpan-gitops/pull/52)
- X.509 SHA-256 Fingerprint: `28:EE:82:23:2C:08:E7:48:A6:65:D7:98:65:AA:BB:5A:58:53:BE:32:BE:28:29:DB:37:F1:EE:02:6C:87:2F:60`

실제 TLS 협상 시험의 인증서·개인키는 실행 때 임시 시험 경로에 따로 생성한다.
운영 CA 변경 때문에 이 고정 시험 자료를 자동으로 교체할 필요는 없다.
