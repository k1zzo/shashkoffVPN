#!/usr/bin/env bash
# generate_xray_protos.sh
# -------------------------------------------------------------------------
# OPTIONAL helper: generate Python protobuf+gRPC stubs from the Xray-core
# `.proto` files needed for HandlerService client management.
#
# The runtime backend/xray_handler_api.py implementation does NOT depend on
# these stubs — it hand-encodes the small number of protobuf messages it
# needs, in the same style as backend/xray_stats.py. This script is provided
# for two reasons:
#
#   1. Reference: makes it easy to inspect the canonical message shapes
#      against the manual encoder/decoder when reviewing changes.
#   2. Extension: if you need to call additional HandlerService RPCs
#      (e.g. AddInbound, ListInbounds), regenerated stubs are the fastest
#      way to bootstrap the new code path.
#
# The script does NOT run automatically. Generated files go to
# backend/xray_proto/ and are NOT committed by default.
#
# -------------------------------------------------------------------------
# USAGE (run from the repository root):
#
#   1. Install build deps in a venv:
#        python3 -m venv .venv && source .venv/bin/activate
#        pip install "grpcio-tools>=1.50,<2.0"
#
#   2. Run this script:
#        bash deploy/generate_xray_protos.sh
#
#   It will:
#     - fetch the four proto files from Xray-core v24.11.30 (the version
#       this repo is pinned to — do NOT bump to 26.x without revisiting)
#       into a temporary directory,
#     - run grpc_tools.protoc to emit *_pb2.py / *_pb2_grpc.py
#       under backend/xray_proto/.
#
#   To use the generated stubs in custom code, import like:
#       from backend.xray_proto.app.proxyman.command import command_pb2
#
# -------------------------------------------------------------------------
set -euo pipefail

XRAY_REF="v24.11.30"
RAW_BASE="https://raw.githubusercontent.com/XTLS/Xray-core/${XRAY_REF}"

PROTO_FILES=(
    "app/proxyman/command/command.proto"
    "common/protocol/user.proto"
    "common/serial/typed_message.proto"
    "proxy/vless/account.proto"
)

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${REPO_ROOT}/backend/xray_proto"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

echo "Fetching proto files from Xray-core ${XRAY_REF}..."
for rel_path in "${PROTO_FILES[@]}"; do
    target="${WORK_DIR}/${rel_path}"
    mkdir -p "$(dirname "${target}")"
    curl -fsSL "${RAW_BASE}/${rel_path}" -o "${target}"
    echo "  - ${rel_path}"
done

echo "Generating Python stubs into ${OUT_DIR}..."
mkdir -p "${OUT_DIR}"

# Touch package __init__.py files so the generated tree is importable as a package.
find "${WORK_DIR}" -type d -print0 | while IFS= read -r -d '' d; do
    rel="${d#${WORK_DIR}}"
    pkg_dir="${OUT_DIR}${rel}"
    mkdir -p "${pkg_dir}"
    : > "${pkg_dir}/__init__.py"
done

python3 -m grpc_tools.protoc \
    -I "${WORK_DIR}" \
    --python_out="${OUT_DIR}" \
    --grpc_python_out="${OUT_DIR}" \
    "${PROTO_FILES[@]/#/${WORK_DIR}/}"

echo "Done. Generated files are under: ${OUT_DIR}"
echo "Note: imports inside generated stubs may need post-processing if you plan to use them — see Xray docs."
