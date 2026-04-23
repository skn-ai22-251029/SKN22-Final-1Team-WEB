#!/bin/bash
set -euo pipefail

GET_CONFIG_BIN="/opt/elasticbeanstalk/bin/get-config"
DEFAULT_MOUNT_POINT="/mnt/mirrai-ncs-pdfs"

skip_mount() {
  echo "[eb-ncs-efs] warning: $1; continuing without EFS mount."
  exit 0
}

run_mount() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "${MOUNT_TIMEOUT_SECONDS}" "$@"
  else
    "$@"
  fi
}

read_env() {
  local key="$1"
  local value=""

  if [ -n "${!key:-}" ]; then
    printf '%s' "${!key}"
    return 0
  fi

  if [ -x "${GET_CONFIG_BIN}" ]; then
    value="$("${GET_CONFIG_BIN}" environment -k "${key}" 2>/dev/null || true)"
    if [ -n "${value}" ] && [ "${value}" != "null" ]; then
      printf '%s' "${value}"
      return 0
    fi
  fi

  return 1
}

append_fstab_entry() {
  local entry="$1"
  local mount_point="$2"

  if grep -qs "[[:space:]]${mount_point}[[:space:]]" /etc/fstab; then
    return 0
  fi

  printf '%s\n' "${entry}" >> /etc/fstab
}

EFS_FILE_SYSTEM_ID="$(read_env NCS_EFS_FILE_SYSTEM_ID || true)"
EFS_ACCESS_POINT_ID="$(read_env NCS_EFS_ACCESS_POINT_ID || true)"
EFS_REGION="$(read_env NCS_EFS_REGION || read_env AWS_REGION || read_env AWS_DEFAULT_REGION || true)"
EFS_MOUNT_POINT="$(read_env NCS_EFS_MOUNT_POINT || read_env NCS_PDF_SYNC_SOURCE_DIR || true)"
MOUNT_TIMEOUT_SECONDS="$(read_env NCS_EFS_MOUNT_TIMEOUT_SECONDS || printf '15')"
INSTALL_UTILS_ENABLED="$(read_env NCS_EFS_INSTALL_UTILS || printf '0')"

if [ -z "${EFS_MOUNT_POINT}" ]; then
  EFS_MOUNT_POINT="${DEFAULT_MOUNT_POINT}"
fi

if [ -z "${EFS_FILE_SYSTEM_ID}" ]; then
  echo "[eb-ncs-efs] NCS_EFS_FILE_SYSTEM_ID not set; skipping EFS mount."
  exit 0
fi

if [ -z "${EFS_REGION}" ]; then
  skip_mount "NCS_EFS_REGION or AWS_REGION is required when NCS_EFS_FILE_SYSTEM_ID is set"
fi

mkdir -p "${EFS_MOUNT_POINT}"

if mountpoint -q "${EFS_MOUNT_POINT}"; then
  echo "[eb-ncs-efs] ${EFS_MOUNT_POINT} is already mounted."
  exit 0
fi

if [ -n "${EFS_ACCESS_POINT_ID}" ] && ! command -v mount.efs >/dev/null 2>&1; then
  if [ "${INSTALL_UTILS_ENABLED}" != "1" ]; then
    skip_mount "NCS_EFS_ACCESS_POINT_ID requires amazon-efs-utils; set NCS_EFS_INSTALL_UTILS=1 to allow host package install"
  fi

  if command -v dnf >/dev/null 2>&1; then
    run_mount dnf install -y amazon-efs-utils || skip_mount "amazon-efs-utils install failed or timed out after ${MOUNT_TIMEOUT_SECONDS}s"
  elif command -v yum >/dev/null 2>&1; then
    run_mount yum install -y amazon-efs-utils || skip_mount "amazon-efs-utils install failed or timed out after ${MOUNT_TIMEOUT_SECONDS}s"
  else
    skip_mount "amazon-efs-utils is required for access point mounts"
  fi
fi

if command -v mount.efs >/dev/null 2>&1; then
  EFS_OPTIONS="tls,_netdev,nofail"
  if [ -n "${EFS_ACCESS_POINT_ID}" ]; then
    EFS_OPTIONS="${EFS_OPTIONS},accesspoint=${EFS_ACCESS_POINT_ID}"
  fi

  if run_mount mount -t efs -o "${EFS_OPTIONS}" "${EFS_FILE_SYSTEM_ID}:/" "${EFS_MOUNT_POINT}"; then
    append_fstab_entry \
      "${EFS_FILE_SYSTEM_ID}:/ ${EFS_MOUNT_POINT} efs ${EFS_OPTIONS} 0 0" \
      "${EFS_MOUNT_POINT}"
    chmod 0775 "${EFS_MOUNT_POINT}" || true
    echo "[eb-ncs-efs] Mounted ${EFS_FILE_SYSTEM_ID} to ${EFS_MOUNT_POINT}."
    exit 0
  fi

  skip_mount "mount.efs failed or timed out after ${MOUNT_TIMEOUT_SECONDS}s"
else
  if [ -n "${EFS_ACCESS_POINT_ID}" ]; then
    skip_mount "NCS_EFS_ACCESS_POINT_ID requires amazon-efs-utils on the host"
  fi

  NFS_SOURCE="${EFS_FILE_SYSTEM_ID}.efs.${EFS_REGION}.amazonaws.com:/"
  NFS_OPTIONS="nfsvers=4.1,rsize=1048576,wsize=1048576,soft,timeo=50,retrans=2,noresvport,_netdev,nofail"

  if run_mount mount -t nfs4 -o "${NFS_OPTIONS}" "${NFS_SOURCE}" "${EFS_MOUNT_POINT}"; then
    append_fstab_entry \
      "${NFS_SOURCE} ${EFS_MOUNT_POINT} nfs4 ${NFS_OPTIONS} 0 0" \
      "${EFS_MOUNT_POINT}"
    chmod 0775 "${EFS_MOUNT_POINT}" || true
    echo "[eb-ncs-efs] Mounted ${EFS_FILE_SYSTEM_ID} to ${EFS_MOUNT_POINT}."
    exit 0
  fi

  skip_mount "nfs4 mount failed or timed out after ${MOUNT_TIMEOUT_SECONDS}s"
fi
