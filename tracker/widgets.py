"""Fixed-size tabs: selection changes the background only."""
import tkinter as tk


class FlatTabs(tk.Frame):
    def __init__(self, master, **kwargs):
        super().__init__(master, bg='#f3f6f8', **kwargs)
        self.bar = tk.Frame(self, bg='#e3ecee', bd=0)
        self.bar.pack(fill='x')
        self.body = tk.Frame(self, bg='#f3f6f8', bd=0)
        self.body.pack(fill='both', expand=True)
        self.frames, self.buttons = [], []
        self.current = None

    def add(self, frame, text=''):
        index = len(self.frames)
        button = tk.Button(self.bar, text=text, command=lambda: self.select(index),
                           font=('Microsoft YaHei UI', 10), bd=0, relief='flat',
                           highlightthickness=0, padx=18, pady=12, takefocus=True,
                           fg='#173544', bg='#e3ecee', activeforeground='#173544',
                           activebackground='#d1ece7', cursor='hand2')
        button.pack(side='left')
        self.frames.append(frame)
        self.buttons.append(button)
        if self.current is None:
            self.select(index)

    def select(self, tab=None):
        if tab is None:
            return str(self.frames[self.current]) if self.current is not None else ''
        index = tab if isinstance(tab, int) else next(i for i, frame in enumerate(self.frames) if str(frame) == str(tab))
        if self.current is not None:
            self.frames[self.current].pack_forget()
        self.current = index
        self.frames[index].pack(in_=self.body, fill='both', expand=True)
        for i, button in enumerate(self.buttons):
            button.configure(bg='#cbe7e2' if i == index else '#e3ecee')

    def tabs(self):
        return tuple(str(frame) for frame in self.frames)
