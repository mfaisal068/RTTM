@echo off
python -m pip install -r requirements.txt --quiet
python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload
pause
