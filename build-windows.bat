@echo off
chcp 65001 >nul
cd /d %~dp0
rem ============================================================
rem  K-Rail 매크로 (KTX + SRT 통합) 윈도우 단일 exe 빌드
rem  - 윈도우에서 이 파일을 더블클릭하면 dist\k-rail-macro.exe 생성
rem  - 사전 요구사항: Python 3.10+ (amd64/x64) — python.org에서
rem    "Windows installer (64-bit)" 설치 시 "Add python.exe to PATH" 체크
rem  - GitHub Actions(build-windows.yml)와 동일한 빌드 플래그를 사용
rem ============================================================

where python >nul 2>nul
if errorlevel 1 goto nopython

rem ARM64 Python으로 빌드하면 일반 x64 PC(친구들)에서 실행 불가한 exe가 나온다.
rem Parallels(Apple Silicon)는 기본이 ARM 윈도우라 x64 Python을 따로 설치해야 한다.
for /f %%a in ('python -c "import platform;print(platform.machine())"') do set PYARCH=%%a
if /i "%PYARCH%"=="ARM64" goto armpython

echo === 1/4 빌드용 가상환경 생성 ===
if not exist build_venv python -m venv build_venv
if errorlevel 1 goto fail
call build_venv\Scripts\activate.bat

echo === 2/4 의존성 설치 (srtgo는 git에서 받음, 수 분 소요) ===
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller pywin32-ctypes pillow
if errorlevel 1 goto fail

echo === 3/4 아이콘 변환 (실패해도 계속) ===
set ICONARG=
python -c "from PIL import Image; Image.open('icon.png').save('icon.ico', sizes=[(s,s) for s in (16,32,48,64,128,256)])" 2>nul
if exist icon.ico set ICONARG=--icon icon.ico

echo === 4/4 단일 exe 빌드 ===
rem srtgo는 collect-all 금지(CLI가 telegram/inquirer를 끌어와 빌드를 깬다).
rem srtgo.ktx만 hidden-import로 명시. Crypto(pycryptodome)는 바이너리라 collect-all.
pyinstaller --onefile --noconfirm --clean --name k-rail-macro %ICONARG% ^
  --add-data "static;static" ^
  --collect-all uvicorn ^
  --collect-all keyring ^
  --collect-all Crypto ^
  --collect-submodules anyio ^
  --hidden-import win32ctypes.core ^
  --hidden-import keyring.backends.Windows ^
  --hidden-import srtgo.ktx ^
  launch.py
if errorlevel 1 goto fail

echo.
echo [완료] dist\k-rail-macro.exe 생성됨
echo        더블클릭하면 검은 창이 뜨고 브라우저에서 통합 화면이 열립니다.
pause
exit /b 0

:nopython
echo [오류] Python이 없습니다. https://www.python.org/downloads/ 에서 설치하세요.
echo        설치 시 "Add python.exe to PATH" 체크 필수 (64-bit installer).
pause
exit /b 1

:armpython
echo [오류] ARM64 Python입니다. 이걸로 빌드한 exe는 일반 x64 윈도우 PC에서 실행되지 않습니다.
echo        python.org에서 "Windows installer (64-bit)" - amd64 - 를 설치한 뒤 다시 실행하세요.
echo        (Parallels는 ARM 윈도우이므로 x64 Python을 별도 설치해야 친구들 PC에서 돌아갑니다.)
pause
exit /b 1

:fail
echo.
echo [오류] 빌드 실패 - 위 메시지를 확인하세요.
pause
exit /b 1
