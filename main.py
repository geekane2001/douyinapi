import asyncio
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional, Any
from playwright.async_api import async_playwright, BrowserContext

# ==========================================================
# 1. 数据模型
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

# 通用请求体
class GeneralPayload(BaseModel):
    entity_type: int = 1
    entity_ids: Optional[List[str]] = []
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode

# 画像请求体
class PortraitPayload(BaseModel):
    awe_poi_id: str
    locsight_fence: Fence
    locsight_time: Time
    awe_type_code: AweTypeCode
    entity_type: int = 1

# ==========================================================
# 2. 系统初始化
# ==========================================================

playwright_instance = None
browser_context: Optional[BrowserContext] = None
AUTH_FILE = "auth.json"

@asynccontextmanager
async def lifespan(app: FastAPI):
    global playwright_instance, browser_context
    print("🚀 [System] 服务启动中...")
    playwright_instance = await async_playwright().start()
    browser = await playwright_instance.chromium.launch(
        headless=True, 
        args=['--no-sandbox', '--disable-setuid-sandbox']
    )
    try:
        browser_context = await browser.new_context(storage_state=AUTH_FILE)
        print("✅ [System] Cookie 加载成功")
    except:
        print("⚠️ [System] Cookie 加载失败，将使用空上下文")
        browser_context = await browser.new_context()
    yield
    if browser_context: await browser_context.close()
    if playwright_instance: await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

# ==========================================================
# 3. 核心请求逻辑
# ==========================================================

USER_ID = '72410786115270758551286874604511870'
ROOT_ACCOUNT_ID = '7241078611527075855'
BASE_TOPK_URL = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/v2'
BASE_POIS_URL = 'https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/topk/pois/v2'

async def fetch_api(page, url, payload):
    """浏览器内执行 fetch"""
    js_code = f"""
        async (payload) => {{
            try {{
                const response = await fetch('{url}', {{
                    method: 'POST',
                    headers: {{ 'content-type': 'application/json', 'user': '{USER_ID}' }},
                    body: JSON.stringify(payload)
                }});
                return await response.json();
            }} catch(e) {{ return {{ code: -1, msg: e.toString() }}; }}
        }}
    """
    return await page.evaluate(js_code, payload)

async def get_signed_response(url: str, payload: dict):
    if not browser_context: raise HTTPException(503, "Service not ready")
    page = await browser_context.new_page()
    try:
        # 预导航防 504
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass
        
        return await fetch_api(page, url, payload)
    finally:
        await page.close()

def build_payload(origin: GeneralPayload, etype: int):
    """构造纯净的 Payload，剔除不必要的字段"""
    return {
        "entity_type": etype,
        "locsight_fence": origin.locsight_fence.dict(exclude_none=True),
        "locsight_time": origin.locsight_time.dict(),
        "awe_type_code": origin.awe_type_code.dict()
    }

# ==========================================================
# 4. 接口路由
# ==========================================================

# --- 1. 门店列表 (TopK) ---
@app.post("/topk")
async def get_topk_data(payload: GeneralPayload):
    url = f"{BASE_POIS_URL}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    # 强制 entity_type = 1
    data = build_payload(payload, 1)
    print(f"📥 [API] /topk (Radius: {payload.locsight_fence.radius})")
    return await get_signed_response(url, data)

# --- 2. 画像数据 ---
@app.post("/portrait")
async def get_portrait_data(payload: PortraitPayload):
    url = f"https://lbs-locsight.bytedance.com/lbs/analysis/v1/customize/busi_bible/locsight/arrive/portrait/v2?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
    print(f"📥 [API] /portrait")
    return await get_signed_response(url, payload.dict())

# --- 3. 商品套餐 (智能组装版) ---
@app.post("/products")
async def get_products_data(payload: GeneralPayload):
    print(f"📥 [API] /products (智能组装模式)")
    if not browser_context: raise HTTPException(503)
    page = await browser_context.new_page()
    
    try:
        try: await page.goto("https://lbs-locsight.bytedance.com/locsight/result", timeout=5000, wait_until="domcontentloaded")
        except: pass

        # 构造两个请求：一个查商品详情，一个查门店关联
        url_products = f"{BASE_TOPK_URL}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        url_stores   = f"{BASE_POIS_URL}?user={USER_ID}&root_account_id={ROOT_ACCOUNT_ID}"
        
        payload_products = build_payload(payload, 2)
        payload_stores   = build_payload(payload, 2) # 查关联时 entity_type 也要是 2

        # 并行请求
        task1 = fetch_api(page, url_products, payload_products)
        task2 = fetch_api(page, url_stores, payload_stores)
        
        res_products, res_stores = await asyncio.gather(task1, task2)

        # 提取数据
        products_list = res_products.get('data', {}).get('product_operation', [])
        stores_list = res_stores.get('data', {}).get('pois', [])

        print(f"   ↳ 抓取结果: {len(products_list)} 商品, {len(stores_list)} 门店")

        # 组装数据 (Join)
        product_map = {p['product_id']: p for p in products_list}
        final_result = []

        for store in stores_list:
            store_obj = {
                "awe_poi_id": store.get('awe_poi_id'),
                "name": store.get('name'), # 获取到了店名！
                "product_details": []
            }
            # 关联
            related_ids = store.get('related_entity_ids', [])
            for pid in related_ids:
                if pid in product_map:
                    store_obj['product_details'].append(product_map[pid])
            
            # 只返回有商品的门店
            if len(store_obj['product_details']) > 0:
                final_result.append(store_obj)

        return {
            "code": 0,
            "message": "success",
            "data": final_result # 结构: [{awe_poi_id, name, product_details}, ...]
        }

    except Exception as e:
        print(f"❌ [Error] {e}")
        raise HTTPException(500, str(e))
    finally:
        await page.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
