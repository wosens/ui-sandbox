# -*- coding: utf-8 -*-
"""Demo target app for ui-sandbox: a plain tkinter window with a button,
an entry and a label. Used by selftest and as a playground for `sb.py`.
Window title: "SB Demo". Button client area is at approximately (60, 25)."""
import tkinter as tk


class DemoApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SB Demo")
        self.geometry("420x260+40+40")
        self.clicks = 0
        tk.Button(self, text="点我 Click Me", command=self.on_click,
                  width=14).place(x=10, y=10)
        self.entry = tk.Entry(self, width=30)
        self.entry.place(x=10, y=60)
        self.label = tk.Label(self, text="clicks=0", anchor="w", width=40)
        self.label.place(x=10, y=100)
        self.listbox = tk.Listbox(self, height=4)
        self.listbox.place(x=10, y=140)
        self.after(200, lambda: None)

    def on_click(self):
        self.clicks += 1
        txt = f"clicks={self.clicks} entry={self.entry.get()!r}"
        self.label.config(text=txt)
        self.listbox.insert(tk.END, txt)


if __name__ == "__main__":
    DemoApp().mainloop()
