@echo off
chcp 65001 >nul
title JLCPCB 離線元件搜尋器 (Vue 3 + SQLite)
cd /d "%~dp0"

if not exist "data\jlc_parts.db" (
    echo [初始化] 尚未偵測到離線 SQLite 資料庫，正在為您從 JLCPCB 下載 MOSFETs ^(2954^) 有庫存元件...
    python jlc_fetcher.py --max 5000 --out data/jlc_parts.db
)

echo.
echo 正在啟動本地網頁搜尋介面 (http://127.0.0.1:8080)...
python app.py
pause
