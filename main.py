import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from playwright.async_api import async_playwright, BrowserContext

# ==========================================================
# 1. Pydantic 数据模型
# ==========================================================

class Fence(BaseModel):
    poi_id: str
    radius: int
    center_lng: Optional[float] = None
    center_lat: Optional[float] = None

class Time(BaseModel):
    raw_text: str 

class AweTypeCode(BaseModel):
    code: str
    level: int

# --- 模型：画像接口 ---
class PortraitPayload(BaseModel):
    awe_poi_id: str
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode
    entity_type: int = 1

# --- 模型：门店列表接口 (TopK) ---
class TopkPayload(BaseModel):
    entity_type: int = 1
    entity_ids: List[str] = []
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

# --- 模型：商品套餐接口 (Products) ---
class ProductPayload(BaseModel):
    entity_type: int = 2 # 核心区别：必须是 2
    entity_ids: List[str] = []
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

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
    
    # 生产环境必须添加 --no-sandbox
    browser = await playwright_instance.chromium.launch(
        headless=True, 
        args=['--no-sandbox', '--disable-setuid-sandbox']
    )
    
    try:
        print(f"📂 加载 Cookie: {AUTH_FILE}")
        browser_context = await browser.new_context(storage_state=AUTH_FILE)
        print("✅ 浏览器上下文就绪")
    except Exception as e:
        print(f"⚠️ 加载 auth.json 失败: {e}")
        browser_context = await browser.new_context()

    yield
    
    print("🛑 服务关闭中...")
    if browser_context: await browser_context.close()
    if playwright_instance: await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

# ==========================================================
# 3. 核心签名函数 (浏览器内执行)
# ==========================================================

async def get_signed_response(target_url: str, payload: dict, user_id: str):
    if not browser_context:
        raise HTTPException(status_code=503, detail="Service not ready")

    page = await browser_context.new_page()
    response_future = asyncio.Future()

    # 监听响应
    async def handle_response(response):
        # 模糊匹配 URL 路径
        req_path = target_url.split('?')[0]
        if req_path in response.url and response.request.method == "POST":
            print(f"🔍 捕获响应: {response.status} | {req_path.split('/')[-1]}")
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
        # 1. 预导航：激活 Cookie 和 Origin
        try:
            await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=15000, wait_until="domcontentloaded")
        except:
            pass # 忽略导航超时，只要域名对了就行

        # 2. 注入 JS 发送请求 (利用浏览器的自动签名能力)
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
        await page.evaluate(js_code, payload)
        
        # 3. 等待结果
        result = await asyncio.wait_for(response_future, timeout=25.0)
        return result

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Request timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await page.close()

# ==========================================================
# 4. 接口路由
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'

# --- 1. 获取门店列表 (竞品) ---
@app.post("/topk")
async def get_topk_data(payload: TopkPayload):
    # URL 包含 /pois/v2
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/pois/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    return await get_signed_response(target_url, payload.dict(), USER_ID)

# --- 2. 获取商品套餐 (核心修复) ---
@app.post("/products")
async def get_products_data(payload: ProductPayload):
    # URL 是 /topk/v2 (没有 pois)
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    # 强制修正参数
    data = payload.dict()
    data['entity_type'] = 2 
    
    return await get_signed_response(target_url, data, USER_ID)

# --- 3. 获取画像 ---
@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    return await get_signed_response(target_url, payload.dict(), USER_ID)

@app.get("/")
def read_root():
    return {"status": "ok", "endpoints": ["/topk", "/products", "/portrait"]}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
