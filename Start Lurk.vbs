Option Explicit
Dim shell, files, folder, python, app
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
folder = files.GetParentFolderName(WScript.ScriptFullName)
python = files.BuildPath(folder, ".venv\Scripts\pythonw.exe")
app = files.BuildPath(folder, "taskbar.pyw")
If Not files.FileExists(python) Then
    MsgBox "Python environment missing: " & python, vbExclamation, "Lurk"
    WScript.Quit 1
End If
shell.Run Chr(34) & python & Chr(34) & " " & Chr(34) & app & Chr(34), 0, False
