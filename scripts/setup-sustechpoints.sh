#!/bin/bash
set -euo pipefail

# ==============================================================================
# SUSTechPOINTS — 3D Point Cloud Annotation Tool Setup
#
# Web-based tool for drawing 7-DOF bounding boxes on point clouds.
# Runs on EC2, accessed via browser.
#
# Usage:
#   bash scripts/setup-sustechpoints.sh
#   # Then open http://<ec2-ip>:8081 in your browser
#
# Annotation workflow:
#   1. Copy PLY files into tools/SUSTechPOINTS/data/<scene>/
#   2. Open the web UI, select scene
#   3. Ctrl + drag to draw bounding boxes around doors
#   4. Set label to "door"
#   5. Save annotations (auto-saved as JSON)
#   6. Run scripts/convert-sustechpoints.py to convert to our format
# ==============================================================================

echo "============================================"
echo "Setting up SUSTechPOINTS annotation tool"
echo "============================================"

TOOLS_DIR="tools"
SUSTECHPOINTS_DIR="${TOOLS_DIR}/SUSTechPOINTS"

# A fixed commit (the head of the default branch, dev-auto-annotate, 2024-06-13),
# fetched by its id and checked after checkout, rather than whatever the branch points
# at on the day. Bump it deliberately.
SUSTECHPOINTS_COMMIT="50fa18871a6d46507ff9057175669235885f79f9"

# Clone if not present
if [ ! -d "${SUSTECHPOINTS_DIR}" ]; then
    mkdir -p "${SUSTECHPOINTS_DIR}"
    git -C "${SUSTECHPOINTS_DIR}" init -q
    git -C "${SUSTECHPOINTS_DIR}" fetch -q --depth 1 \
        https://github.com/naurril/SUSTechPOINTS.git "${SUSTECHPOINTS_COMMIT}"
    git -C "${SUSTECHPOINTS_DIR}" checkout -q FETCH_HEAD
fi
if [ "$(git -C "${SUSTECHPOINTS_DIR}" rev-parse HEAD)" != "${SUSTECHPOINTS_COMMIT}" ]; then
    echo "${SUSTECHPOINTS_DIR} is not at ${SUSTECHPOINTS_COMMIT}; remove it and re-run." >&2
    exit 1
fi

cd "${SUSTECHPOINTS_DIR}"

# Install Python dependencies, at exact versions. numpy stays on 1.x, which the tool
# was written against.
pip install flask==3.1.3 numpy==1.26.4 cherrypy==18.10.0

# Disable TensorFlow dependency (auto-annotate not needed for manual annotation)
sed -i 's/from algos import pre_annotate/# from algos import pre_annotate/' main.py 2>/dev/null || true

# Create data directory structure for our scenes
mkdir -p data

echo ""
echo "============================================"
echo "SUSTechPOINTS installed."
echo ""
echo "To prepare point clouds for annotation:"
echo "  python ../../scripts/prepare-for-annotation.py <your-file.ply> <scene-name>"
echo ""
echo "To start the annotation server:"
echo "  cd ${SUSTECHPOINTS_DIR}"
echo "  python main.py --port 8081"
echo ""
echo "Then open: http://<ec2-ip>:8081"
echo ""
echo "After annotating, convert to our format:"
echo "  python ../../scripts/convert-sustechpoints.py <scene-name>"
echo "============================================"
