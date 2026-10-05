' anime-sort: main window (engine\main.py + gui\main_window.py).
' It checks configs\config.json, starts the batches listed in "run" and shows their log and statistics.
' Every batch opens one window per dataset. Closing the main window stops everything.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root
' pythonw: no console - the main window (gui\main_window.py) shows the log, statistics and window buttons.
shell.Run """" & root & "\venv\Scripts\pythonw.exe"" """ & root & "\engine\main.py""", 1, False
