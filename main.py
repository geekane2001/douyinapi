import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Any
from playwright.async_api import async_playwright, BrowserContext

# ==========================================================
# 1. Pydantic 数据模型
# ==========================================================

class Fence(BaseModel):
    poi_id: str
    radius: int
    # 经纬度可选
    center_lng: Optional[float] = None
    center_lat: Optional[float] = None

class Time(BaseModel):
    raw_text: str 

class AweTypeCode(BaseModel):
    code: str
    level: int

# 通用 Payload 模型
class GeneralPayload(BaseModel):
    # 默认为 1，但在处理逻辑中会强制覆盖
    entity_type: int = 1
    # 允许接收 entity_ids，但在构建最终请求时会根据情况剔除
    entity_ids: List[str] = []
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

# 画像 Payload 模型 (保持不变)
class PortraitPayload(BaseModel):
    awe_poi_id: str
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode
    entity_type: int = 1

# ==========================================================
# 2. 全局变量与生命周期
# ==========================================================

playwright_instance = None
browser_context: Optional[BrowserContext] = None
AUTH_FILE = "auth.json"

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser_context
    print("🚀 服务启动中...")
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True, 
        args=['--no-sandbox', '--disable-setuid-sandbox']
    )
    try:
        print(f"📂 加载 Cookie: {AUTH_FILE}")
        browser_context = await browser.new_context(storage_state=AUTH_FILE)
    except Exception as e:
        print(f"⚠️ 加载 auth.json 失败: {e}")
        browser_context = await browser.new_context()
    yield
    print("🛑 服务关闭中...")
    if browser_context: await browser_context.close()
    if playwright_instance: await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

# ==========================================================
# 3. 核心签名函数
# ==========================================================

async def get_signed_response(target_url: str, payload: dict, user_id: str):
    if not browser_context:
        raise HTTPException(status_code=503, detail="Service not ready")

    page = await browser_context.new_page()
    response_future = asyncio.Future()

    # 日志
    page.on("console", lambda msg: print(f"[Browser JS] {msg.text}"))

    async def handle_response(response):
        req_path = target_url.split('?')[0]
        if req_path in response.url and response.request.method == "POST":
            print(f"🔍 捕获响应: {response.status}")
            if not response_future.done():
                if response.ok:
                    try:
                        response_future.set_result(await response.json())
                    except Exception as e:
                        response_future.set_exception(e)
                else:
                    try:
                        err = await response.text()
                        response_future.set_exception(Exception(f"API Error {response.status}: {err[:100]}"))
                    except:
                        response_future.set_exception(Exception(f"API Error {response.status}"))

    page.on("response", handle_response)

    try:
        # 预导航
        try:
            await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=8000, wait_until="domcontentloaded")
        except:
            pass 

        js_code = f"""
            async (payload) => {{
                const url = '{target_url}';
                const xhr = new XMLHttpRequest();
                xhr.open('POST', url, true);
                xhr.setRequestHeader('content-type', 'application/json');
                xhr.setRequestHeader('user', '{user_id}');
                xhr.send(JSON.stringify(payload));
            }}
        """
        # 这里传入的 payload 必须是纯净的字典
        await page.evaluate(js_code, payload)
        result = await asyncio.wait_for(response_future, timeout=25.0)
        return result

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await page.close()

# ==========================================================
# 4. 接口路由 (严格匹配老 Worker Payload)
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'
BASE_TOPK_URL = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'

# --- 1. 获取门店列表 (Competitors) ---
@app.post("/topk")
async def get_topk_data(payload: GeneralPayload):
    target_url = f"{BASE_TOPK_URL}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    # 构造严格符合老 Worker 的 Payload
    # 1. 强制 entity_type = 1
    # 2. 剔除 entity_ids
    # 3. locsight_fence 只保留 poi_id 和 radius
    
    clean_payload = {
        "entity_type": 1,
        "locsight_fence": {
            "poi_id": payload.locsight_fence.poi_id,
            "radius": payload.locsight_fence.radius
        },
        "locsight_time": {
            "raw_text": payload.locsight_time.raw_text
        },
        "awe_type_code": {
            "code": payload.awe_type_code.code,
            "level": payload.awe_type_code.level
        }
    }
    
    print(f"📥 [API] /topk (门店) - Payload Cleaned. Type: 1")
    return await get_signed_response(target_url, clean_payload, USER_ID)

# --- 2. 获取商品套餐 (Products) ---
@app.post("/products")
async def get_products_data(payload: GeneralPayload):
    target_url = f"{BASE_TOPK_URL}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    # 商品接口 entity_type = 2
    # 结构通常与门店接口一致，只是 type 不同
    clean_payload = {
        "entity_type": 2,
        "locsight_fence": {
            "poi_id": payload.locsight_fence.poi_id,
            "radius": payload.locsight_fence.radius
        },
        "locsight_time": {
            "raw_text": payload.locsight_time.raw_text
        },
        "awe_type_code": {
            "code": payload.awe_type_code.code,
            "level": payload.awe_type_code.level
        }
    }
    
    # 如果商品接口确实需要经纬度，可以在这里加回去，但根据老Worker逻辑，先保持最简
    if payload.locsight_fence.center_lng:
        clean_payload["locsight_fence"]["center_lng"] = payload.locsight_fence.center_lng
        clean_payload["locsight_fence"]["center_lat"] = payload.locsight_fence.center_lat

    print(f"📥 [API] /products (套餐) - Payload Cleaned. Type: 2")
    return await get_signed_response(target_url, clean_payload, USER_ID)

# --- 3. 获取画像 ---
@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    print(f"📥 [API] /portrait (用户画像)")
    return await get_signed_response(target_url, payload.dict(), USER_ID)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
