' Runs a .bat file without showing a console window (used by the scheduled task).
If WScript.Arguments.Count > 0 Then
  CreateObject("WScript.Shell").Run """" & WScript.Arguments(0) & """", 0, False
End If
