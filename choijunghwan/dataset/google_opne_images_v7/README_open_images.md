# Google Open Images V7 - 헤어스타일 추천 sLLM 데이터셋

헤어스타일 추천 sLLM 훈련에 필요한 이미지를 **Google Open Images V7**에서 다운로드하는 스크립트 모음입니다.

---

## 📂 파일 구성

| 파일 | 설명 |
|------|------|
| `download_hairstyle_images.py` | **FiftyOne 방식** (권장) — 간단하고 빠름 |
| `download_hairstyle_manual.py` | **수동 방식** — CSV 필터링 후 downloader.py 사용 |

---

## 🎯 다운로드 대상 클래스

헤어스타일 추천 sLLM에 꼭 필요한 클래스만 선택:

| 클래스 | MID | 용도 |
|--------|-----|------|
| **Hair** | `/m/0c_jw` | 머리카락 영역 탐지·세그멘테이션 |
| **Hairstyle** | `/m/01d9ld` | 헤어스타일 분류 레이블 |
| **Human face** | `/m/0dzct` | 얼굴형 파악 |
| **Woman** | `/m/03bt1vf` | 여성 인물 |
| **Man** | `/m/04yx4` | 남성 인물 |

---

## 🚀 방법 1: FiftyOne (권장)

### 설치

```bash
pip install fiftyone
```

### 실행

```bash
python download_hairstyle_images.py
```

### 다운로드 규모 (기본값)

| Split | 최대 샘플 수 |
|-------|------------|
| validation | 500 장 |
| train | 3,000 장 |

> `download_hairstyle_images.py` 상단 `MAX_SAMPLES` 딕셔너리로 조절 가능

---

## 🔧 방법 2: 수동 다운로드

FiftyOne 설치가 어려운 환경에서 사용.

### 설치

```bash
pip install requests pandas tqdm boto3
```

### 실행

```bash
python download_hairstyle_manual.py
```

스크립트가 자동으로:
1. Open Images 어노테이션 CSV 다운로드
2. 헤어스타일 관련 이미지 ID 필터링
3. `downloader.py` 다운로드 및 실행 안내

---

## 📁 출력 디렉터리 구조

```
open_images_hairstyle/          # FiftyOne 방식
├── train/
│   └── data/                   # 이미지 파일
└── validation/
    └── data/

open_images_hairstyle_manual/   # 수동 방식
├── images/
│   ├── train/
│   └── validation/
├── csv/                        # 어노테이션 CSV
└── hairstyle_image_list.txt
```

---

## 💡 sLLM 학습용 데이터 활용 팁

1. **얼굴 + 헤어 세그멘테이션** 쌍으로 구성하면 멀티모달 입력에 활용도 높음
2. **Hair** 클래스의 인스턴스 세그멘테이션 마스크를 활용해 헤어 영역만 크롭 가능
3. 메타데이터 CSV에 포함된 `OriginalURL`로 고해상도 원본 접근 가능
4. Open Images Extended의 **MIAP**(More Inclusive Annotations for People) 데이터셋에는 인종·연령 다양성 어노테이션 포함

---

## 📜 라이선스

Open Images V7 데이터셋은 [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/) 라이선스.  
상업적 이용 가능, **출처 표기 필수**.
