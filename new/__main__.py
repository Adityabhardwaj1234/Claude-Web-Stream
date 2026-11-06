import sys
if len(sys.argv) == 1:
    from gui import *
    # launch gui
    try:
        import tkinter as tk
        from tkinter import ttk
        import threading, time, webbrowser, os, subprocess
        ROOT = __import__("pathlib").Path(__file__).resolve().parent
        def start():
            env = dict(os.environ)
            env["PYTHONPATH"] = str(ROOT / "src")
            subprocess.Popen([sys.executable, "-m", "claudewebstream", "start"], cwd=str(ROOT), env=env)
            time.sleep(4)
            webbrowser.open("http://127.0.0.1:8787/")
        root = tk.Tk()
        root.title("ClaudeWebStream")
        root.geometry("300x140")
        ttk.Label(root, text="ClaudeWebStream").pack(pady=10)
        ttk.Button(root, text="Start Server", command=start).pack(pady=10)
        root.mainloop()
    except Exception as e:
        print("GUI unavailable:", e)
else:
    from claudewebstream.cli.main import app
    app()
