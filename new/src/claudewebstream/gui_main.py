import sys, os, time, webbrowser, subprocess
from pathlib import Path

def main():
    ROOT = Path(__file__).resolve().parents[2]
    SRC = ROOT / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env.setdefault("CWS_HOME", str(Path.home() / ".claudewebstream"))
    try:
        import tkinter as tk
        from tkinter import ttk
        root = tk.Tk()
        root.title("ClaudeWebStream")
        root.geometry("320x160")
        ttk.Label(root, text="ClaudeWebStream").pack(pady=10)
        def start():
            subprocess.Popen([sys.executable, "-m", "claudewebstream", "start"], cwd=str(ROOT), env=env)
            time.sleep(4)
            webbrowser.open("http://127.0.0.1:8787/")
        ttk.Button(root, text="Start Server & Open Dashboard", command=start).pack(pady=10)
        root.mainloop()
    except Exception as e:
        print("GUI unavailable:", e)
        subprocess.Popen([sys.executable, "-m", "claudewebstream", "start"], cwd=str(ROOT), env=env)

if __name__ == "__main__":
    main()
