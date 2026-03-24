# ☁️ MirrAI 클라우드 배포 가이드 (DevOps)

이 가이드는 현재 MirrAI 프로젝트의 **AWS Elastic Beanstalk (EB)** 기반 배포 구조와 **GitHub Actions**를 통한 CI/CD 자동화 파이프라인에 대한 안내입니다. 관리의 편의성과 확장성을 고려하여 Docker 컨테이너 기반의 EB 환경을 사용합니다.

---

## 🏗️ 1. 아키텍처 개요

- **오케스트레이션**: `AWS Elastic Beanstalk` (Docker Platform)
- **컨테이너 저장소**: `AWS ECR` (Amazon Elastic Container Registry)
- **컴퓨팅 환경**: `AWS EC2` (EB에 의해 자동 관리, t3.micro 권장)
- **이미지 업로드 저장소**: `AWS S3` (mirrai-user-images-dev 및 EB 배포 번들 저장용)
- **CI/CD 파이프라인**: `GitHub Actions` (OIDC 인증 방식)
- **배포 설정 파일**: `Dockerrun.aws.json` (EB 전용 컨테이너 정의 파일)

---

## 🛠️ 2. 핵심 배포 구성 요소

### 1) `Dockerrun.aws.json`
Elastic Beanstalk에 배포할 Docker 컨테이너의 정보를 담고 있습니다. 
- 배포 시 GitHub Actions가 빌드한 최신 ECR 이미지 URI를 이 파일에 동적으로 주입합니다.
- 호스트의 80포트를 컨테이너의 8000포트(Django)로 포워딩하도록 설정되어 있습니다.

### 2) `terraform/` (인프라 코드)
프로젝트에 필요한 핵심 리소스를 코드로 관리합니다.
- `aws_ecr_repository`: Backend/Frontend Docker 이미지를 저장할 리포지토리 생성
- `aws_s3_bucket`: 고객 분석 이미지 저장용 S3 버킷 및 CORS 설정
- `aws_iam_role`: EC2/EB 환경에서 필요한 최소 권한(ECR Pull, SSM Core 등) 정의

---

## 🚀 3. CI/CD 파이프라인 (`.github/workflows/deploy.yml`)

`main` 브랜치에 코드가 푸시되면 자동으로 이미지가 빌드되고 EB 환경이 업데이트됩니다.

### 배포 프로세스
1. **Docker 빌드 및 푸시**: `backend/Dockerfile`을 기반으로 이미지를 빌드하고 AWS ECR에 전송합니다.
2. **이미지 URI 주입**: 빌드된 최신 이미지 주소를 `Dockerrun.aws.json` 파일에 자동으로 기록합니다.
3. **배포 패키지 생성**: `Dockerrun.aws.json`을 `zip`으로 압축하여 배포 번들을 만듭니다.
4. **EB 버전 생성 및 배포**: 생성된 번들을 S3에 업로드하고, Elastic Beanstalk 환경을 최신 버전으로 업데이트하여 무중단 배포를 시도합니다.

### 필수 GitHub Secrets 등록
배포를 위해 GitHub 레포지토리 `Settings` > `Secrets and variables` > `Actions`에 다음 항목을 반드시 등록해야 합니다.

| Secret Name | 설명 |
|---|---|
| `AWS_OIDC_ROLE_ARN` | GitHub Actions가 OIDC로 임시 인증을 받을 IAM Role ARN |
| `EB_APPLICATION_NAME` | AWS Elastic Beanstalk 애플리케이션 이름 (예: `mirrai-app`) |
| `EB_ENVIRONMENT_NAME` | AWS Elastic Beanstalk 환경 이름 (예: `mirrai-env`) |
| `ECR_REPOSITORY_NAME` | AWS ECR 리포지토리 이름 (예: `mirrai-backend`) |

---

## 🔐 4. 앱 환경 변수(Secret) 및 보안 지침

민감한 정보(예: `DB_URL`, `SECRET_KEY`, `OPENAI_API_KEY` 등)는 코드에 하드코딩하지 않으며 다음과 같이 관리합니다.

1.  **EB 환경 속성 주입**: AWS 콘솔의 `Configuration > Updates, monitoring, and logging > Environment properties`에서 설정합니다.
2.  **SSM Parameter Store**: 테라폼 코드에 정의된 대로 `/mirrai/*` 경로의 파라미터를 사용하여 런타임에 동적으로 주입받을 수 있습니다.
3.  **보안 유지**: `.env` 파일은 절대 Git에 포함하지 않으며, `.gitignore`를 통해 철저히 관리합니다.

> 💡 **참고**: 배포에 실패할 경우 AWS 콘솔의 `Elastic Beanstalk > Environments > Logs`에서 전체 로그를 확인하여 원인을 파악하세요.
