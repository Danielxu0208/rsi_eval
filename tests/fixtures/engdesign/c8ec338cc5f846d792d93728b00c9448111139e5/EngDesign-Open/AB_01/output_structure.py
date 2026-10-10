from pydantic import BaseModel, Field
import json

class RadiomicsOutput(BaseModel):
    mean: float = Field(description="Mean intensity value of the ROI")
    variance: float = Field(description="Sample variance of intensity values in the ROI")
    skewness: float = Field(description="Skewness of intensity distribution in the ROI")
    kurtosis: float = Field(description="Excess kurtosis of intensity distribution in the ROI")
    contrast: float = Field(description="GLCM contrast feature computed from ROI pixels")

# This is the expected output structure of the LLM
class Response_structure(BaseModel):
    reasoning: str = Field(..., description="Detailed reasoning process to accomplish the task, please solve all the tasks step by step.")
    config: RadiomicsOutput

