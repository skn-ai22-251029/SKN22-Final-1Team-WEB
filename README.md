# 🪞 MirrAI (SKN22-Final-1Team-WEB)

AI 기반 퍼스널 헤어 스타일 분석 및 추천 솔루션, **MirrAI** 프로젝트 저장소입니다.  
고객의 기본 정보, 스타일 취향, 그리고 얼굴 촬영 데이터를 분석하여 최적화된 헤어스타일을 제안하는 혁신적인 사용자 경험을 제공합니다.

현재 구조는 **Django (MVT)** 아키텍처를 기반으로 백엔드 API와 프론트엔드 UI를 통합하여 관리하며, **AWS Elastic Beanstalk**을 통해 컨테이너 기반으로 배포됩니다.

---

## 🌟 서비스 개요

- **고객(Customer) 여정**: 서비스 시작 ➡️ 정보 입력 ➡️ 취향 설문 ➡️ 페이스 스캔 ➡️ AI 분석 리포트 확인
- **파트너(Partner) 관리**: 미용실 운영자를 위한 대시보드, 고객 상담 이력 관리 및 스타일 트렌드 리포트 제공
- **시스템 관리(Admin)**: 서비스 전체 데이터베이스 관리 및 핵심 설정 제어 (커스텀 UI)
- **디자인 컨셉**: 소프트 미니멀리즘 + 에디토리얼 레이아웃 기반의 세련된 사용자 경험

---

## ✨ 핵심 기능

- **정교한 스타일 설문**: 5가지 핵심 카테고리(길이, 분위기, 모발 상태, 컬러, 예산)에 대한 개인별 취향 수집
- **AI 페이스 분석 연동**: 촬영된 이미지를 전처리하고 AI 엔진을 통해 얼굴형과 어울리는 스타일 매칭
- **역할별 UI 분리**: 고객, 파트너, 시스템 관리자가 각각의 목적에 맞는 전용 화면에서 서비스 이용
- **통합 데모 & 쇼케이스**: 프로젝트의 모든 페이지를 한눈에 확인하고 테스트할 수 있는 관리 도구 제공

---

## 🏗️ 프로젝트 구조

```text
.
├── backend/                # Django 통합 서버 (핵심 코드)
│   ├── app/                # 비즈니스 로직 및 API (api/v1/)
│   ├── mirrai_project/     # Django 프로젝트 설정
│   ├── static/             # 정적 자산 (shared/, customer/, admin/ CSS/JS/Images)
│   ├── templates/          # HTML 템플릿 (layouts/, components/, customer/, admin/, demo/)
│   ├── manage.py           # Django 관리 스크립트
│   └── requirements.txt    # 파이썬 의존성 패키지
├── docs/                   # 프로젝트 문서 및 DevOps 가이드
├── terraform/              # AWS 인프라 관리 (IaC)
├── .github/workflows/      # CI/CD 자동화 (GitHub Actions)
└── Dockerrun.aws.json      # Elastic Beanstalk 배포 정의 파일
```

---

## 🚀 로컬 실행 방법

### 1) 환경 설정 및 패키지 설치
```bash
cd backend
# 가상환경 활성화 후 실행 권장
pip install -r requirements.txt
python manage.py migrate
```

### 2) 서버 실행
```bash
# 직접 실행
python manage.py runserver

# 또는 제공된 배치 파일 사용 (Windows 전용, 로그는 server_log.txt에 기록됨)
run_server.bat
```

---

## 🔗 주요 접속 경로 (Access Paths)

### 통합 데모 (Quick Access)
- **전체 페이지 쇼케이스:** [http://localhost:8000/demo/discovery/](http://localhost:8000/demo/discovery/)
  - *이 페이지에서 모든 프론트엔드 화면으로 즉시 접근할 수 있습니다.*

### 고객 서비스 (Customer)
- **메인 홈**: [http://localhost:8000/](http://localhost:8000/)
- **정보 입력**: [http://localhost:8000/customer/](http://localhost:8000/customer/)
- **취향 설문**: [http://localhost:8000/customer/survey/](http://localhost:8000/customer/survey/)
- **페이스 스캔**: [http://localhost:8000/customer/camera/](http://localhost:8000/customer/camera/)
- **분석 결과**: [http://localhost:8000/customer/recommendations/](http://localhost:8000/customer/recommendations/)

### 파트너 & 관리자 (Admin)
- **파트너 로그인**: [http://localhost:8000/partner/login/](http://localhost:8000/partner/login/)
- **관리 대시보드**: [http://localhost:8000/partner/dashboard/](http://localhost:8000/partner/dashboard/)
- **시스템 관리자**: [http://localhost:8000/admin/](http://localhost:8000/admin/)
- **API 문서 (Swagger)**: [http://localhost:8000/docs/](http://localhost:8000/docs/)

---

## 🧪 테스트 계정 정보 (Test Credentials)

로컬 개발 및 테스트를 위해 다음 계정들을 사용할 수 있습니다.

### 1) 시스템 관리자 (Django Admin)
- **URL**: [http://localhost:8000/admin/](http://localhost:8000/admin/)
- **ID**: `admin`
- **PW**: `admin1234`

### 2) 파트너 센터 (Partner/Manager)
- **URL**: [http://localhost:8000/partner/login/](http://localhost:8000/partner/login/)
- **전화번호**: `010-1234-5678`
- **PW**: `partner1234`

### 3) 고객 테스트 데이터 (Customer)
- **성함**: `홍길동`
- **전화번호**: `010-9999-8888`
- **기타**: 이미 설문 데이터가 등록된 상태입니다.

---

## ☁️ 클라우드 배포 (DevOps)

**AWS Elastic Beanstalk**을 통해 무중단 배포를 지원합니다. 상세 내용은 [docs/devops_guide.md](docs/devops_guide.md)를 참고하세요.

## 🛠️ 기술 스택
- **Backend**: Python 3.10+, Django 5.0, DRF
- **Frontend**: Django Templates, Vanilla JS, CSS3
- **Cloud**: AWS (Elastic Beanstalk, ECR, S3)
- **Database**: Supabase (PostgreSQL), SQLite (Local)
