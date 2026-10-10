from typing import Optional, List
from pydantic import BaseModel, Field

class ConfigFile(BaseModel):
    tetromino_type: str = Field(description="Type of the tetromino (I, O, T, L, J, Z, S)")
    rotation: int = Field(description="Rotation state (0, 1, 2, 3)")
    transformation: Optional[str] = Field(default=None, description="Transformation applied (rotate, reflect, etc.)")

class TetrominoPattern(BaseModel):
    bit_grid: List[List[int]] = Field(description="4x4 grid representing the tetromino bit pattern")
    visual: List[str] = Field(description="Visual representation of the tetromino using '#' and '.'")

class TetrominoTransformation(BaseModel):
    transformation_type: str = Field(description="Type of transformation (rotate, reflect, etc.)")
    bit_grid: List[List[int]] = Field(description="Transformed 4x4 grid")
    visual: List[str] = Field(description="Visual representation after transformation")

class VerificationResult(BaseModel):
    matches_rom: bool = Field(description="Whether the extracted pattern matches the ROM")
    details: Optional[str] = Field(default=None, description="Details of the verification")

class EngineeringReport(BaseModel):
    summary: str = Field(description="Summary of the analysis and findings")
    steps: List[str] = Field(description="Step-by-step explanation of the process")

class Response_structure(BaseModel):
    reasoning: str = Field(..., description="Detailed reasoning process to accomplish the task, please solve all the tasks step by step")
    config: ConfigFile
    tetromino_pattern: TetrominoPattern
    transformed_pattern: Optional[TetrominoTransformation] = None
    verification_result: Optional[VerificationResult] = None