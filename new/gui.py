import subprocess, sys, os, webbrowser, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"

def start_server():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env.setdefault("CWS_HOME", str(Path.home() / ".claudewebstream"))
    subprocess.Popen([sys.executable, "-m", "claudewebstream", "start"], cwd=str(ROOT), env=env)
    time.sleep(4)
    webbrowser.open("http://127.0.0.1:8787/")

if __name__ == "__main__":
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.title("ClaudeWebStream")
        root.geometry("320x160")
        ttk.Label(root, text="ClaudeWebStream GUI").pack(pady=10)
        ttk.Button(root, text="Start Server & Open Dashboard", command=start_server).pack(pady=10)
        root.mainloop()
    except Exception as e:
        print("GUI unavailable:", e)
        start_server()
