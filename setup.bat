@echo off
chcp 65001 >nul
title CNC Transfer - Kurulum
color 0B
echo.
echo  ╔══════════════════════════════════════════╗
echo  ║     CNC Transfer System - Kurulum        ║
echo  ╚══════════════════════════════════════════╝
echo.

:: ─── 1. Python kontrolu ───
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [1/4] Python bulunamadi. Indiriliyor...
    echo.
    curl -L -o python_installer.exe https://www.python.org/ftp/python/3.13.2/python-3.13.2-amd64.exe
    if %errorlevel% neq 0 (
        echo  [HATA] Python indirilemedi!
        echo  Manuel kurun: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo  Python kuruluyor (bu biraz surebilir)...
    python_installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
    del python_installer.exe >nul 2>&1
    echo.
    echo  [!] Python kuruldu. Bu pencereyi kapatip setup.bat'i tekrar calistirin.
    pause
    exit /b 0
)
echo  [1/4] Python bulundu:
python --version
echo.

:: ─── 2. Paketler ───
echo  [2/4] Paketler kuruluyor...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install customtkinter >nul 2>&1
if %errorlevel% neq 0 (
    echo  [HATA] customtkinter yuklenemedi!
    pause
    exit /b 1
)
echo        - customtkinter OK
python -m pip install chattertools >nul 2>&1
if %errorlevel% neq 0 (
    echo  [UYARI] chattertools yuklenemedi (FOCAS DLL olmadan devam edilecek)
) else (
    echo        - chattertools OK (FOCAS DLL)
)
echo.

:: ─── 3. Uygulama dosyalari ───
echo  [3/4] Uygulama dosyalari kontrol ediliyor...
set "TARGET_DIR=C:\Users\Public\Documents\Shared Mastercam 2025"
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

copy /Y "%~dp0dosya_aktarim.py" "%TARGET_DIR%\dosya_aktarim.py" >nul 2>&1
echo        - dosya_aktarim.py kopyalandi

if exist "%~dp0machines.json" (
    if not exist "%TARGET_DIR%\machines.json" (
        copy /Y "%~dp0machines.json" "%TARGET_DIR%\machines.json" >nul 2>&1
        echo        - machines.json kopyalandi
    ) else (
        echo        - machines.json zaten mevcut (atlanildi)
    )
)
echo.

:: ─── 4. Masaustu kisayolu ───
echo  [4/4] Masaustu kisayolu olusturuluyor...
set "SHORTCUT_VBS=%TEMP%\create_shortcut.vbs"
(
echo Set oWS = WScript.CreateObject("WScript.Shell"^)
echo sLinkFile = oWS.ExpandEnvironmentStrings("%%USERPROFILE%%"^) ^& "\Desktop\CNC Transfer.lnk"
echo Set oLink = oWS.CreateShortcut(sLinkFile^)
echo oLink.TargetPath = "pythonw.exe"
echo oLink.Arguments = """%TARGET_DIR%\dosya_aktarim.py"""
echo oLink.WorkingDirectory = "%TARGET_DIR%"
echo oLink.Description = "CNC Transfer Uygulamasi"
echo oLink.WindowStyle = 1
echo oLink.Save
) > "%SHORTCUT_VBS%"
cscript //nologo "%SHORTCUT_VBS%" >nul 2>&1
del "%SHORTCUT_VBS%" >nul 2>&1
echo        - CNC Transfer kisayolu masaustune eklendi
echo.

:: ─── FOCAS DLL testi ───
python -c "import ctypes,sys,os;d=os.path.join(os.path.dirname(sys.executable),'Lib','site-packages','chattertools','lib','Fwlib64');p=os.path.join(d,'fwlibe64.dll');os.environ['PATH']=d+';'+os.environ.get('PATH','');os.add_dll_directory(d);ctypes.windll.LoadLibrary(p);print('  [OK] FOCAS DLL yuklendi')" 2>nul || echo  [UYARI] FOCAS DLL yuklenemedi (Torna CNC bellek transferi calismayabilir)
echo.

echo  ╔══════════════════════════════════════════╗
echo  ║         Kurulum tamamlandi!               ║
echo  ║                                            ║
echo  ║  Masaustundeki "CNC Transfer" simgesine    ║
echo  ║  cift tiklayarak uygulamayi baslatin.      ║
echo  ║                                            ║
echo  ║  Veya: python dosya_aktarim.py                   ║
echo  ╚══════════════════════════════════════════╝
echo.
pause
