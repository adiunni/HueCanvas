import numpy as np
def map_gamut(xy, triangle):
    if not triangle:
        return xy
    p = np.array(xy); a, b, c = np.array(triangle)
    weights = np.linalg.solve(np.column_stack((b-a, c-a)), p-a)
    if min(weights) >= 0 and sum(weights) <= 1:
        return xy
    candidates = []
    for start, end in ((a,b), (b,c), (c,a)):
        edge = end-start
        t = np.clip(np.dot(p-start, edge)/np.dot(edge, edge), 0, 1)
        candidates.append(start+t*edge)
    return min(candidates, key=lambda q: np.linalg.norm(p-q)).tolist()
