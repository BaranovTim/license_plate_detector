# JetPack 4 (Ubuntu 18.04) is too old for onnxruntime's wheels, so the service runs in a
# small Debian container. Recognition is ONNX on the CPU: the Nano's GPU isn't needed, which
# keeps power draw low enough for a powerbank.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Download the detector + OCR models (~11 MB) at build time so the device works without internet
RUN python -c "from fast_alpr import ALPR; ALPR(detector_model='yolo-v9-t-384-license-plate-end2end', ocr_model='cct-xs-v2-global-model', ocr_device='cpu', detector_providers=['CPUExecutionProvider'])"

COPY anpr_device.py .
CMD ["python", "anpr_device.py"]
