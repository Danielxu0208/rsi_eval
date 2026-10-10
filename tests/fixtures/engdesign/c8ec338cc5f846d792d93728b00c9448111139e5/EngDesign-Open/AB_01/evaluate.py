import numpy as np
from scipy.stats import skew, kurtosis
from matplotlib.path import Path
from collections import defaultdict
from pprint import pprint

def calculate_glcm_contrast(image, coords, offset=(0, 1)):
    glcm = defaultdict(int)
    count = 0
    dr, dc = offset
    inside = set(coords)
    for r, c in coords:
        nr, nc = r + dr, c + dc
        if (nr, nc) in inside:
            glcm[(image[r, c], image[nr, nc])] += 1
            count += 1
    if count == 0:
        return 0.0
    return sum((i - j)**2 * (freq / count) for (i, j), freq in glcm.items())

def reference_radiomics_features(image, verts_rc, offset=(0,1)):
    rows, cols = image.shape
    ys, xs = np.arange(rows) + 0.5, np.arange(cols) + 0.5
    xx, yy = np.meshgrid(xs, ys)
    pts = np.column_stack((xx.ravel(), yy.ravel()))
    mask = Path(verts_rc[:, ::-1]).contains_points(pts).reshape(rows, cols)

    coords = list(zip(*np.nonzero(mask)))
    vals = np.array([image[r, c] for r, c in coords], float)

    return {
        'mean':     vals.mean(),
        'variance': vals.var(ddof=1),
        'skewness': skew(vals, bias=False),
        'kurtosis': kurtosis(vals, fisher=True, bias=False),
        'contrast': calculate_glcm_contrast(image, coords, offset)
    }

def evaluate_llm_response(llm_response):
    try:
        image = np.array([
            [15,18,20,23,26,29,32,35,38,41],
            [17,20,23,26,29,32,35,38,41,44],
            [19,22,25,28,31,34,37,40,43,46],
            [21,24,27,30,33,36,39,42,45,48],
            [23,26,29,32,35,38,41,44,47,50],
            [25,28,31,34,37,40,43,46,49,52],
            [27,30,33,36,39,42,45,48,51,54],
            [29,32,35,38,41,44,47,50,53,56],
            [31,34,37,40,43,46,49,52,55,58],
            [33,36,39,42,45,48,51,54,57,60]
        ])
        verts = np.array([(2,2),(2,7),(7,7),(7,2)])
        ref = reference_radiomics_features(image, verts)

        mean = llm_response.config.mean
        variance = llm_response.config.variance
        skewness = llm_response.config.skewness
        kurtosis = llm_response.config.kurtosis
        contrast = llm_response.config.contrast

        llm_vals = {
            'mean': mean,
            'variance': variance,
            'skewness': skewness,
            'kurtosis': kurtosis,
            'contrast': contrast
        }



        tol = 1e-1
        #correct = {k: abs(llm_vals.get(k, np.nan) - ref[k]) < tol for k in ref}
        #score = sum(correct.values()) * 20
        score = 0
        details = ""
        if abs(llm_vals['mean'] - ref['mean']) < tol:
            score += 20
            details += "Mean intensity value of the ROI is correct.\n"
        else:
            details += "Mean intensity value of the ROI is wrong.\n"

        if abs(llm_vals['variance'] - ref['variance']) < tol:
            score += 20
            details += "Sample variance of intensity values in the ROI is correct.\n"
        else:
            details += "Sample variance of intensity values in the ROI is wrong.\n"

        if abs(llm_vals['skewness'] - ref['skewness']) < tol:
            score += 20
            details += "Skewness of intensity distribution in the ROI is correct.\n"
        else:
            details += "Skewness of intensity distribution in the ROI is wrong.\n"

        if abs(llm_vals['kurtosis'] - ref['kurtosis']) < tol:
            score += 20
            details += "Excess kurtosis of intensity distribution in the ROI is correct.\n"
        else:
            details += "Excess kurtosis of intensity distribution in the ROI is wrong.\n"

        if abs(llm_vals['contrast'] - ref['contrast']) < tol:
            score += 20
            details += "GLCM contrast feature computed from ROI pixels is correct.\n"
        else:
            details += "GLCM contrast feature computed from ROI pixels is wrong.\n"


        passed = score == 100

        return bool(passed), details, score, 100
    except Exception as e:
        return False, str(e), None, None


# if __name__ == '__main__':
#     #input LLM numerical responses below
#     llm_vals = {'mean': 35.0000, 'variance': 27.0833, 'skewness': 0.0000, 'kurtosis': -0.6356, 'contrast': 9.0000}
#     pprint(evaluate_radiomics_solution(llm_vals))

