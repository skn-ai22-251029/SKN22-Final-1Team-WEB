# Google Open Images V7 헤어스타일 데이터셋 활용 보고서

> **작성일**: 2026-03-09  
> **목적**: 헤어스타일 추천 sLLM 구축을 위한 Open Images V7 데이터 현황 및 활용 방안

---

## 1. 다운로드 개요

| 항목 | 내용 |
|------|------|
| **데이터 출처** | Google Open Images Dataset V7 |
| **다운로드 도구** | FiftyOne v1.13.3 |
| **다운로드 일시** | 2026-03-09 |
| **저장 경로** | `C:\Users\Playdata\fiftyone\open-images-v7\` |
| **총 이미지 수** | **3,500장** (validation 500 + train 3,000) |
| **총 용량** | 약 **1.0 GB**  (이미지 ~870 MB + 어노테이션 CSV) |

---

## 2. 데이터셋 구성

### 2-1. 폴더 구조

```
C:\Users\Playdata\fiftyone\open-images-v7\
├── validation\
│   ├── data\           ← 이미지 500장 (JPG, ~159.9 MB)
│   ├── labels\
│   │   ├── detections.csv          ← 바운딩박스 어노테이션
│   │   ├── classifications.csv     ← 이미지 레벨 레이블
│   │   ├── segmentations.csv       ← 세그멘테이션 좌표
│   │   └── masks\                  ← 세그멘테이션 마스크 (PNG)
│   └── metadata\
│       ├── image_ids.csv           ← 이미지 ID 목록
│       └── classes.csv             ← 클래스 정의
│
└── train\
    ├── data\           ← 이미지 3,000장 (JPG, ~870 MB)
    ├── labels\
    │   ├── detections.csv
    │   ├── classifications.csv
    │   ├── segmentations.csv
    │   └── masks\
    └── metadata\
```

### 2-2. Split별 이미지 현황

| Split | 이미지 수 | 용량 | 용도 |
|-------|----------|------|------|
| **validation** | 500장 | 159.9 MB | 모델 평가·검증 |
| **train** | 3,000장 | ~710 MB | 모델 학습 |
| **합계** | **3,500장** | **~870 MB** | |

---

## 3. 포함된 어노테이션 상세

### 3-1. 다운로드 대상 클래스

| 클래스 (영어) | MID | 의미 | 어노테이션 타입 |
|--------------|-----|------|----------------|
| **Hair** | `/m/0c_jw` | 머리카락 | Bbox + Segmentation |
| **Hairstyle** | `/m/01d9ld` | 헤어스타일 유형 | Classification |
| **Human face** | `/m/0dzct` | 얼굴 영역 | Bbox + Segmentation |
| **Woman** | `/m/03bt1vf` | 여성 인물 | Bbox |
| **Man** | `/m/04yx4` | 남성 인물 | Bbox |

### 3-2. 어노테이션 타입별 설명

#### ① Bounding Box (`detections.csv`)
- 각 이미지에 존재하는 객체의 위치 정보 (x_min, y_min, x_max, y_max)
- validation 기준: 약 **18만 개** 바운딩박스 어노테이션
- train 기준: 약 **268만 개** 바운딩박스 어노테이션
- 포함 컬럼: `ImageID`, `LabelName`, `XMin`, `XMax`, `YMin`, `YMax`, `Confidence`

```
ImageID,         LabelName,   XMin, XMax, YMin, YMax, Confidence
00075905539074f2, /m/0dzct,   0.31, 0.68, 0.05, 0.59, 1
00075905539074f2, /m/04yx4,   0.22, 0.78, 0.0,  0.99, 1
```

#### ② Instance Segmentation (`segmentations.csv` + `masks/`)
- Hair 클래스의 **픽셀 단위 마스크** 제공
- 마스크 PNG 파일: 이미지와 동일한 해상도, 해당 영역만 흰색
- validation 기준: 처리된 세그멘테이션 어노테이션 존재
- **헤어스타일 추천에서 가장 핵심적인 데이터**

#### ③ Image Classification (`classifications.csv`)
- 이미지 전체에 대한 레이블 (바운딩박스 없음)
- Hairstyle 여부, 사람 존재 여부 등 이미지 수준 레이블

---

## 4. 데이터 품질 및 다양성

### 4-1. 인물 다양성
Open Images는 전 세계의 다양한 이미지를 수집하므로:
- **다양한 인종**: 동남아시아, 유럽, 아프리카, 동아시아 인물 포함
- **다양한 성별**: Woman / Man 레이블로 구분
- **다양한 헤어스타일**: 단발, 장발, 곱슬, 직모, 묶음, 민머리 등 포함

### 4-2. 이미지 품질
- 해상도: 이미지마다 다르나 평균 **1,000 × 700px** 이상
- 형식: JPEG, 크기 40KB ~ 11MB 
- 배경: 실내·실외·전신·반신·얼굴 클로즈업 등 다양

---

## 5. 헤어스타일 추천 sLLM 활용 방안

### 5-1. 멀티모달 입력 파이프라인

```
사용자 얼굴 사진
      │
      ▼
[얼굴 탐지] ← Human face BBox 어노테이션으로 학습
      │
      ▼
[헤어 세그멘테이션] ← Hair Segmentation Mask로 학습
      │              → 현재 헤어스타일 인식
      ▼
[sLLM 추천 엔진] ← Hairstyle Classification 레이블로 학습
      │
      ▼
  추천 결과 텍스트 + 참조 이미지
```

### 5-2. 단계별 활용 계획

| 단계 | 활용 데이터 | 학습 목표 |
|------|------------|----------|
| **1단계: 전처리** | `Human face` Bbox | 입력 이미지에서 얼굴 영역 자동 크롭 |
| **2단계: 헤어 인식** | `Hair` Segmentation Mask | 현재 헤어스타일 영역 추출 |
| **3단계: 스타일 분류** | `Hairstyle` Classification | 헤어스타일 유형 분류 (단발·장발·웨이브 등) |
| **4단계: 추천** | 전체 이미지 + 레이블 | 얼굴형·성별 기반 맞춤 추천 텍스트 생성 |

### 5-3. 구체적인 코드 활용 예시

```python
import fiftyone as fo

# 학습용 데이터셋 로드
dataset = fo.load_dataset("hairstyle_train")

# Hair 세그멘테이션이 있는 이미지만 필터
hair_seg_view = dataset.filter_labels(
    "segmentations",
    fo.ViewField("label") == "Hair"
)

# 얼굴 + 헤어 둘 다 있는 이미지
face_hair_view = dataset.match(
    (fo.ViewField("detections.detections.label") == "Hair").length() > 0
)

print(f"Hair 세그멘테이션 포함 이미지: {len(hair_seg_view)}")
print(f"얼굴+헤어 동시 포함 이미지: {len(face_hair_view)}")
```

### 5-4. 데이터 증강 전략

현재 3,500장으로 sLLM 파인튜닝에는 다소 부족할 수 있으므로:

| 증강 기법 | 적용 이유 |
|----------|----------|
| **수평 반전 (Flip)** | 헤어스타일은 좌우 대칭으로 학습 가능 |
| **색상 변환 (Color Jitter)** | 조명 변화에 강건한 모델 학습 |
| **랜덤 크롭** | 다양한 얼굴 위치에 대한 일반화 |
| **밝기·대비 조정** | 다양한 촬영 환경 시뮬레이션 |

---

## 6. 추가 데이터 확보 방안

현재 3,500장은 탐색적 학습에 충분하나, 고품질 sLLM 구축을 위해 아래 방법으로 확대 권장:

### 방법 A: Open Images 추가 다운로드
```python
# 현재 스크립트에서 max_samples 조정
MAX_SAMPLES = {"train": 10000}  # 1만 장으로 확대
```

### 방법 B: 공개 헤어스타일 특화 데이터셋 추가
| 데이터셋 | 이미지 수 | 특징 |
|---------|---------|------|
| **CelebA** | 202,599장 | 유명인 얼굴 + 40개 속성 레이블 |
| **FairFace** | 97,698장 | 인종 다양성 균형 잡힌 데이터셋 |
| **Helen Dataset** | 2,330장 | 고해상도 얼굴 랜드마크 |
| **FFHQ** | 70,000장 | 고품질 다양한 인물 얼굴 |

### 방법 C: Open Images Extended (MIAP)
- 인종·연령·성별 다양성 어노테이션이 추가된 확장 데이터셋
- `https://storage.googleapis.com/openimages/web/extended.html`

---

## 7. 다음 단계 권장 사항

```
[ ] 1. 이미지 EDA (탐색적 데이터 분석)
       - FiftyOne App으로 시각화: python -c "import fiftyone as fo; fo.launch_app(fo.load_dataset('hairstyle_validation'))"
       - 헤어스타일 유형별 이미지 수 파악

[ ] 2. 전처리 파이프라인 구축
       - 얼굴 크롭 (MTCNN / MediaPipe Face Detection)
       - Hair 마스크를 이용한 헤어 영역 크롭

[ ] 3. 헤어스타일 레이블링 확장
       - 현재 Open Images는 'Hair' 단일 클래스
       - GPT-4V 또는 수동으로 세부 헤어스타일 분류 추가
         (단발, 장발, 웨이브, 곱슬, 포니테일, 업스타일 등)

[ ] 4. sLLM 파인튜닝
       - Base Model: LLaVA / InternVL / Qwen-VL 등 멀티모달 sLLM
       - LoRA/QLoRA로 경량 파인튜닝
       - 입력: 얼굴 이미지 + "어떤 헤어스타일을 추천해줘?"
       - 출력: 추천 헤어스타일 텍스트 + 이유

[ ] 5. 평가 지표 설정
       - 정확도: 헤어스타일 분류 정확도
       - 다양성: 추천 결과의 헤어스타일 분포
       - 사용자 만족도: A/B 테스트
```

---

## 8. 요약

| 항목 | 내용 |
|------|------|
| 총 이미지 | 3,500장 (validation 500 + train 3,000) |
| 핵심 어노테이션 | Hair 세그멘테이션 마스크, Human face Bbox, 성별 레이블 |
| 즉시 활용 가능 용도 | 얼굴 탐지 학습, 헤어 영역 세그멘테이션 학습 |
| 향후 필요 작업 | 세부 헤어스타일 레이블링, 데이터 증강, sLLM 파인튜닝 |
| 라이선스 | CC BY 4.0 (출처 표기 필수) |

> **결론**: 현재 다운로드된 3,500장의 데이터는 헤어스타일 추천 sLLM의 기초 파이프라인(얼굴 탐지 → 헤어 세그멘테이션 → 스타일 인식) 구축에 충분하며, 세부 헤어스타일 분류를 위한 추가 레이블링 작업과 CelebA 등 보완 데이터가 병행되면 더욱 강력한 모델을 만들 수 있습니다.
