@echo off
REM =====================================================================
REM Reverse Remote Play — PC Quick Runner (Windows)
REM =====================================================================
REM اختصار لتشغيل مكونات PC من داخل مجلد pc.
REM =====================================================================

setlocal enabledelayedexpansion
set "PYTHON=python"

echo.
echo ============================================================
echo  Reverse Remote Play — PC Runner
echo ============================================================
echo.
echo [1] receive_input.py --debug    (المستقبل - يستمع على UDP 47001)
echo [2] fake_ps4_input.py           (محاكي PS4 للإختبار المحلي)
echo [3] send_mjpeg.py --test-pattern(مرسل فيديو اختباري)
echo [4] تثبيت المتطلبات (pip install)
echo [0] خروج
echo.
set /p c="اختر رقم: "

if "%c%"=="1" (
    %PYTHON% receive_input.py --debug
    pause
    goto :eof
)
if "%c%"=="2" (
    %PYTHON% tools\fake_ps4_input.py --btn cross --rate 10 --debug
    pause
    goto :eof
)
if "%c%"=="3" (
    %PYTHON% send_mjpeg.py --test-pattern --no-send
    pause
    goto :eof
)
if "%c%"=="4" (
    %PYTHON% -m pip install -r requirements.txt
    pause
    goto :eof
)
if "%c%"=="0" (
    goto :eof
)
echo اختيار غير صحيح.
pause