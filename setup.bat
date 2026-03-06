@echo off
chcp 65001 >nul
title CNC Transfer - Setup
color 0B
echo.
echo  ЩЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЛ
echo  К        CNC Transfer System - Setup        К
echo  ШЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭМ
echo.

:: --- 1. Check Python ---
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [1/4] Python not found. Downloading...
    echo.
    curl -L -o python_installer.exe https://www.python.org/ftp/python/3.13.2/python-3.13.2-amd64.exe
    if %errorlevel% neq 0 (
        echo  [ERROR] Python download failed!
        echo  Install manually: https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo  Installing Python (this may take a while)...
    python_installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
    del python_installer.exe >nul 2>&1
    echo.
    echo  [!] Python installed. Close this window and run setup.bat again.
    pause
    exit /b 0
)
echo  [1/4] Python found:
python --version
echo.

:: --- 2. Packages ---
echo  [2/4] Installing packages...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install customtkinter >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] customtkinter could not be installed!
    pause
    exit /b 1
)
echo        - customtkinter OK
python -m pip install chattertools >nul 2>&1
if %errorlevel% neq 0 (
    echo  [WARNING] chattertools could not be installed (continuing without FOCAS DLL)
) else (
    echo        - chattertools OK (FOCAS DLL)
)
echo.

:: --- 3. App files ---
echo  [3/4] Checking app files...
set "TARGET_DIR=C:\Users\Public\Documents\Shared Mastercam 2025"
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

copy /Y "%~dp0dosya_aktarim.py" "%TARGET_DIR%\dosya_aktarim.py" >nul 2>&1
echo        - dosya_aktarim.py copied

if exist "%~dp0machines.json" (
    if not exist "%TARGET_DIR%\machines.json" (
        copy /Y "%~dp0machines.json" "%TARGET_DIR%\machines.json" >nul 2>&1
        echo        - machines.json copied
    ) else (
        echo        - machines.json already exists (skipped)
    )
)
echo.

:: --- 4. Desktop shortcut ---
echo  [4/4] Creating desktop shortcut...
set "SHORTCUT_VBS=%TEMP%\create_shortcut.vbs"
(
echo Set oWS = WScript.CreateObject("WScript.Shell"^)
echo sLinkFile = oWS.ExpandEnvironmentStrings("%%USERPROFILE%%"^) ^& "\Desktop\CNC Transfer.lnk"
echo Set oLink = oWS.CreateShortcut(sLinkFile^)
echo oLink.TargetPath = "pythonw.exe"
echo oLink.Arguments = """%TARGET_DIR%\dosya_aktarim.py"""
echo oLink.WorkingDirectory = "%TARGET_DIR%"
echo oLink.Description = "CNC Transfer App"
echo oLink.WindowStyle = 1
echo oLink.Save
) > "%SHORTCUT_VBS%"
cscript //nologo "%SHORTCUT_VBS%" >nul 2>&1
del "%SHORTCUT_VBS%" >nul 2>&1
echo        - CNC Transfer shortcut added to desktop
echo.

:: --- FOCAS DLL check ---
python -c "import ctypes,sys,os;d=os.path.join(os.path.dirname(sys.executable),'Lib','site-packages','chattertools','lib','Fwlib64');p=os.path.join(d,'fwlibe64.dll');os.environ['PATH']=d+';'+os.environ.get('PATH','');os.add_dll_directory(d);ctypes.windll.LoadLibrary(p);print('  [OK] FOCAS DLL loaded')" 2>nul || echo  [WARNING] FOCAS DLL could not be loaded (lathe CNC memory transfer may not work)
echo.

echo  ЩЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЛ
echo  К            Setup complete!                К
echo  К                                            К
echo  К  Double-click the "CNC Transfer" icon on   К
echo  К  your desktop to start the app.            К
echo  К                                            К
echo  К  Or: python dosya_aktarim.py               К
echo  ШЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭЭМ
echo.
pause
