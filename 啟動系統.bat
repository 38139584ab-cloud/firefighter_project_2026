@echo off
chcp 65001 >nul
title 消防人員裝備影像偵測系統 - 啟動終端機

echo ===================================================
echo     歡迎使用 消防人員裝備影像偵測系統
echo ===================================================
echo.

:: 🛠️ 新增：自動從 GitHub 拉取最新版本
echo [1/3] 正在檢查並下載最新版本...
git pull origin main

echo.
echo [2/3] 正在檢查並安裝必要的 Python 套件，請稍候...
python -m pip install -r requirements.txt

echo.
echo [3/3] 準備啟動系統...
echo.
echo 提示：請勿關閉此黑盒子視窗。若要結束系統，請直接關閉此視窗。
echo ===================================================

python 主頁.py

pause