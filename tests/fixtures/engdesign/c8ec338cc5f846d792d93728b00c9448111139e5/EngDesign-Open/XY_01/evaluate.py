import os
from typing import Any, Tuple
from font_rom_data import get_rom_pattern

def evaluate_llm_response(llm_response) -> Tuple[bool, dict, int, int]:
    try:
        # 1. Extract config and patterns
        config = llm_response.config
        tetromino_type = config.tetromino_type
        rotation = config.rotation

        # 2. Load ground truth ROM data
        

        ground_truth = get_rom_pattern(tetromino_type, rotation)
        extracted = llm_response.tetromino_pattern.bit_grid

        # 3. Compare bit pattern
        pattern_matches = (extracted == ground_truth)
        
        # 4. Check visual representation
        expected_visual = []
        for row in ground_truth:
            visual_row = ""
            for cell in row:
                visual_row += "#" if cell == 1 else "."
            expected_visual.append(visual_row)
        
        actual_visual = llm_response.tetromino_pattern.visual
        visual_matches = (expected_visual == actual_visual)
        
        # 5. Prepare detailed results
        details = {
            "pattern_match": pattern_matches,
            "visual_match": visual_matches,
            "expected_pattern": ground_truth,
            "extracted_pattern": extracted,
            "expected_visual": expected_visual,
            "actual_visual": actual_visual
        }

        # 6. Scoring based on updated rubric
        pattern_score = 70 if pattern_matches else 0  # Pattern extraction worth 70 points
        visual_score = 30 if visual_matches else 0    # Visual representation worth 30 points
        
        score = pattern_score + visual_score
        
        passed = score >= 100
        return passed, details, score, 100

    except Exception as e:
        return False, {"error": str(e)}, 0, 0