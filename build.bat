@echo off
REM ==========================================================================
REM  Gera "NanoVNA Mirror.exe" e "SV4401A Mirror.exe" e os pacotes .zip
REM  Uso: de dois cliques neste arquivo (precisa do Python instalado)
REM ==========================================================================
cd /d "%~dp0"

echo [1/4] Instalando dependencias...
python -m pip install --upgrade -r requirements.txt pyinstaller || goto :erro

echo [2/4] Gerando NanoVNA Mirror.exe...
python -m PyInstaller --noconfirm --onefile --windowed --icon icon.ico --name "NanoVNA Mirror" nanovna_mirror.py || goto :erro

echo [3/4] Gerando SV4401A Mirror.exe...
python -m PyInstaller --noconfirm --onefile --windowed --icon icon.ico --name "SV4401A Mirror" sv4401a_mirror.py || goto :erro

echo [4/4] Montando os pacotes .zip...
copy /Y "docs\LEIA-ME.txt" "dist\LEIA-ME.txt" >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\NanoVNA Mirror.exe','dist\LEIA-ME.txt' -DestinationPath 'dist\NanoVNA_Mirror_Windows.zip'" || goto :erro
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\SV4401A Mirror.exe','dist\LEIA-ME.txt' -DestinationPath 'dist\SV4401A_Mirror_Windows.zip'" || goto :erro

echo.
echo Pronto! Arquivos em: %~dp0dist
echo   - NanoVNA Mirror.exe   / NanoVNA_Mirror_Windows.zip
echo   - SV4401A Mirror.exe   / SV4401A_Mirror_Windows.zip
pause
exit /b 0

:erro
echo.
echo *** Ocorreu um erro. Veja as mensagens acima. ***
pause
exit /b 1
