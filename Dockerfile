FROM python:3.12-slim

# AI Ride Coach — single stdlib Python process, no dependencies to install.
WORKDIR /app
COPY . .

# Non-root runtime user.
RUN useradd --create-home --uid 10001 coach && \
    mkdir -p /app/.runtime && chown -R coach:coach /app
USER coach

# HF Spaces routes external traffic to container port 7860.
# Zeabur/Render-style routers inject PORT at runtime; 7860 is the local default.
ENV LUMA_COACH_ALLOW_REMOTE=1 \
    PORT=7860
# LUMA_COACH_MASTER_KEY and LUMA_COACH_PUBLIC_ORIGIN are injected as
# platform secrets at runtime; without the master key the keystore fails closed.
EXPOSE 7860
CMD ["python", "coach_server.py", "--host", "0.0.0.0"]
