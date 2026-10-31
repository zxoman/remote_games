@echo off
chcp 65001 >nul
REM =====================================================================
REM Reverse Remote Play (PS4 homebrew <-> PC) — Windows Build/Run Helper
REM =====================================================================
REM هذا الملف يساعدك على تثبيت المتطلبات وتشغيل مكونات PC على ويندوز.
REM ملاحظة: كود PS4 (C/C++) يُبنى عبر podman على لينكس، وليس مباشرة على ويندوز.
REM =====================================================================

setlocal enabledelayedexpansion

set "ROOT=%~dp0"
set "PC_DIR=%ROOT%pc"
set "PYTHON=python"

echo.
echo ============================================================
echo  Reverse Remote Play — Windows Helper
echo ============================================================
echo.

REM التحقق من وجود بايثون
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] بايثون غير مثبت أو غير موجود في PATH.
    echo         ثبّت Python 3.10+ من https://www.python.org/
    echo         وتأكد من تحديد "Add Python to PATH" أثناء التثبيت.
    pause
    exit /b 1
)

echo [OK] بايثون موجود:
%PYTHON% --version
echo.

REM عرض القائمة
:menu
echo اختر العملية:
echo   [1] تثبيت المتطلبات (pip install -r pc\requirements.txt)
echo   [2] تشغيل مستقبل الإدخال (receive_input.py) مع وضع تصحيح (debug)
echo   [3] تشغيل محاكي PS4 للإدخال (fake_ps4_input.py) للاختبار المحلي
echo   [4] تشغيل مستقبل الإدخال + محاكي PS4 معًا (نافذتان منفصلتان)
echo   [5] تشغيل مرسل الفيديو (send_mjpeg.py) toward PS4
echo   [6] عرض عنوان IPurrent الخاص بحاسبك
echo   [0] خروج
echo.
set /p choice="اختر رقم: "

if "%choice%"=="1" goto install
if "%choice%"=="2" goto run_receiver
if "%choice%"=="3" goto run_fake
if "%choice%"=="4" goto run_both
if "%choice%"=="5" goto run_sender
if "%choice%"=="6" goto show_ip
if "%choice%"=="0" goto end

echo [ERR] اختيار غير صحيح.
goto menu

:install
echo.
echo ============================================================
echo  تثبيت المتطلبات...
echo ============================================================
cd /d "%PC_DIR%"
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERR] فشل تثبيت المتطلبات.
    pause
    goto menu
)
echo [OK] تم تثبيت المتطلبات بنجاح.
pause
goto menu

:run_receiver
echo.
echo ============================================================
echo  تشغيل receive_input.py (المستقبل)
echo ============================================================
echo سيستمع على المنفذ UDP 47001 لاستقبال حزم الإدخال من PS4.
echo تأكد أن جدار حماية ويندوز يسمح بوصلة UDP 47001 وارد.
echo.
cd /d "%PC_DIR%"
%PYTHON% receive_input.py --debug
pause
goto menu

:run_fake
echo.
echo ============================================================
echo  تشغيل fake_ps4_input.py (محاكي PS4)
echo ============================================================
echo هذا يرسل حزم إدخال مزيفة إلى 127.0.0.1:47001
echo afinك تختبر المستقبل بدون حاجة للـ PS4 الحقيقي.
echo.
cd /d "%PC_DIR%"
%PYTHON% tools\fake_ps4_input.py --btn cross --rate 10 --debug
pause
goto menu

:run_both
echo.
echo ============================================================
echo  تشغيل المستقبل والمحاكي معًا
echo ============================================================
echo سيفتح نافذتان: واحدة للمستقبل وأخرى للمحاكي.
echo.
cd /d "%PC_DIR%"
start "Receiver" cmd /c "%PYTHON% receive_input.py --debug & pause"
timeout /t 2 >nul
start "FakePS4" cmd /c "%PYTHON% tools\fake_ps4_input.py --btn cross --rate 10 & pause"
echo.
echo [INFO] تم تشغيل المستقبل والمحاكي. راقب نافذة المستقبل.
echo         يجب أن تظهر rate=10 pkt/s وتแสดง الأزرار المضغوطة.
pause
goto menu

:run_sender
echo.
echo ============================================================
echo  تشغيل send_mjpeg.py (مرسل الفيديو إلى PS4)
echo ============================================================
echo تأكد من ضبط ps4_host في pc\config.ini إلى عنوان IP البلايستيشن.
echo.
cd /d "%PC_DIR%"
%PYTHON% send_mjpeg.py --test-pattern --no-send
pause
goto menu

:show_ip
echo.
echo ============================================================
echo  عنوان IPurrent الخاص بحاسبك
echo ============================================================
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr "IPv4"') do echo   %%a
echo.
echo [INFO] ضع هذا العنوان في ps4/include/config.h كقيمة لـ CFG_PC_IP.
echo         ثم أعد بناء PKG وتثبيته على البلايستيشن.
pause
goto menu

:end
echo.
echo تم الإنتهاء.
endlocal