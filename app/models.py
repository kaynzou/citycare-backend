import enum
import uuid
from datetime import datetime

from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Enum
from sqlalchemy.orm import relationship

from .database import Base


class VerificationStatus(str, enum.Enum):
    verified = "Verified"
    in_progress = "In Progress"
    rejected = "Rejected"


class Complaint(Base):
    __tablename__ = "complaints"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    complaint_no = Column(String, unique=True, index=True, nullable=False)  # e.g. CMPI1234

    category = Column(String, nullable=False)
    location = Column(String, nullable=False)

    original_text = Column(Text, nullable=False)
    detected_language = Column(String, default="en")

    translated_text = Column(Text, nullable=True)
    translated_language = Column(String, default="en")

    status = Column(Enum(VerificationStatus), default=VerificationStatus.in_progress)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    photos = relationship(
        "ComplaintPhoto", back_populates="complaint", cascade="all, delete-orphan"
    )


class ComplaintPhoto(Base):
    __tablename__ = "complaint_photos"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    complaint_id = Column(String, ForeignKey("complaints.id"), nullable=False)
    file_path = Column(String, nullable=False)  # served path, e.g. /uploads/CMPI1234/xyz.jpg

    complaint = relationship("Complaint", back_populates="photos")
