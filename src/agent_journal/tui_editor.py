import curses
from collections import deque

from .vi import Paste


class Keys:
    def __init__(self, window):
        self.window = window
        self.pending = deque()
        self.paste = None

    def read(self):
        if self.pending:
            return self.pending.popleft()
        try:
            key = self.window.get_wch()
        except curses.error:
            return None
        if key == "\x1b":
            ending = []
            self.window.timeout(12)
            try:
                for expected in "[201~" if self.paste is not None else "[200~":
                    observed = self.window.get_wch()
                    ending.append(observed)
                    if observed != expected:
                        break
                else:
                    if self.paste is None:
                        self.paste = []
                        return None
                    text = "".join(self.paste)
                    self.paste = None
                    return Paste(text)
            except curses.error:
                pass
            finally:
                self.window.timeout(40)
            if self.paste is None:
                self.pending.extend(ending)
            else:
                self.paste.extend(
                    value for value in [key, *ending] if isinstance(value, str)
                )
                return None
        elif self.paste is not None:
            if isinstance(key, str) and len(self.paste) < 80_000:
                self.paste.append(key)
            return None
        return key
