# Backend Additional Issues

기준일: 2026-03-24

참고: 현재 `.gitignore`에 `docs/`가 포함되어 있어 이 문서는 기본 상태로는 Git 추적 대상이 아닙니다.

---

## 기존 issue 목록에는 없지만 반영된 항목

아래 이슈들은 2026-03-23 기준 issue 목록에는 없었지만, 기획서/화면설계도와 실제 구현 내역을 기준으로 보면 별도 이슈로 봐도 되는 항목들입니다.

---

## 관리자 회원가입 동의 검증
### 어떤 기능인가요?
관리자 회원가입 시 필수 약관 동의 항목을 검증하고 저장합니다.

> 관리자 화면설계도의 회원가입 흐름에 맞춰 필수 동의값이 없으면 가입이 되지 않도록 처리합니다.

### 작업 상세 내용

- [x] `agree_terms`, `agree_privacy`, `agree_third_party_sharing` 필수 입력 검증 추가
- [x] 동의값을 `consent_snapshot`으로 저장하도록 반영
- [x] 동의 시각 `consented_at` 저장 반영
- [x] 미동의 시 회원가입 실패 응답 테스트 추가
- [x] 관리자 프로필 응답에서 동의 정보 조회 가능하도록 정리

### 참고할만한 자료(선택)
- `app/api/v1/admin_serializers.py`
- `app/models_django.py`

---

## 벡터 전용 이미지 비저장 정책
### 어떤 기능인가요?
클라이언트 촬영 이미지와 추천 이미지를 기본적으로 저장하지 않고, 벡터/스냅샷 중심으로 유지하는 정책입니다.

> 기획상 이미지 원본을 최소 보관하고 분석 결과와 재생성 정보만 남기도록 백엔드 기본 동작을 바꿉니다.

### 작업 상세 내용

- [x] 캡처 업로드 기본 정책을 `vector_only`로 정리
- [x] 추천 이력 저장 시 이미지 경로 대신 `regeneration_snapshot` 저장 반영
- [x] 응답에서 `image_policy`, `can_regenerate_simulation` 필드 제공
- [x] 관리자 상세와 추천 응답에서 `vector_only` 상태를 조회할 수 있게 정리
- [x] `vector_only` 정책 테스트 추가

### 참고할만한 자료(선택)
- `docs/backend_contract_handoff_2026-03-23.md`
- `app/api/v1/services_django.py`

---

## 관리자 로그인 / 인증 보호
### 어떤 기능인가요?
관리자 계정이 로그인한 뒤, 보호된 관리자 API에만 접근할 수 있도록 인증 체계를 분리합니다.

> B2B 관리자 화면설계도 기준으로 로그인, 토큰 검증, 보호 endpoint 접근이 가능해야 합니다.

### 작업 상세 내용

- [x] 관리자 회원가입/로그인 endpoint를 별도로 정리
- [x] 관리자 전용 signed bearer token 발급 및 검증 로직 반영
- [x] `GET /api/v1/admin/auth/me/` endpoint 추가
- [x] dashboard, clients, consultation, report endpoint에 관리자 인증 보호 적용
- [x] 관리자 로그인 및 보호 endpoint 접근 테스트 추가

### 참고할만한 자료(선택)
- `docs/frontend_reference_pack_2026-03-23.md`
- `app/api/v1/admin_auth.py`

---

## 연령 입력 및 연령 프로필 저장
### 어떤 기능인가요?
클라이언트 등록 시 나이를 받고, 연령대/연령 구간 정보를 함께 계산할 수 있도록 저장합니다.

> 숫자 나이 입력을 받아 추천과 트렌드 분석에 쓸 수 있는 연령 프로필로 변환합니다.

### 작업 상세 내용

- [x] 클라이언트 등록 시 `age`와 `ages` 입력을 모두 받을 수 있게 반영
- [x] `age_input`과 `birth_year_estimate` 저장 로직 추가
- [x] `age_decade`, `age_segment`, `age_group` 계산 로직 반영
- [x] 신년 기준으로 DB 재기록 없이 나이가 자동 갱신되도록 정리
- [x] 연령 입력/계산 테스트 추가

### 참고할만한 자료(선택)
- `app/services/age_profile.py`
- `app/tests/test_client_age_features.py`

---

## 연령대 기반 트렌드 분석 지원
### 어떤 기능인가요?
클라이언트 연령대에 맞는 트렌드 추천과 관리자 리포트 필터를 제공합니다.

> 기획상 트렌드를 연령대별로 보고 싶다는 요구를 반영해 age group/decade 기준 분석을 추가합니다.

### 작업 상세 내용

- [x] 클라이언트 trend API에서 `age_group` 우선 스코프 적용
- [x] 필요 시 `age_decade` fallback 스코프 적용
- [x] 관리자 trend report에 `age_decade`, `age_group` 필터 추가
- [x] 연령대 분포 집계(`age_decade_distribution`, `age_group_distribution`) 반영
- [x] 연령대 기준 trend/report 테스트 추가

### 참고할만한 자료(선택)
- `app/api/v1/services_django.py`
- `app/api/v1/admin_services.py`

---

## 프론트엔드-백엔드 계약 동기화
### 어떤 기능인가요?
프론트 화면 구조와 Django API 계약을 1:1로 맞추는 동기화 이슈입니다.

> 프론트 구현 시 화면명, 상태값, 요청/응답 필드가 어긋나지 않도록 최종 계약을 고정합니다.

### 작업 상세 내용

- [ ] 화면 목록 기준으로 `client flow`와 `admin flow`를 다시 매핑
- [ ] 각 화면별 사용 API, 필수 입력값, 성공 응답값을 최종 확정
- [ ] `status`, `next_action`, `next_actions`를 프론트 라우팅 규칙과 연결
- [ ] `client_input`, `capture`, `ready`, `empty` 같은 상태값을 실제 화면 route와 매칭
- [ ] 프론트 팀이 바로 사용할 수 있는 최신 contract 문서와 예시 payload를 갱신

#### 프론트 교차검증 체크리스트

- [ ] 화면 route와 API endpoint 매핑이 실제 구현과 일치하는지 확인
- [ ] `status`, `next_action`, `next_actions` 값으로 화면 분기가 가능한지 확인
- [ ] `client_input`, `capture`, `ready`, `empty` 상태가 실제 화면 이름과 맞는지 확인
- [ ] 프론트에서 사용하는 필드명이 현재 contract 문서와 일치하는지 확인

### 참고할만한 자료(선택)
- `docs/backend_contract_handoff_2026-03-23.md`
- `docs/frontend_reference_pack_2026-03-23.md`

---

## 벡터 전용 정책 UI 처리
### 어떤 기능인가요?
`vector_only` 정책에서 `simulation_image_url`가 없을 때도 프론트가 자연스럽게 동작하도록 화면 정책을 정의하는 이슈입니다.

> 현재 백엔드는 이미지 비저장 정책을 따르므로, 프론트는 `null` 이미지와 재생성 가능 상태를 함께 처리해야 합니다.

### 작업 상세 내용

- [ ] 추천 카드에서 `simulation_image_url=null`인 경우의 기본 UI 확정
- [ ] `sample_image_url`과 실제 시뮬레이션 이미지의 역할을 분리해 문서화
- [ ] `image_policy=vector_only` 노출 여부와 문구 기준 정리
- [ ] 재생성 버튼, placeholder, 안내 문구를 프론트와 합의
- [ ] 관리자 상세 화면과 고객 추천 화면의 처리 방식을 일관되게 맞춤

#### 프론트 교차검증 체크리스트

- [ ] 추천 카드에서 `simulation_image_url=null`일 때 placeholder가 정상 표시되는지 확인
- [ ] `sample_image_url`만으로도 카드 UI가 깨지지 않는지 확인
- [ ] `image_policy=vector_only` 안내 문구를 어디에 노출할지 화면 기준으로 확정
- [ ] 관리자 화면과 고객 화면에서 null 이미지 처리 방식이 일관적인지 확인

### 참고할만한 자료(선택)
- `docs/frontend_reference_pack_2026-03-23.md`
- `app/api/v1/django_serializers.py`

---

## 추천 시뮬레이션 재생성 API 제공
### 어떤 기능인가요?
`vector_only` 정책 아래에서도 추천 결과를 다시 시뮬레이션할 수 있는 공개 API를 제공하는 이슈입니다.

> 현재 추천 응답에는 `can_regenerate_simulation=true`가 있지만, 프론트가 직접 호출할 공개 재생성 API는 아직 없습니다.

### 작업 상세 내용

- [ ] 클라이언트 또는 관리자 화면에서 호출할 재생성 endpoint 정의
- [ ] `recommendation_id` 또는 `regeneration_snapshot` 기반 재생성 입력 스키마 확정
- [ ] 내부 AI 서비스와 Django public API 사이의 연결 경로 설계
- [ ] 재생성 성공/실패/대기 상태 응답 형식 정의
- [ ] `simulation_image_url`가 `null`인 카드와 재생성 버튼 동작을 프론트와 동기화

#### 프론트 교차검증 체크리스트

- [ ] 재생성 버튼을 어떤 화면에 둘지 확정
- [ ] `recommendation_id`를 프론트 상태에서 안정적으로 보관하는지 확인
- [ ] 재생성 대기/실패/완료 상태를 어떤 UI로 보여줄지 확정
- [ ] 재생성 후 카드 갱신 방식(재조회/부분 갱신)을 프론트와 맞춤

### 참고할만한 자료(선택)
- `docs/frontend_reference_pack_2026-03-23.md`
- `app/api/v1/services_django.py`
- `main.py`

---

## 관리자 매장 스코프 고도화
### 어떤 기능인가요?
관리자 계정별로 고객, 상담, 리포트가 매장 단위로 명확히 분리되도록 강화하는 이슈입니다.

> 현재 `admin_id` 전달 시 연결은 되지만, 장기적으로는 매장 기준 조회 범위와 데이터 격리를 더 분명하게 해야 합니다.

### 작업 상세 내용

- [ ] 상담 생성, 선택 확정, 메모, 종료 흐름에서 `admin` 연결 규칙 재점검
- [ ] 대시보드, active clients, all clients, report 조회가 매장 기준으로 일관되게 필터링되는지 점검
- [ ] `admin_id`가 없는 확정 흐름에서의 fallback 정책을 명확히 정리
- [ ] 관리자 간 데이터 혼선이 없는지 테스트 케이스 보강
- [ ] B2B 화면설계도 기준으로 `점내 고객`과 `전체 고객` 범위를 문서화

#### 프론트 교차검증 체크리스트

- [ ] 스타일 확정 시 `admin_id`를 함께 보내는지 확인
- [ ] `점내 고객`과 `전체 고객` 화면이 실제로 다른 API/필터를 쓰는지 확인
- [ ] 관리자 계정 전환 시 이전 매장 데이터가 남아 보이지 않는지 확인

### 참고할만한 자료(선택)
- `docs/backend_contract_handoff_2026-03-23.md`
- `app/tests/test_issue_backlog_progress.py`

---

## 클라이언트 인증 체계 고도화
### 어떤 기능인가요?
현재 `mock-token-*` 기반인 클라이언트 인증 흐름을 실제 운영 가능한 토큰 체계로 전환하는 이슈입니다.

> 프론트 동기화가 본격화되기 전에 클라이언트 인증도 관리자 인증 수준에 가깝게 정리할 필요가 있습니다.

### 작업 상세 내용

- [ ] 클라이언트 로그인/회원가입 응답의 실제 토큰 정책 확정
- [ ] 관리자용 Bearer 인증과 클라이언트 인증의 차이를 문서에 분리 명시
- [ ] 프론트 저장 방식(session/local storage 등)에 맞춘 만료/재로그인 정책 정리
- [ ] 보호가 필요한 클라이언트 전용 endpoint 범위 재검토
- [ ] 인증 실패 시 공통 에러 응답과 프론트 분기 방식 정리

#### 프론트 교차검증 체크리스트

- [ ] 클라이언트 토큰 저장 위치(session/local storage)를 프론트에서 확정
- [ ] 로그인 후 새로고침 시 인증 상태 유지 방식 확인
- [ ] 인증 만료 또는 실패 시 이동할 화면과 에러 문구를 확정

### 참고할만한 자료(선택)
- `docs/frontend_reference_pack_2026-03-23.md`
- `app/api/v1/django_views.py`

---

## 동의 기반 이미지 저장 정책 분기
### 어떤 기능인가요?
고객 동의 여부에 따라 `vector_only`와 이미지 저장 정책을 분기하는 이슈입니다.

> 장기적으로는 "기본은 비저장, 동의 시에만 저장" 규칙을 제품 정책으로 굳힐 가능성이 높습니다.

### 작업 상세 내용

- [ ] 고객 동의 입력값을 어느 시점에 받을지 프론트와 합의
- [ ] 동의값을 저장할 모델 필드 또는 스냅샷 구조 설계
- [ ] 업로드 시 동의 여부에 따라 `vector_only`와 asset 저장을 분기
- [ ] 관리자 상세 조회에서 저장된 이미지가 없는 경우의 표시 방식 정리
- [ ] 비동의 상태에서 재생성/이력 조회가 어떻게 동작할지 정책 문서화

#### 프론트 교차검증 체크리스트

- [ ] 동의 버튼/체크박스를 어느 화면에서 받을지 확정
- [ ] 동의하지 않았을 때 촬영 진행 가능 여부를 UX 기준으로 확정
- [ ] 관리자 화면에서 저장된 이미지가 없을 때의 안내 문구를 확정

### 참고할만한 자료(선택)
- `docs/backend_contract_handoff_2026-03-23.md`
- `app/services/storage_service.py`
- `app/api/v1/django_views.py`

---

## 연령대 기반 트렌드 화면 연동
### 어떤 기능인가요?
연령 입력, `age_decade`, `age_segment`, `age_group` 기반 트렌드 기능을 프론트와 연결하는 이슈입니다.

> 백엔드에는 연령대 계산과 필터링이 들어가 있으므로, 이제는 화면에서 이를 어떻게 보여줄지 정리해야 합니다.

### 작업 상세 내용

- [ ] 클라이언트 등록/로그인 이후 연령대 정보를 어떤 화면에서 보여줄지 확정
- [ ] 트렌드 화면에서 `age_group` 기반 추천과 글로벌 추천을 어떻게 구분할지 정의
- [ ] 관리자 trend report에 연령 필터와 연령 분포를 어떻게 노출할지 결정
- [ ] 신년 기준 자동 나이 갱신 로직을 프론트 문구와 동기화
- [ ] 연령 미입력 또는 불완전 데이터 상태의 fallback UI 정리

#### 프론트 교차검증 체크리스트

- [ ] 연령 입력값을 어느 화면에서 받고 어떤 문구로 안내할지 확정
- [ ] `age_decade`, `age_segment`, `age_group`를 어떤 UI 라벨로 보여줄지 확정
- [ ] 트렌드 화면에서 연령대 기반 추천과 일반 추천을 구분해서 보여줄지 확인
- [ ] 연령 미입력 상태의 fallback 문구와 화면 흐름을 확정

### 참고할만한 자료(선택)
- `app/services/age_profile.py`
- `app/tests/test_client_age_features.py`
