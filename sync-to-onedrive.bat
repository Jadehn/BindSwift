@echo off
rem Mirrors this Bindswift repo into the shared OneDrive folder (one way: this PC -> OneDrive).
rem Copies everything except the Python venv and caches (each computer builds its own venv).
set "SRC=C:\Users\jaden\Documents\New folder\Python\Stock Machine\BindSwift"
set "DST=C:\Users\jaden\Desktop\Howard Analytics - Howard Indemnity Group\BindSwift\BindSwift Platform"
robocopy "%SRC%" "%DST%" /MIR /XD .venv __pycache__ node_modules /XF index.new.html /R:1 /W:1 /NFL /NDL /NP
echo.
echo Synced to OneDrive: %DST%
pause
