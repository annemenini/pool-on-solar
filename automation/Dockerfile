# Use the official lightweight Python image.
# https://hub.docker.com/_/python
FROM python:3.14-slim

# Allow statements and log messages to immediately appear in the logs
ENV PYTHONUNBUFFERED=True

# Copy local code to the container image.
ENV APP_HOME=/app
WORKDIR $APP_HOME
COPY . ./

# Install production dependencies together with grpcio-tools so pip resolves a
# protoc compiler version that matches the installed protobuf Python runtime,
# then compile config.proto and remove the build-only grpcio-tools package.
RUN pip install --no-cache-dir -r requirements.txt grpcio-tools \
    && python -m grpc_tools.protoc -I. --python_out=. config.proto \
    && pip uninstall -y grpcio-tools

CMD ["python", "main.py"]
