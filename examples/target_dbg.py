# -*- coding: utf-8 -*-
"""Debug target: logs every Button/Key event Tk sees to target_dbg.log."""
import os
import sys
import tkinter as tk

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "target_dbg.log")


def log(*a):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(" ".join(str(x) for x in a) + "\n")
        f.flush()


class Dbg(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SB Dbg")
        self.geometry("420x260+40+40")
        open(LOG, "w").close()
        log("app start")
        tk.Button(self, text="点我 Click Me", command=lambda: log("COMMAND FIRED"),
                  width=14).place(x=10, y=10)
        self.entry = tk.Entry(self, width=30)
        self.entry.place(x=10, y=60)
        self.entry.bind("<Key>", lambda e: log("KEY", repr(e.char), e.keysym,
                                               "widget=entry"))
        self.bind("<Button-1>", lambda e: log("BTN1", e.x, e.y,
                                              "w=", str(e.widget)))
        self.bind("<ButtonRelease-1>", lambda e: log("BTN1-UP", e.x, e.y))
        self.bind("<FocusIn>", lambda e: log("FOCUS-IN", str(e.widget)))
        self.label = tk.Label(self, text="clicks=0")
        self.label.place(x=10, y=100)
        self.clicks = 0

    def on_click(self):
        self.clicks += 1
        self.label.config(text=f"clicks={self.clicks}")


if __name__ == "__main__":
    Dbg().mainloop()
