import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional
from playwright.async_api import async_playwright, BrowserContext

# ==========================================================
# 1. Pydantic 模型：定义请求体的数据结构，用于自动验证
# ==========================================================

class Fence(BaseModel):
    poi_id: str
    radius: int

class Time(BaseModel):
    raw_text: str # e.g., "2025-10"

class AweTypeCode(BaseModel):
    code: str
    level: int

# --- Payload for the 'portrait' API ---
class PortraitPayload(BaseModel):
    awe_poi_id: str
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode
    entity_type: int = 1

# --- Payload for the 'topk' API ---
# 注意：根据你的JS代码，第二个接口是 /topk/v2，它可能需要不同的参数
# 这里根据第一个接口的结构创建了一个合理的模型，你可能需要根据实际情况调整
class TopkPayload(BaseModel):
    entity_type: int = 1
    entity_ids: List[str]
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

# ==========================================================
# 2. 全局变量和 Playwright 生命周期管理
# ==========================================================

# 存储 Playwright 实例的全局变量
playwright_instance = None
browser_context: Optional[BrowserContext] = None
AUTH_FILE = "auth.json"

# 使用 FastAPI 的 lifespan 管理器，在服务启动时开启浏览器，在关闭时关闭
@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser_context
    print("服务启动中，正在初始化 Playwright...")
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(headless=True) # 在 Docker 中必须是 headless
    browser_context = await browser.new_context(storage_state=AUTH_FILE)
    print("Playwright 初始化完成，浏览器已准备就绪。")
    yield
    print("服务关闭中，正在关闭 Playwright...")
    await browser_context.close()
    await playwright_instance.stop()
    print("Playwright 已成功关闭。")

app = FastAPI(lifespan=lifespan)

# ==========================================================
# 3. 核心签名函数
# ==========================================================

async def get_signed_response(target_url: str, payload: dict, user_id: str):
    if not browser_context:
        raise HTTPException(status_code=503, detail="Playwright service not ready")

    response_future = asyncio.Future()
    page = await browser_context.new_page()

    async def handle_response(response):
        if target_url in response.url and response.request.method == "POST":
            print(f"✅ 成功捕获API响应: {response.status}")
            if not response_future.done():
                try:
                    json_data = await response.json()
                    response_future.set_result(json_data)
                except Exception as e:
                    response_future.set_exception(e)
    
    page.on("response", handle_response)

    js_function = f"""
        async (payload) => {{
            const url = '{target_url}';
            const body = JSON.stringify(payload);
            const xhr = new XMLHttpRequest();
            xhr.open('POST', url, true);
            xhr.setRequestHeader('content-type', 'application/json');
            xhr.setRequestHeader('user', '{user_id}');
            xhr.send(body);
        }}
    """
    
    try:
        await page.evaluate(js_function, payload)
        result = await asyncio.wait_for(response_future, timeout=20.0)
        return result
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request to target API timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"An internal error occurred: {str(e)}")
    finally:
        await page.close()

# ==========================================================
# 4. FastAPI 接口端点
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'

@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    """
    代理并签名 /arrive/portrait/v2 接口
    """
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    print(f"收到 /portrait 请求, poi_id: {payload.awe_poi_id}")
    response_data = await get_signed_response(target_url, payload.dict(), USER_ID)
    return response_data

@app.post("/topk")
async def get_topk_data(payload: TopkPayload):
    """
    代理并签名 /topk/v2 接口 (注意：原始脚本是 /topk/pois/v2)
    """
    # 请确认这个URL是否正确，你的JS代码中是 /topk/v2
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    print(f"收到 /topk 请求, poi_id: {payload.locsight_fence.poi_id}")
    response_data = await get_signed_response(target_url, payload.dict(), USER_ID)
    return response_data

@app.get("/")
def read_root():
    return {"status": "Signature service is running"}
