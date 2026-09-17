# Synthetic Linux fixture only; this image is not a qualified VS Code deployment.
FROM python:3.12-slim
COPY src/lab/remote/editor_recovery.py /fixture/helper.py
COPY tests/disposable/recovery.py /fixture/recovery.py
ENV LAB_DISPOSABLE_RECOVERY_FIXTURE=1
CMD ["sleep", "infinity"]
