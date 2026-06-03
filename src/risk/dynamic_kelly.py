import numpy as np
from collections import deque

class AdaptiveKelly:
    def __init__(self, base=0.25, window=20, low=0.1, high=0.4):
        self.base = base; self.window = deque(maxlen=window)
        self.low = low; self.high = high
    def update(self, prob, outcome):
        self.window.append(np.log(prob) if outcome == 1 else np.log(1 - prob))
    def fraction(self):
        if len(self.window) < 5: return self.base
        avg = np.mean(self.window)
        adj = np.clip(2 * (avg + 0.5), 0.5, 1.5)
        return np.clip(self.base * adj, self.low, self.high)
