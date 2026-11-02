@echo off
chcp 65001 >nul
title 消防人員裝備影像偵測系統 - 啟動終端機

echo ===================================================
echo     歡迎使用 消防人員裝備影像偵測系統
echo ===================================================
echo.

:: 檢查是否有 .git 資料夾，有的話才執行 git pull，避免非 git 專案報紅字
if exist .git (
    echo [1/3] 正在檢查並下載最新版本...
    git pull origin main
) else (
    echo [1/3] 非 Git 專案，跳過版本更新檢查。
)

echo.
echo [2/3] 正在升級 pip 並安裝必要的 Python 套件，請稍候...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo.
echo [3/3] 準備啟動系統...
echo.
echo 提示：請勿關閉此黑盒子視窗。若要結束系統，請直接關閉此視窗。
echo ===================================================

:: 啟動背景計時器，等待 3 秒後自動開啟預設瀏覽器進入網頁
start /b cmd /c "timeout /t 3 >nul & start http://127.0.0.1:7860"

:: 啟動主程式
python 主頁.py

pause