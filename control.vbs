' anime-sort: control panel (gui\control\app.py) - one window for everything the project does:
' datasets overview, pipelines (start / stop / prepare / finish), the second pass for "Other",
' config editors, names.json, API keys and probes, collection tools.
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = root
' pythonw: no console - tool output is shown inside the control panel ("Tasks").
shell.Run """" & root & "\venv\Scripts\pythonw.exe"" """ & root & "\gui\control\app.py""", 1, False
