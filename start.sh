#!/bin/bash
# ─────────────────────────────────────────────
# RTTM Engine — Quick Start (no Docker needed)
# ─────────────────────────────────────────────
set -e

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   RTTM Engine — INTECH Process Auto.    ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# 1. Install dependencies
echo "▸ Installing Python dependencies..."
pip install -r requirements.txt --quiet

# 2. Start API server
echo "▸ Starting RTTM API on http://localhost:8000"
echo "▸ Dashboard:  http://localhost:8000/dashboard"
echo "▸ API docs:   http://localhost:8000/docs"
echo ""

uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
