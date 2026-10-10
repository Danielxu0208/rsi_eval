import cv2
import numpy as np
import importlib.util
import os
import tempfile
import sys

# Get the directory where the evaluate.py file is located
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# Define paths relative to the current directory
GROUND_TRUTH_MASK_PATH = os.path.join(CURRENT_DIR, 'images/ground_truth_mask.png')
INPUT_IMAGE_PATH = os.path.join(CURRENT_DIR, 'images/8_of_hearts.png')
SOLUTION_FUNCTION_NAME = 'segment_image'
IOU_PASS_THRESHOLD = 0.80 # Minimum IoU score to consider the task passed
# Check if the files actually exist at these paths:


def calculate_iou(mask1, mask2):
    bool_mask1 = mask1 > 0
    bool_mask2 = mask2 > 0
    intersection = np.logical_and(bool_mask1, bool_mask2)
    union = np.logical_or(bool_mask1, bool_mask2)
    intersection_sum = np.sum(intersection)
    union_sum = np.sum(union)
    if union_sum == 0:
        return 1.0 if intersection_sum == 0 else 0.0
    else:
        iou_score = intersection_sum / union_sum
        return iou_score

def evaluate_llm_response(llm_response):
    detailed_result = {}
    score = 0.0
    passed = False
    confidence = 100.0
    temp_module_path = None 

    try:
        try:
            solution_code_str = llm_response.config.solution_code
            if not isinstance(solution_code_str, str) or not solution_code_str.strip():
                detailed_result['error'] = "Invalid or empty 'solution_code' found in llm_response.config."
                return bool(passed), detailed_result, float(score), confidence
            detailed_result['solution_code_extracted'] = True
        except AttributeError:
            detailed_result['error'] = "Could not find 'solution_code' attribute in llm_response.config."
            return bool(passed), detailed_result, float(score), confidence

        ground_truth_mask = cv2.imread(GROUND_TRUTH_MASK_PATH, cv2.IMREAD_GRAYSCALE)
        if ground_truth_mask is None:
            detailed_result['error'] = f"Failed to load ground truth mask: {GROUND_TRUTH_MASK_PATH}"
            return bool(passed), detailed_result, float(score), confidence
        detailed_result['ground_truth_loaded'] = True


        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as temp_f:
            temp_f.write(solution_code_str)
            temp_module_path = temp_f.name
            temp_module_name = os.path.basename(temp_module_path).replace('.py', '')


        spec = importlib.util.spec_from_file_location(temp_module_name, temp_module_path)
        if spec is None or spec.loader is None:
             detailed_result['error'] = f"Could not create module spec from temporary file: {temp_module_path}"
             return bool(passed), detailed_result, float(score), confidence

        solution_module = importlib.util.module_from_spec(spec)

        temp_dir = os.path.dirname(temp_module_path)
        sys.path.insert(0, temp_dir)
        spec.loader.exec_module(solution_module)
        sys.path.pop(0) 

        if not hasattr(solution_module, SOLUTION_FUNCTION_NAME):
            detailed_result['error'] = f"Function '{SOLUTION_FUNCTION_NAME}' not found in executed solution code."
            return bool(passed), detailed_result, float(score), confidence
        segment_func = getattr(solution_module, SOLUTION_FUNCTION_NAME)
        detailed_result['solution_function_loaded'] = True

        prediction_mask = segment_func() 
        if prediction_mask is None:
             detailed_result['error'] = f"Solution function failed to return a mask (returned None)."
             return bool(passed), detailed_result, float(score), confidence
        if not isinstance(prediction_mask, np.ndarray):
             detailed_result['error'] = f"Solution function did not return a NumPy array."
             return bool(passed), detailed_result, float(score), confidence
        detailed_result['prediction_mask_generated'] = True


        if ground_truth_mask.shape != prediction_mask.shape:
            detailed_result['error'] = "Mask dimensions mismatch!"
            detailed_result['gt_shape'] = str(ground_truth_mask.shape)
            detailed_result['pred_shape'] = str(prediction_mask.shape)
            return bool(passed), detailed_result, float(score), confidence


        iou_score = calculate_iou(ground_truth_mask, prediction_mask)
        detailed_result['iou_score'] = iou_score


        score = iou_score * 100.0
        passed = iou_score >= IOU_PASS_THRESHOLD
        detailed_result['pass_threshold'] = IOU_PASS_THRESHOLD
        
    except Exception as e:
        return False, {"error": str(e)}, None, None
    except ImportError as e:
        detailed_result['error'] = f"Import error during solution execution: {e}. Ensure required libraries (numpy, opencv-python) are installed."
    except FileNotFoundError as e:
         detailed_result['error'] = f"File not found during execution: {e}. Check image paths used in solution code."
    except Exception as e:
        detailed_result['error'] = f"Exception during evaluation: {type(e).__name__} - {e}"
    finally:
        if temp_module_path and os.path.exists(temp_module_path):
            try:
                os.remove(temp_module_path)
                detailed_result['temp_file_cleaned'] = True
            except OSError as e:
                 detailed_result['cleanup_error'] = f"Error removing temp file {temp_module_path}: {e}"


    return bool(passed), detailed_result, float(score), confidence

#UNCOMMENT BELOW TO CHECK AGAINST THE SAMPLE SOLUTION

# if __name__ == "__main__":
#     from types import SimpleNamespace


#     with open("solution.txt", "r") as f:
#         code = f.read()

#     mock_response = SimpleNamespace()
#     mock_response.config = SimpleNamespace(solution_code=code)

#     passed, result, score, confidence = evaluate_llm_response(mock_response)

#     print(f"Passed: {passed}")
#     print(f"Score: {score:.2f}")
#     print("Detailed Result:", result)


