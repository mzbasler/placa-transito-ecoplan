@echo off
cd /d "%~dp0"
echo.
echo  Iniciando o painel de teste de placas...
echo  O navegador vai abrir sozinho. DEIXE ESTA JANELA ABERTA.
echo  (feche esta janela quando quiser parar o painel)
echo.
python app.py
pause
