from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel


class ComplaintOut(BaseModel):
    # Shaped to match what CityCarePages.jsx expects: complaint.id, .category,
    # .location, .description, .photos (array of URLs), .status
    id: str
    category: str
    location: str
    description: str
    detected_language: str
    translated_text: Optional[str] = None
    translated_language: str
    photos: List[str]
    status: str
    created_at: datetime

    class Config:
        from_attributes = True


class StatusUpdate(BaseModel):
    status: str  # "Verified" | "In Progress" | "Rejected"
