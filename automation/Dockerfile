# Use the official lightweight Python image.
# https://hub.docker.com/_/python
FROM python:3.11-slim

# Allow statements and log messages to immediately appear in the logs
ENV PYTHONUNBUFFERED True

# Copy local code to the container image.
ENV APP_HOME /app
WORKDIR $APP_HOME
COPY . ./

# Install protobuf
RUN apt-get update
RUN apt-get install -y protobuf-compiler

# Install production dependencies.
RUN pip install --no-cache-dir -r requirements.txt

# Generate config_pb2.py
RUN protoc --python_out=. config.proto

CMD python main.py
