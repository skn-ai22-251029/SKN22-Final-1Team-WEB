"""
Google Open Images V7 - 수동 다운로드 방식 (FiftyOne 미사용)
=============================================================
인터넷 환경 문제나 FiftyOne 설치가 어려운 경우 사용.

과정:
  1. 어노테이션 CSV 다운로드 (자동)
  2. Hair / Hairstyle이 포함된 이미지 ID 필터링
  3. downloader.py로 실제 이미지 다운로드

필요 패키지:
  pip install requests pandas tqdm
  pip install boto3  (AWS S3 경유 다운로드용)
=============================================================
"""

import os
import sys
import requests
import pandas as pd
from pathlib import Path

# ─────────────────────────────────────
# 설정
# ─────────────────────────────────────
OUTPUT_DIR      = Path("./open_images_hairstyle_manual")
DOWNLOAD_FOLDER = OUTPUT_DIR / "images"
CSV_DIR         = OUTPUT_DIR / "csv"

# 헤어스타일 관련 클래스 MID (Machine ID)
# Open Images V7 class-descriptions.csv 기준
TARGET_MIDS = {
    "/m/0c_jw":   "Hair",
    "/m/01d9ld":  "Hairstyle",
    "/m/0dzct":   "Human_face",
    "/m/03bt1vf": "Woman",
    "/m/04yx4":   "Man",
}

# 다운로드할 Split
SPLITS = ["validation", "train"]

# train 이미지 최대 개수 (None = 전체)
TRAIN_LIMIT = 3000

# Open Images V7 어노테이션 CSV URL
CSV_URLS = {
    # 바운딩 박스 어노테이션
    "bbox_train":      "https://storage.googleapis.com/openimages/v6/oidv6-train-annotations-bbox.csv",
    "bbox_validation": "https://storage.googleapis.com/openimages/v5/validation-annotations-bbox.csv",
    "bbox_test":       "https://storage.googleapis.com/openimages/v5/test-annotations-bbox.csv",

    # 이미지 메타데이터
    "meta_train":      "https://storage.googleapis.com/openimages/2018_04/train/train-images-boxable-with-rotation.csv",
    "meta_validation": "https://storage.googleapis.com/openimages/2018_04/validation/validation-images-with-rotation.csv",
    "meta_test":       "https://storage.googleapis.com/openimages/2018_04/test/test-images-with-rotation.csv",
}

# ─────────────────────────────────────
# 헬퍼 함수
# ─────────────────────────────────────

def download_csv(url: str, dest: Path) -> Path:
    """URL에서 CSV 파일을 다운로드. 이미 있으면 스킵."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"  [skip] 이미 존재: {dest.name}")
        return dest

    print(f"  다운로드 중: {dest.name} ...")
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
    print(f"  완료: {dest.name}")
    return dest


def get_image_ids_from_bbox(csv_path: Path, target_mids: dict, limit=None) -> list:
    """바운딩 박스 CSV에서 대상 클래스가 포함된 이미지 ID 추출"""
    print(f"\n  분석 중: {csv_path.name}  (클래스: {list(target_mids.values())})")
    df = pd.read_csv(csv_path, usecols=["ImageID", "LabelName"])
    filtered = df[df["LabelName"].isin(target_mids.keys())]
    image_ids = filtered["ImageID"].unique().tolist()
    if limit:
        image_ids = image_ids[:limit]
    print(f"  → 해당 이미지 수: {len(image_ids)}")
    return image_ids


def save_image_list(image_ids: list, split: str, out_path: Path):
    """downloader.py에서 사용할 이미지 목록 파일 저장"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for img_id in image_ids:
            f.write(f"{split}/{img_id}\n")
    print(f"  저장: {out_path}  ({len(image_ids)} 항목)")


def download_downloader_script(dest: Path):
    """Open Images 공식 downloader.py 다운로드"""
    url = "https://raw.githubusercontent.com/openimages/dataset/master/downloader.py"
    if dest.exists():
        print(f"[skip] downloader.py 이미 존재")
        return
    print("downloader.py 다운로드 중...")
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
    print("완료: downloader.py")


# ─────────────────────────────────────
# 메인 실행
# ─────────────────────────────────────

def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print(" Step 1: 어노테이션 CSV 다운로드")
    print("=" * 60)

    # validation + train bbox CSV만 다운로드 (가장 중요)
    bbox_csvs = {}
    for split in SPLITS:
        url  = CSV_URLS[f"bbox_{split}"]
        dest = CSV_DIR / f"bbox_{split}.csv"
        bbox_csvs[split] = download_csv(url, dest)

    print("\n" + "=" * 60)
    print(" Step 2: 헤어스타일 관련 이미지 ID 필터링")
    print("=" * 60)

    image_list_file = OUTPUT_DIR / "hairstyle_image_list.txt"
    all_ids = []

    for split in SPLITS:
        limit = TRAIN_LIMIT if split == "train" else None
        ids = get_image_ids_from_bbox(bbox_csvs[split], TARGET_MIDS, limit=limit)
        all_ids.extend([(split, img_id) for img_id in ids])

    # 전체 이미지 목록 파일 저장
    with open(image_list_file, "w") as f:
        for split, img_id in all_ids:
            f.write(f"{split}/{img_id}\n")
    print(f"\n총 이미지 수: {len(all_ids)}")
    print(f"이미지 목록 저장: {image_list_file}")

    print("\n" + "=" * 60)
    print(" Step 3: downloader.py 준비")
    print("=" * 60)

    downloader_path = OUTPUT_DIR / "downloader.py"
    download_downloader_script(downloader_path)

    print("\n" + "=" * 60)
    print(" Step 4: 이미지 다운로드 명령어")
    print("=" * 60)
    print()
    print("아래 명령어를 실행하여 이미지를 다운로드하세요:")
    print()
    print(f"  python {downloader_path} \\")
    print(f"      {image_list_file} \\")
    print(f"      --download_folder={DOWNLOAD_FOLDER} \\")
    print(f"      --num_processes=5")
    print()
    print("  (num_processes: 병렬 다운로드 수, 네트워크 상황에 따라 조절)")
    print()

    # 자동 실행 여부 확인
    run_now = input("지금 바로 다운로드를 시작하시겠습니까? (y/N): ").strip().lower()
    if run_now == "y":
        import subprocess
        cmd = [
            sys.executable, str(downloader_path),
            str(image_list_file),
            f"--download_folder={DOWNLOAD_FOLDER}",
            "--num_processes=5",
        ]
        print("\n다운로드 시작...")
        subprocess.run(cmd)
    else:
        print("\n수동으로 위 명령어를 실행해 주세요.")

    print(f"\n저장 위치: {DOWNLOAD_FOLDER.resolve()}")


if __name__ == "__main__":
    main()
