FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制你的应用代码和认证文件到镜像中
COPY . .

# 暴露端口 8000
EXPOSE 8000

# 容器启动时运行 FastAPI 应用
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
