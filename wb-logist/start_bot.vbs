' Запуск воркера без окна консоли (для автозагрузки)
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "C:\Users\User\.cline\data\workspaces\chat\wb-logist"
shell.Run """C:\Users\User\.cline\data\workspaces\chat\wb-logist\.venv\Scripts\pythonw.exe"" ""C:\Users\User\.cline\data\workspaces\chat\wb-logist\bot_worker.py""", 0, False
