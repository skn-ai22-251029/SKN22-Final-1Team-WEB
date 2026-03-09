"""
Google Open Images V7 - 헤어스타일 추천 sLLM용 이미지 다운로드 스크립트
===========================================================================
대상 클래스:
  - Hair         (/m/0c_jw)   : 머리카락
  - Hairstyle    (/m/01d9ld)  : 헤어스타일
  - Human face   (/m/0dzct)   : 얼굴 (얼굴형 확인)
  - Woman        (/m/03bt1vf) : 여성
  - Man          (/m/04yx4)   : 남성
  - Human hair color (/m/0k0pj): 머리카락 색상

다운로드 방법: FiftyOne (공식 권장 방법)
  pip install fiftyone
  pip install fiftyone-db-ubuntu2004  # (리눅스 환경이면)
===========================================================================
"""

import os

# ─────────────────────────────────────
# 설정 (필요 시 수정)
# ─────────────────────────────────────
OUTPUT_DIR = "./open_images_hairstyle"   # 저장 경로

# 다운로드할 Split: "train" | "validation" | "test"
# 빠른 탐색: validation, 대규모 학습: train
SPLITS = ["validation", "train"]

# 스플릿별 최대 샘플 수 (None = 제한 없음)
MAX_SAMPLES = {
    "validation": 500,   # validation 전체 (~4만 장 중 Hair 포함 이미지)
    "train": 3000,       # train은 일단 3,000장으로 제한
}

# 헤어스타일 추천에 필요한 핵심 클래스
HAIRSTYLE_CLASSES = [
    "Hair",            # 머리카락 (탐지/세그멘테이션)
    "Hairstyle",       # 헤어스타일 레이블
    "Human face",      # 얼굴형 파악
    "Woman",           # 여성
    "Man",             # 남성
]

# 다운로드할 어노테이션 타입
LABEL_TYPES = [
    "detections",      # 바운딩박스
    "segmentations",   # 인스턴스 세그멘테이션 (Hair 영역)
    "classifications", # 이미지 레벨 레이블
]

# ─────────────────────────────────────
# 다운로드 실행
# ─────────────────────────────────────
try:
    import fiftyone as fo
    import fiftyone.zoo as foz
except ImportError:
    print("=" * 60)
    print("[오류] fiftyone 미설치. 아래 명령어로 설치하세요:")
    print()
    print("  pip install fiftyone")
    print()
    print("설치 후 다시 실행하세요.")
    print("=" * 60)
    exit(1)


def download_split(split: str):
    """지정된 split의 헤어스타일 관련 이미지를 다운로드"""
    print(f"\n{'='*60}")
    print(f"  Split: {split}  |  Classes: {HAIRSTYLE_CLASSES}")
    print(f"  Max samples: {MAX_SAMPLES.get(split, 'All')}")
    print(f"{'='*60}")

    dataset = foz.load_zoo_dataset(
        "open-images-v7",
        split=split,
        label_types=LABEL_TYPES,
        classes=HAIRSTYLE_CLASSES,
        max_samples=MAX_SAMPLES.get(split),
        dataset_dir=os.path.join(OUTPUT_DIR, split),
        # 이미 다운로드된 데이터가 있으면 이름 충돌 방지
        dataset_name=f"hairstyle_{split}",
        overwrite=True,
    )

    print(f"\n✅ {split} 완료: {len(dataset)} 장 다운로드")

    # 간단한 통계 출력
    print(f"\n[{split}] 클래스별 샘플 수:")
    for cls in HAIRSTYLE_CLASSES:
        count = dataset.filter_labels("detections", fo.ViewField("label") == cls).count()
        print(f"  - {cls}: {count}")

    return dataset


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"저장 경로: {os.path.abspath(OUTPUT_DIR)}")

    all_datasets = {}
    for split in SPLITS:
        ds = download_split(split)
        all_datasets[split] = ds

    print("\n" + "=" * 60)
    print("🎉 전체 다운로드 완료!")
    for split, ds in all_datasets.items():
        print(f"  {split}: {len(ds)} 장")
    print(f"\n저장 위치: {os.path.abspath(OUTPUT_DIR)}")
    print("=" * 60)

    # FiftyOne App으로 시각화 (선택)
    view_flag = input("\nFiftyOne App으로 결과 확인하시겠습니까? (y/N): ").strip().lower()
    if view_flag == "y":
        session = fo.launch_app(all_datasets["validation"])
        session.wait()


if __name__ == "__main__":
    main()
