from pydantic import BaseModel, Field
from typing import Optional

class CodeSolutionOutput(BaseModel):
    """
    Defines the expected structured output containing reasoning and
    the generated Python code solution.
    """
    reasoning: str = Field(
        ...,
        description="Detailed step-by-step reasoning for how the solution code was derived and how it solves the problem."
    )
    solution_code: str = Field(
        ...,
        description="The complete, executable Python code block that solves the requested task."
    )

class Response_structure(BaseModel):
    """
    The main response structure expected by the evaluation harness.
    For this task, it contains the reasoning and the code solution.
    """
    reasoning: str = Field(description="Detailed reasoning process to accomplish the task, please solve all the tasks step by step.")
    config: CodeSolutionOutput 