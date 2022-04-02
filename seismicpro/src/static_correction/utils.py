"""Static correction utils. """

import numpy as np


def calculate_depth_coef(v1, v2, avg_v2):
    if np.any(v2 - v1 <= 0):
        raise ValueError("v1 > v2")
    return (avg_v2 * v2 - v1**2) / (v1*avg_v2*(v2**2 - v1**2)**.5)