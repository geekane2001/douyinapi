import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from playwright.async_api import async_playwright, BrowserContext

# ==========================================================
# 1. Pydantic 数据模型定义
# ==========================================================

class Fence(BaseModel):
    poi_id: str
    radius: int
    # 经纬度设为可选，兼容不同调用情况
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

# --- 模型：门店列表 (TopK) ---
class TopkPayload(BaseModel):
    entity_type: int = 1 # 门店查询默认为 1
    entity_ids: List[str] = []
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

# --- 模型：商品套餐 (Products) ---
class ProductPayload(BaseModel):
    entity_type: int = 2 # 商品查询必须为 2
    entity_ids: List[str] = []
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

# ==========================================================
# 2. Playwright 生命周期
# ==========================================================

playwright_instance = None
browser_context: Optional[BrowserContext] = None
AUTH_FILE = "auth.json"

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser_context
    print("🚀 [System] 服务启动中...")
    
    playwright_instance = await async_playwright().start()
    
    # Docker 环境必备参数
    browser = await playwright_instance.chromium.launch(
        headless=True, 
        args=['--no-sandbox', '--disable-setuid-sandbox']
    )
    
    try:
        print(f"📂 [System] 加载 Cookie: {AUTH_FILE}")
        browser_context = await browser.new_context(storage_state=AUTH_FILE)
        print("✅ [System] 浏览器上下文就绪")
    except Exception as e:
        print(f"⚠️ [System] 加载 auth.json 失败: {e}")
        browser_context = await browser.new_context()

    yield
    
    print("🛑 [System] 服务关闭中...")
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

    # 日志监听
    page.on("console", lambda msg: print(f"[Browser JS] {msg.text}"))
    
    async def handle_response(response):
        # 模糊匹配 URL，忽略 query 参数
        req_path = target_url.split('?')[0]
        if req_path in response.url and response.request.method == "POST":
            print(f"🔍 [Network] 捕获响应: {response.status} | {req_path.split('/')[-1]}")
            if not response_future.done():
                if response.ok:
                    try:
                        response_future.set_result(await response.json())
                    except Exception as e:
                        response_future.set_exception(e)
                else:
                    try:
                        err = await response.text()
                        print(f"❌ [API Error] {err[:100]}")
                        response_future.set_exception(Exception(f"API Error {response.status}"))
                    except:
                        response_future.set_exception(Exception(f"API Error {response.status}"))

    page.on("response", handle_response)

    try:
        # 1. 预导航：激活 Cookie
        try:
            await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=10000, wait_until="domcontentloaded")
        except:
            pass 

        # 2. 注入 JS
        js_code = f"""
            async (payload) => {{
                console.log("🚀 发送 XHR: {target_url.split('?')[0]}");
                const xhr = new XMLHttpRequest();
                xhr.open('POST', '{target_url}', true);
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
        print("❌ [Timeout] 请求超时")
        raise HTTPException(status_code=504, detail="Upstream request timed out")
    except Exception as e:
        print(f"❌ [System Error] {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await page.close()

# ==========================================================
# 4. 接口路由 (必须严格区分!)
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'

# --- 1. 获取门店列表 (Competitors) ---
# 对应 Cloudflare 的 /fetch
@app.post("/topk")
async def get_topk_data(payload: TopkPayload):
    # ⚠️ 关键点：URL 必须包含 /pois/v2
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/pois/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    # 强制 entity_type = 1
    data = payload.dict()
    data['entity_type'] = 1
    
    print(f"📥 [API] /topk (门店列表) - Radius: {payload.locsight_fence.radius}")
    return await get_signed_response(target_url, data, USER_ID)

# --- 2. 获取商品套餐 (Products) ---
# 对应 Cloudflare 的 /products
@app.post("/products")
async def get_products_data(payload: ProductPayload):
    # ⚠️ 关键点：URL 是 /topk/v2 (没有 pois)
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    # 强制 entity_type = 2
    data = payload.dict()
    data['entity_type'] = 2
    
    print(f"📥 [API] /products (商品套餐) - Radius: {payload.locsight_fence.radius}")
    return await get_signed_response(target_url, data, USER_ID)

# --- 3. 获取用户画像 ---
# 对应 Cloudflare 的 /portrait
@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    base_url = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2'
    target_url = f"{base_url}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    
    print(f"📥 [API] /portrait (用户画像)")
    return await get_signed_response(target_url, payload.dict(), USER_ID)

@app.get("/")
def read_root():
    return {"status": "ok", "routes": ["/topk", "/products", "/portrait"]}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
