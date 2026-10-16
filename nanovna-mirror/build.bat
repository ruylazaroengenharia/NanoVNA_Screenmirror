@echo off
REM ==========================================================================
REM  Gera o executavel "NanoVNA Mirror.exe" e o pacote .zip para distribuicao
REM  Uso: de dois cliques neste arquivo (precisa do Python instalado)
REM ==========================================================================
cd /d "%~dp0"

echo [1/3] Instalando dependencias...
python -m pip install --upgrade -r requirements.txt pyinstaller || goto :erro

echo [2/3] Gerando o executavel...
python -m PyInstaller --noconfirm --onefile --windowed --name "NanoVNA Mirror" nanovna_mirror.py || goto :erro

echo [3/3] Montando o pacote .zip...
copy /Y "docs\LEIA-ME.txt" "dist\LEIA-ME.txt" >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\NanoVNA Mirror.exe','dist\LEIA-ME.txt' -DestinationPath 'dist\NanoVNA_Mirror_Windows.zip'" || goto :erro

echo.
echo Pronto! Arquivos em: %~dp0dist
echo   - NanoVNA Mirror.exe
echo   - NanoVNA_Mirror_Windows.zip  (para distribuir)
pause
exit /b 0

:erro
echo.
echo *** Ocorreu um erro. Veja as mensagens acima. ***
pause
exit /b 1
