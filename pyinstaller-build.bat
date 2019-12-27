IF NOT EXIST venv (python -m venv venv)
CALL venv\Scripts\activate
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python ./setup.py develop
venv\Scripts\python setup.py pyinstaller -- --noconsole --onefile --add-data "gfx/*;gfx" --icon gfx/icon.ico
copy dist\lanscatter_gui.exe .\
del /S dist /Q
venv\Scripts\python setup.py pyinstaller -- --onefile
copy dist\lanscatter_master.exe .\
copy dist\lanscatter_peer.exe .\
