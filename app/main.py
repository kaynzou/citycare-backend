import os
import random
import string
from typing import List, Optional

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import models, schemas
from .database import engine, get_db
from .translation import translate_text

models.Base.metadata.create_all(bind=engine)

app = FastAPI(title="CityCare API")

# Dev-friendly CORS — tighten allow_origins to your real frontend URL before shipping.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


def generate_complaint_no(db: Session) -> str:
    """CMPI1234-style ID, matching the mockup. Retries on the rare collision."""
    for _ in range(10):
        candidate = "CMPI" + "".join(random.choices(string.digits, k=4))
        exists = db.query(models.Complaint).filter_by(complaint_no=candidate).first()
        if not exists:
            return candidate
    raise HTTPException(500, "Could not generate a unique complaint number")


def to_complaint_out(complaint: models.Complaint) -> schemas.ComplaintOut:
    return schemas.ComplaintOut(
        id=f"#{complaint.complaint_no}",
        category=complaint.category,
        location=complaint.location,
        description=complaint.original_text,
        detected_language=complaint.detected_language,
        translated_text=complaint.translated_text,
        translated_language=complaint.translated_language,
        photos=[f"/uploads/{complaint.complaint_no}/{os.path.basename(p.file_path)}"
                for p in complaint.photos],
        status=complaint.status.value,
        created_at=complaint.created_at,
    )


@app.post("/api/complaints", response_model=schemas.ComplaintOut)
async def create_complaint(
    category: str = Form(...),
    location: str = Form(...),
    description: str = Form(...),
    target_language: str = Form("en"),
    photos: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    complaint_no = generate_complaint_no(db)
    detected_lang, translated = translate_text(description, target_language)

    complaint = models.Complaint(
        complaint_no=complaint_no,
        category=category,
        location=location,
        original_text=description,
        detected_language=detected_lang,
        translated_text=translated,
        translated_language=target_language,
    )
    db.add(complaint)
    db.flush()  # get complaint.id before attaching photos

    complaint_upload_dir = os.path.join(UPLOAD_DIR, complaint_no)
    os.makedirs(complaint_upload_dir, exist_ok=True)

    for photo in photos:
        dest_path = os.path.join(complaint_upload_dir, photo.filename)
        with open(dest_path, "wb") as f:
            f.write(await photo.read())
        db.add(models.ComplaintPhoto(complaint_id=complaint.id, file_path=dest_path))

    db.commit()
    db.refresh(complaint)
    return to_complaint_out(complaint)


@app.get("/api/complaints", response_model=List[schemas.ComplaintOut])
def list_complaints(
    status: Optional[str] = None,
    category: Optional[str] = None,
    db: Session = Depends(get_db),
):
    query = db.query(models.Complaint)
    if status:
        query = query.filter(models.Complaint.status == status)
    if category:
        query = query.filter(models.Complaint.category == category)
    return [to_complaint_out(c) for c in query.order_by(models.Complaint.created_at.desc())]


@app.get("/api/complaints/{complaint_no}", response_model=schemas.ComplaintOut)
def get_complaint(complaint_no: str, db: Session = Depends(get_db)):
    complaint = db.query(models.Complaint).filter_by(complaint_no=complaint_no).first()
    if not complaint:
        raise HTTPException(404, f"No complaint with ID {complaint_no}")
    return to_complaint_out(complaint)


@app.patch("/api/complaints/{complaint_no}/status", response_model=schemas.ComplaintOut)
def update_status(complaint_no: str, payload: schemas.StatusUpdate, db: Session = Depends(get_db)):
    complaint = db.query(models.Complaint).filter_by(complaint_no=complaint_no).first()
    if not complaint:
        raise HTTPException(404, f"No complaint with ID {complaint_no}")

    valid_statuses = {s.value for s in models.VerificationStatus}
    if payload.status not in valid_statuses:
        raise HTTPException(422, f"status must be one of {valid_statuses}")

    complaint.status = payload.status
    db.commit()
    db.refresh(complaint)
    return to_complaint_out(complaint)


@app.get("/")
def health_check():
    return {"status": "CityCare API running"}
