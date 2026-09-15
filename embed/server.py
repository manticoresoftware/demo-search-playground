from __future__ import annotations

import base64
import binascii
import io

import torch
from fastapi import FastAPI, HTTPException
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel
from transformers import CLIPModel, CLIPProcessor

# CLIP fine-tuned on fashion product photos and their descriptions; 512-dimensional vectors.
MODEL_NAME = "patrickjohncyh/fashion-clip"

model = CLIPModel.from_pretrained(MODEL_NAME).eval()
processor = CLIPProcessor.from_pretrained(MODEL_NAME)
app = FastAPI(title="Fashion CLIP embeddings")


class TextRequest(BaseModel):
    text: str


class ImagesRequest(BaseModel):
    # Base64-encoded image files.
    images: list[str]


def normalized(features: torch.Tensor) -> list[list[float]]:
    return torch.nn.functional.normalize(features, dim=-1).tolist()


def decode_image(data: str) -> Image.Image:
    try:
        return Image.open(io.BytesIO(base64.b64decode(data, validate=True))).convert("RGB")
    except (binascii.Error, UnidentifiedImageError) as exc:
        raise HTTPException(status_code=400, detail="Not a supported image file") from exc


@app.post("/text")
def embed_text(request: TextRequest) -> list[float]:
    inputs = processor(text=[request.text], return_tensors="pt", padding=True, truncation=True)
    with torch.inference_mode():
        return normalized(model.get_text_features(**inputs))[0]


@app.post("/images")
def embed_images(request: ImagesRequest) -> list[list[float]]:
    inputs = processor(images=[decode_image(data) for data in request.images], return_tensors="pt")
    with torch.inference_mode():
        return normalized(model.get_image_features(**inputs))
