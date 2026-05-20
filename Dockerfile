FROM python:3.12-slim
WORKDIR /app
COPY server.py ./server.py
COPY static ./static
ENV PORT=8080
EXPOSE 8080
CMD ["python", "server.py"]
